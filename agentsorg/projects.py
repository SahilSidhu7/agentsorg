"""Projects: one owner command -> one project -> one result the owner reviews.

planning -> running -> review (all tasks ended; one combined result) -> done (accepted) | failed | cancelled
A result nobody opened for REVIEW_DAYS is closed by itself (code is never merged that way: its branch is discarded).

Every task of a project carries project_id. A compact board (shared/projects/<id>/board.md) is the only
cross-department context every task in the project sees: decisions, open questions, each team's latest summary.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path

log = logging.getLogger("agentsorg")
TERMINAL = ("done", "cached", "failed", "cancelled", "archived")
OPEN = ("queued", "waiting", "running", "held", "backlog", "queued_blocked", "routine_blocked")
WORKING = ("queued", "waiting", "running", "held", "queued_blocked", "routine_blocked")   # parked suggestions don't count
BOARD_CHARS = 3000
REVIEW_DAYS = 7
COMBINE_CHARS = 6000
COMBINE_PROMPT = """Write the project result for the company's owner from the material below. Markdown, no preamble.
What the OWNER ASKED is decided: report how it was delivered; never argue against it or for an older decision.
Structure:
## Delivered vs asked
The ACCEPTANCE table as given (requirement, status, evidence). Do not upgrade any status.
## What was built and where
Concrete files, branches, documents and how to try each one. Mention the CHANGES and the TESTS result exactly as
given. Accepting this result merges the changes; never give manual git merge or revert commands.
## Not done
Every FAILED task and every missing requirement, with the plain reason. Write "nothing" only if there is nothing.
## Decisions for you
At most 5, most important first, each one line with your recommended answer.
## Assumed (change any)
Everything else a careful team would decide by default, one line each with the default chosen.
Keep every concrete fact, number and piece of copy/code worth keeping; remove repetition and internal chatter."""


def _title(command: str) -> str:
    t = re.sub(r"\s+", " ", command).strip()
    return (t[:77] + "…") if len(t) > 78 else t


class Projects:
    def __init__(self, org):
        self.org = org
        self.s = org.s

    # ---------- records ----------
    def create(self, command: str, status: str = "planning") -> int:
        now = time.time()
        return self.s.x("INSERT INTO projects(created,updated,title,command,status) VALUES(?,?,?,?,?)",
                        (now, now, _title(command), command, status))

    def set(self, pid: int, **f) -> None:
        f["updated"] = time.time()
        if "plan" in f and not isinstance(f["plan"], str):
            f["plan"] = json.dumps(f["plan"])
        self.s.x(f"UPDATE projects SET {','.join(k + '=?' for k in f)} WHERE id=?", (*f.values(), pid))

    def get(self, pid: int) -> dict | None:
        p = self.s.one("SELECT * FROM projects WHERE id=?", (pid,))
        if not p:
            return None
        p["plan"] = json.loads(p["plan"]) if p.get("plan") else None
        p["tasks"] = self.tasks(pid)
        p["messages"] = self.s.q("SELECT * FROM messages WHERE project_id=? ORDER BY id", (pid,))
        p["suggestions"] = self.suggestions(pid)
        p["review"] = json.loads(p["review"]) if p.get("review") else None
        p["changes"] = self.s.q("SELECT id,source,branch,status,files,worktree FROM changes WHERE project_id=?", (pid,))
        p["tokens_used"] = self.s.one("SELECT COALESCE(SUM(u.input+u.output+u.cache_create),0) n FROM usage u "
                                      "JOIN tasks t ON t.id=u.task_id WHERE t.project_id=?", (pid,))["n"]
        return p

    # ---------- questions for the owner ----------
    # older agents wrote owner questions as NEXT lines; these words mark one
    OWNER_GATE = re.compile(r"\b(founder|owner|sign[- ]?off|once you|after you|when you|"
                            r"you (pick|choose|decide|confirm|set|approve))\b", re.I)

    _suggest_lock = threading.Lock()                     # parallel tasks append to the same JSON list

    def suggest(self, pid: int, text: str, dept: str, member: str, from_task: int) -> None:
        """A QUESTION: line from project work: one question for the owner (answered before: applied instead)."""
        with self._suggest_lock:
            self._suggest(pid, text, dept, member, from_task)

    def _suggest(self, pid: int, text: str, dept: str, member: str, from_task: int) -> None:
        p = self.s.one("SELECT suggestions FROM projects WHERE id=?", (pid,))
        if not p:
            return
        items = json.loads(p["suggestions"]) if p.get("suggestions") else []
        kind = "question"
        norm = re.sub(r"\W+", " ", text.lower()).strip()
        if any(re.sub(r"\W+", " ", i["text"].lower()).strip() == norm for i in items):
            return
        if sum(1 for i in items if i["kind"] == kind) >= 5:
            return
        text = text[:300]
        if kind == "question":
            from . import decisions
            src = self.s.one("SELECT body FROM tasks WHERE id=?", (from_task,)) or {}
            had_answer = re.match(r"(This was already decided|The owner (answered|changed))",
                                  src.get("body") or "")
            # asked and answered before: apply it, don't ask. If the team had that answer and still asks, it
            # needs something new, so the question goes to the owner (no loop).
            if not had_answer and (prior := decisions.already_decided(self.org, text)):
                items.append({"id": len(items), "kind": kind, "text": text, "dept": dept, "member": member,
                              "task": from_task, "state": "answered", "answer": prior["answer"], "auto": prior["by"]})
                self.set(pid, suggestions=json.dumps(items))
                self.org.submit(dept, f"This was already decided ({prior['by']}), so it was not asked again.\n"
                                      f"QUESTION: {text}\nDECISION: {prior['answer']}\nApply it and finish what was "
                                      "waiting on it.", member=member, source="ceo", priority=2, project_id=pid,
                                kind="deliverable")
                return
        if kind == "question" and (ctx := self._asked_context(from_task)):
            text += f" (Context: {ctx})"                # "once the founder picks A" means nothing without the A
        items.append({"id": len(items), "kind": kind, "text": text[:900], "dept": dept, "member": member,
                      "task": from_task, "state": "open"})
        self.set(pid, suggestions=json.dumps(items))

    def _asked_context(self, task_id: int) -> str:
        """When the task continued after another team's answer, the question and the start of that answer."""
        t = self.s.one("SELECT depends_on FROM tasks WHERE id=?", (task_id,))
        if not t or not t.get("depends_on"):
            return ""
        m = self.s.one("SELECT to_dept, question FROM messages WHERE answer_task=? ORDER BY id DESC LIMIT 1",
                       (t["depends_on"],))
        a = self.s.one("SELECT result FROM tasks WHERE id=?", (t["depends_on"],))
        if not m or not a:
            return ""
        ans = " ".join(re.sub(r"(?m)^(DONE|NEXT|ASK):.*$", "", a.get("result") or "").split())
        return f"asked {m['to_dept']} \"{m['question'][:200]}\"; {m['to_dept']} answered: {ans[:350]}"

    def suggestions(self, pid: int) -> list[dict]:
        p = self.s.one("SELECT suggestions FROM projects WHERE id=?", (pid,))
        return json.loads(p["suggestions"]) if p and p.get("suggestions") else []

    def act_suggestion(self, pid: int, sid: int, action: str) -> int | None:
        with self._suggest_lock:
            items = self.suggestions(pid)
            it = next((i for i in items if i["id"] == sid and i["state"] == "open"), None)
            if not it:
                raise KeyError("no such suggestion")
            it["state"] = "done" if action == "run" else "dismissed"
            self.set(pid, suggestions=json.dumps(items))
        tid = None
        if action == "run":
            tid = self.org.submit(it["dept"], it["text"], member=it["member"], source="ceo", priority=2, owner=True,
                                  project_id=pid, kind="deliverable")
            self.set(pid, status="running")
        return tid

    def answer_suggestion(self, pid: int, sid: int, text: str, save: bool = True, run: bool = False,
                          by: str = "owner") -> dict:
        """The owner (or Research, when the owner lets the company decide) answers a question. Saved as a company
        fact it is never asked again (planner FACTS, every team's KB); `run` hands the answer to the team that asked."""
        from . import decisions
        text = " ".join(str(text or "").split())[:600]
        if not text:
            raise ValueError("write an answer")
        with self._suggest_lock:
            items = self.suggestions(pid)
            it = next((i for i in items if i["id"] == sid and i["state"] in ("open", "delegated")), None)
            if not it:
                raise KeyError("no such question")
            it["state"], it["answer"] = "answered", text
            if by != "owner":
                it["auto"] = by
            self.set(pid, suggestions=json.dumps(items))
        q = re.sub(r"\s*\(Context:.*$", "", it["text"], flags=re.S)
        q = re.sub(r"^Owner decision needed \([^)]*\):\s*", "", q)[:110]
        if save:
            if by == "owner":
                self.org.kb.add(f"Owner: {q}", text, tags="owner decision")
            else:
                self.org.kb.add(f"Decided by {by}: {q}", text, tags="company decision")
        decisions.remember(self.org, it["text"], text, by)
        self.org.log_decision("answer", q, text[:200], ref=f"project:{pid}")
        tid = None
        if run and it["dept"] in self.org.c.depts:
            head = "The owner answered your question." if by == "owner" else \
                f"Research decided your open question (the owner lets the company decide; {by} answered)."
            tid = self.org.submit(it["dept"], f"{head}\nQUESTION: {it['text'][:600]}\n"
                                              f"ANSWER: {text}\nApply the answer to the work of this project and "
                                              f"finish what was waiting on it.", member=it["member"],
                                  source="ceo" if by == "owner" else "duty",
                                  priority=2, owner=by == "owner", project_id=pid, kind="deliverable")
            self.set(pid, status="running")
        return {"task": tid, "saved": bool(save)}

    def tasks(self, pid: int) -> list[dict]:
        return self.s.q("SELECT id,dept,member,title,status,kind,tier,model,done_line,summary,output_path,tokens,error,"
                        "depends_on,final,files,created,updated FROM tasks WHERE project_id=? AND dept!='ceo' ORDER BY id",
                        (pid,))

    def list(self, status: str | None = None, limit: int = 50) -> list[dict]:
        sql, args = "SELECT id,created,updated,title,status,final FROM projects", ()
        if status:
            sql, args = sql + " WHERE status=?", (status,)
        rows = self.s.q(sql + " ORDER BY id DESC LIMIT ?", (*args, max(1, min(limit, 200))))
        for r in rows:
            c = self.s.one("SELECT COUNT(*) n, SUM(status IN ('done','cached')) d FROM tasks WHERE project_id=? "
                           "AND dept!='ceo'", (r["id"],))
            r["tasks"], r["tasks_done"] = c["n"] or 0, c["d"] or 0
        return rows

    # ---------- held plans ----------
    def release(self, pid: int) -> int:
        """Start a held plan: each task queues, or waits for the step it needs."""
        n = 0
        for t in self.s.q("SELECT id, depends_on FROM tasks WHERE project_id=? AND status='held' ORDER BY id", (pid,)):
            dep = self.s.task(t["depends_on"]) if t["depends_on"] else None
            status = "queued" if not dep or dep["status"] in ("done", "cached") else \
                "failed" if dep["status"] in ("failed", "cancelled") else "waiting"
            self.s.update_task(t["id"], status=status, **({"error": "an earlier step failed"} if status == "failed" else {}))
            n += 1
        self.set(pid, status="running")
        self.org.pump()
        return n

    def approve_plan(self, pid: int, why: str = "") -> int:
        n = self.release(pid)
        self.org.log_decision("plan", self._name(pid), "approved", why, ref=f"project:{pid}")
        return n

    def cancel(self, pid: int, why: str = "cancelled by you") -> int:
        n = 0
        for t in self.s.q(f"SELECT id FROM tasks WHERE project_id=? AND status IN ({','.join('?' * len(OPEN))})",
                          (pid, *OPEN)):
            if self.org.cancel(t["id"]):
                n += 1
        self.set(pid, status="cancelled", error=why)
        self.org.log_decision("project", self._name(pid), "cancelled", why, ref=f"project:{pid}")
        return n

    def _name(self, pid: int) -> str:
        p = self.s.one("SELECT title FROM projects WHERE id=?", (pid,))
        return f"#{pid} {p['title']}" if p else f"#{pid}"

    # ---------- the owner's review ----------
    def accept(self, pid: int, why: str = "") -> list[str]:
        """Accept the result. Its code changes merge now: accepting is the owner's one click for the whole result.
        Returns what happened to each change (a failed merge keeps the branch and says why)."""
        notes, failed = [], False
        for c in self.s.q("SELECT id, source FROM changes WHERE project_id=? AND status='ready'", (pid,)):
            try:
                notes.append(f"{c['source']}: {self.org.changes.merge(c['id'])}")
            except Exception as e:                     # conflicts etc.: the result stays open, nothing is lost
                failed = True
                notes.append(f"{c['source']}: not merged. {e}"[:600])
        if failed:
            return notes
        self.set(pid, status="done", final=1)
        self.s.x("UPDATE tasks SET final=1 WHERE project_id=? AND status IN ('done','cached')", (pid,))
        self.org.log_decision("deliverable", self._name(pid), "accepted", why, ref=f"project:{pid}")
        return notes

    def discard(self, pid: int, why: str = "discarded by you") -> None:
        """Not wanted: the result is closed and its unmerged code changes are thrown away."""
        for c in self.s.q("SELECT id FROM changes WHERE project_id=? AND status IN ('ready','open')", (pid,)):
            try:
                self.org.changes.discard(c["id"], why)
            except Exception:
                log.exception("discard change %s", c["id"])
        self.set(pid, status="cancelled", error=why)
        self.org.log_decision("deliverable", self._name(pid), "discarded", why, ref=f"project:{pid}")

    def expire(self) -> int:
        """Results left unopened for REVIEW_DAYS close by themselves, so Needs-you never piles up.
        Plain results count as accepted; code is never merged without the owner: its branch is discarded."""
        n = 0
        for p in self.s.q("SELECT id FROM projects WHERE status='review' AND updated<?",
                          (time.time() - REVIEW_DAYS * 86400,)):
            code = self.s.one("SELECT COUNT(*) n FROM changes WHERE project_id=? AND status IN ('ready','open')",
                              (p["id"],))["n"]
            if code:
                self.discard(p["id"], f"not reviewed in {REVIEW_DAYS} days; code changes were not merged")
            else:
                self.set(p["id"], status="done")
                self.org.log_decision("deliverable", self._name(p["id"]), "closed",
                                      f"not reviewed in {REVIEW_DAYS} days", ref=f"project:{p['id']}")
            n += 1
        return n

    def revise(self, pid: int, notes: str, tid: int | None = None) -> int:
        """Rerun one deliverable (default: the last one) with the owner's notes and the previous version."""
        notes = notes.strip()
        if not notes:
            raise ValueError("say what to change")
        t = self.s.task(tid) if tid else self.s.one(
            "SELECT * FROM tasks WHERE project_id=? AND status IN ('done','cached') AND dept!='ceo' "
            "ORDER BY id DESC LIMIT 1", (pid,))
        if not t or t.get("project_id") != pid:
            raise KeyError("nothing to revise in this project yet")
        base = re.split(r"\n\nREVISION NOTES FROM THE OWNER:", t["body"])[0]
        body = (f"{base}\n\nREVISION NOTES FROM THE OWNER:\n{notes}\n\n"
                f"PREVIOUS VERSION (improve it; keep what the notes don't change):\n{(t['result'] or '')[:6000]}")
        new = self.org.submit(t["dept"], body, member=t["member"], source="ceo", priority=1, owner=True,
                              project_id=pid, kind="deliverable", tier=t["tier"])
        self.set(pid, status="running", final=0)
        self.org.log_decision("revision", self._name(pid), "revise", notes, ref=f"task:{t['id']}")
        return new

    # ---------- progress ----------
    def on_task_end(self, tid: int, status: str) -> None:
        t = self.s.task(tid)
        if not t or not t.get("project_id"):
            return
        pid = t["project_id"]
        try:
            self.write_board(pid)
        except OSError:
            log.exception("board for project %s", pid)
        p = self.s.one("SELECT status FROM projects WHERE id=?", (pid,))
        if not p or p["status"] not in ("running",):
            return
        left = self.s.one(f"SELECT COUNT(*) n FROM tasks WHERE project_id=? AND dept!='ceo' AND status IN "
                          f"({','.join('?' * len(WORKING))})", (pid, *WORKING))["n"]
        if left == 0:
            try:
                self.org.pool.submit(self.finish, pid)
            except RuntimeError:                         # pool already shut down (tests, CLI exit): finish here
                self.finish(pid)

    def retry_waiting(self) -> int:
        """Commands that hit a limit plan themselves as soon as the budget allows (oldest first, one at a time)."""
        if not self.org.budget.check(auto=False)[0]:
            return 0
        p = self.s.one("SELECT id, command, plan FROM projects WHERE status='waiting' ORDER BY id LIMIT 1")
        if not p or not self.s.xc("UPDATE projects SET status='planning', error=NULL, updated=? WHERE id=? "
                                  "AND status='waiting'", (time.time(), p["id"])):
            return 0
        from .pipeline import plan_and_dispatch
        r = json.loads(p["plan"]) if p.get("plan") else {}
        r = r if isinstance(r, dict) else {}
        self.org.pool.submit(plan_and_dispatch, self.org, p["command"], r.get("forced"), p["id"], r.get("decision"),
                             r.get("gate", True), bool(r.get("auto")), r.get("source") or "ceo")
        return 1

    def retry(self, pid: int) -> None:
        """Try a command again whose planning failed."""
        if not self.s.xc("UPDATE projects SET status='waiting', error=NULL, updated=? WHERE id=? AND status='failed'",
                         (time.time(), pid)):
            raise ValueError("only a failed command can be tried again")
        self.retry_waiting()

    def sweep(self) -> int:
        """Self-healing: a running project with no work left gets its result (e.g. after a crash or an upgrade)."""
        n = 0
        self.s.x("UPDATE projects SET status='running' WHERE status='combining' AND updated<?", (time.time() - 600,))
        for p in self.s.q("SELECT id FROM projects WHERE status='running'"):
            left = self.s.one(f"SELECT COUNT(*) n FROM tasks WHERE project_id=? AND dept!='ceo' AND status IN "
                              f"({','.join('?' * len(WORKING))})", (p["id"], *WORKING))["n"]
            if left == 0 and self.s.one("SELECT COUNT(*) n FROM tasks WHERE project_id=? AND dept!='ceo'", (p["id"],))["n"]:
                self.finish(p["id"])
                n += 1
        return n

    def finish(self, pid: int) -> None:
        """All work ended: check it against the command (one review call), fix the gaps once, commit the project's
        branches, then write one result for the owner."""
        if not self.s.xc("UPDATE projects SET status='combining', updated=? WHERE id=? AND status='running'",
                         (time.time(), pid)):
            return                                       # someone else is finishing it
        try:
            from . import inbox as _inbox, review
            proj = self.s.one("SELECT command, fix_round FROM projects WHERE id=?", (pid,))
            done = self.s.q("SELECT * FROM tasks WHERE project_id=? AND dept!='ceo' AND status IN ('done','cached') "
                            "AND COALESCE(kind,'') != 'question' ORDER BY id", (pid,))
            failed = self.s.q("SELECT id, dept, title, error FROM tasks WHERE project_id=? AND dept!='ceo' AND "
                              "status='failed'", (pid,))
            if not done:
                self.org.changes.finish(pid)
                self.set(pid, status="failed", error="; ".join(f"{e['title'][:40]}: {_inbox.plain_error(e['error'])[:100]}"
                                                               for e in failed[:3]) or "nothing was delivered")
                return
            tests = [{"source": c["source"], **self.org.changes.run_tests(c["source"], c["worktree"])}
                     for c in self.s.q("SELECT * FROM changes WHERE project_id=? AND status='open'", (pid,))
                     if self.org.changes.test_command(c["source"])]
            rv = review.review(self.org, pid, tests)
            tokens = (rv or {}).get("tokens", 0)
            if rv:
                self.set(pid, review=json.dumps(rv))
            fixes = [g for g in (rv or {}).get("gaps", []) if g["dept"] in self.org.c.depts]
            if fixes and not proj["fix_round"]:          # one automatic round to close the gaps, then report
                ids, last = [], {}                       # same team, same working copy: one fix after another
                for g in fixes:
                    last[g["dept"]] = self.org.submit(g["dept"], g["task"], source="ceo", priority=1, owner=True,
                                                      project_id=pid, kind="deliverable",
                                                      depends_on=last.get(g["dept"]))
                    ids.append(last[g["dept"]])
                self.set(pid, status="running", fix_round=1, tokens=tokens)
                self.org.log_decision("review", self._name(pid), f"{len(ids)} fix task(s)",
                                      "; ".join(g["need"][:60] for g in fixes), ref=f"project:{pid}")
                return
            changes = self.org.changes.finish(pid)
            # the latest version of each deliverable (a revision replaces the one it revised)
            latest: dict[str, dict] = {}
            for t in done:
                key = re.split(r"\n\nREVISION NOTES FROM THE OWNER:", t["body"])[0][:200]
                latest[key] = t
            parts = list(latest.values())
            head = "\n\n".join([
                f"OWNER ASKED:\n{proj['command']}",
                f"ACCEPTANCE:\n{review.table(rv)}",
                "FAILED:\n" + ("\n".join(f"- #{f['id']} {f['dept']}: {f['title'][:90]} :: {_inbox.plain_error(f['error'])}"
                                         for f in failed) or "none"),
                "CHANGES:\n" + ("\n".join(f"- {c['source']}: {c['files']} file(s) on branch {c['branch']}, waiting for "
                                          f"your merge" for c in changes if c.get("files")) or "none"),
                "TESTS:\n" + ("\n".join(f"- {t['source']}: {'passed' if t['ok'] else 'failed'}"
                                        + (f" ({t['hint']})" if t.get("hint") else "") for t in tests) or "none set"),
            ])
            body = head + "\n\nDELIVERABLES:\n" + "\n\n".join(
                f"### {t['dept']}: {t['title']}\n{(t['result'] or '')[:COMBINE_CHARS]}" for t in parts)
            result = body
            if self.org.exe and self.org.budget.check(auto=False)[0]:
                res = self.org.call(body, model=self.org.c.models[2], system=COMBINE_PROMPT, dept="_project",
                                    task_id=None, auto=False)
                if res.ok and res.text.strip():
                    result, tokens = self.org.env.redact(res.text.strip()), tokens + res.tokens
            self.set(pid, status="review", result=result, tokens=tokens)
        except Exception as e:
            log.exception("finishing project %s", pid)
            self.set(pid, status="review", error=f"couldn't combine results: {e}"[:300])

    # ---------- the board ----------
    def board_path(self, pid: int) -> Path:
        return self.org.c.shared_dir / "projects" / str(pid) / "board.md"

    def write_board(self, pid: int) -> str:
        p = self.s.one("SELECT title, command FROM projects WHERE id=?", (pid,))
        if not p:
            return ""
        lines = [f"# Project #{pid}: {p['title']}", f"Goal: {p['command'][:300]}"]
        dec = self.s.q("SELECT kind, choice, why FROM decisions WHERE ref=? ORDER BY id DESC LIMIT 5", (f"project:{pid}",))
        if dec:
            lines.append("Decisions: " + "; ".join(f"{d['kind']} {d['choice']}" + (f" ({d['why'][:80]})" if d["why"] else "")
                                                  for d in dec))
        qs = self.s.q("SELECT from_dept, to_dept, question, status FROM messages WHERE project_id=? ORDER BY id DESC "
                      "LIMIT 5", (pid,))
        if qs:
            lines.append("Questions between teams:")
            lines += [f"- {q['from_dept']} → {q['to_dept']}: {q['question'][:140]} ({q['status']})" for q in qs]
        files = []
        for t in self.s.q("SELECT id, dept, files FROM tasks WHERE project_id=? AND files IS NOT NULL ORDER BY id", (pid,)):
            for f in json.loads(t["files"] or "[]"):
                files.append(f"{f} (#{t['id']} {t['dept']})")
        if files:
            lines.append("Files written so far in this project (build on these; don't start over):")
            lines += [f"- {f}" for f in files[-30:]]
        lines.append("Latest from each team:")
        for t in self.s.q("SELECT dept, status, summary, done_line, output_path FROM tasks WHERE id IN (SELECT MAX(id) "
                          "FROM tasks WHERE project_id=? AND dept!='ceo' GROUP BY dept)", (pid,)):
            s = (t["summary"] or t["done_line"] or "").replace("\n", " · ")
            lines.append(f"- {t['dept']} [{t['status']}]: {s[:300]}" + (f" (file: {t['output_path']})" if t["output_path"] else ""))
        text = "\n".join(lines)[:BOARD_CHARS]
        path = self.board_path(pid)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")
        return text

    def board(self, pid: int) -> str:
        p = self.board_path(pid)
        return p.read_text(encoding="utf-8")[:BOARD_CHARS] if p.exists() else ""
