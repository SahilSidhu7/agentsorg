"""Conference room: critical decisions are debated before anything that depends on them runs.

Seats: the heads of the departments involved (max 2) + a Skeptic. Round 1 in parallel; round 2 (rebuttals that see
only the other seats' round-1 answers) is skipped when everyone already agrees. A Chair writes the decision memo
with 2-3 options. Hard caps: seats x rounds + chair calls, a token ceiling, a budget check before each round.
The owner then approves, picks another option (the work is re-planned for it), or asks a follow-up question.
Every outcome lands in the decision log and as a `decision` fact in the KB.
"""
from __future__ import annotations

import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger("agentsorg")
POS_RE = re.compile(r"POSITION:\s*\**\s*(support|concerns|oppose|alternative)", re.I)
SEAT_CHARS = 1200
DEFAULT_TRIGGERS = {"planner_marks_critical": True, "plan_over_tasks": 5, "opus_in_plan": True, "new_department": True,
                    "keywords": ["pricing", "price", "positioning", "target customer", "rebrand", "campaign", "delete",
                                 "overwrite", "shut down", "pivot"]}


def decision_text(label: str, note: str, memo: str | None) -> str:
    """What the planner gets after a meeting: the choice, the owner's note, and the memo that explains it."""
    return label + (f". {note}" if note else "") + (f"\nTHE MEMO FROM THE MEETING:\n{memo[:2500]}" if memo else "")


class Conference:
    def __init__(self, org):
        self.org = org
        self.s = org.s

    # ---------- settings ----------
    def _set(self, key, default):
        return self.org.mods.setting("conference", key, default)

    def enabled(self) -> bool:
        m = self.org.mods.all().get("conference")
        return bool(m and m.enabled and not m.error)

    def rules(self) -> str:
        m = self.org.mods.all().get("conference")
        return m.system_text() if m else ""

    # ---------- when ----------
    def is_critical(self, command: str, reply: dict, plan: list[dict], actions: list) -> str:
        """Why this plan must be debated first ('' = it needn't)."""
        if not self.enabled() or not plan:
            return ""
        tr = {**DEFAULT_TRIGGERS, **(self._set("triggers", {}) or {})}
        if tr.get("planner_marks_critical") and reply.get("critical"):
            return str(reply.get("critical_reason") or "the planner marked it critical")[:200]
        if tr.get("plan_over_tasks") and len(plan) > int(tr["plan_over_tasks"]):
            return f"a big plan ({len(plan)} tasks)"
        if tr.get("opus_in_plan") and any(p.get("tier") == 3 for p in plan):
            return "it needs the most expensive model"
        if tr.get("new_department") and any(str(a).startswith("? proposed new department") for a in actions):
            return "it adds a department"
        low = command.lower()
        for k in tr.get("keywords") or []:
            if re.search(rf"\b{re.escape(str(k).lower())}\b", low):
                return f"it's about {k}"
        return ""

    # ---------- seats ----------
    def seats_for(self, depts: list[str]) -> list[dict]:
        n = max(2, min(int(self._set("seats", 3) or 3), 4))
        out = []
        for did in [d for d in dict.fromkeys(depts) if d in self.org.c.depts][: n - 1]:
            d = self.org.c.depts[did]
            head = d.members[0]
            out.append({"name": f"{d.name} ({head.name})", "dept": did, "member": head.id,
                        "tier": 2 if head.max_tier >= 2 else 1})
        if not out:                               # nobody named: the first department speaks for the company
            d = next(iter(self.org.c.depts.values()))
            out.append({"name": f"{d.name} ({d.members[0].name})", "dept": d.id, "member": d.members[0].id, "tier": 1})
        out.append({"name": "Skeptic", "dept": "", "member": "", "tier": 1})
        return out

    def _guess_depts(self, text: str) -> list[str]:
        low = text.lower()
        hits = [d.id for d in self.org.c.depts.values() if d.id in low or d.name.lower() in low]
        return hits or list(self.org.c.depts)[:2]

    # ---------- start ----------
    def convene(self, question: str, *, project_id: int | None = None, depts: list[str] | None = None,
                plan: list[dict] | None = None, options: list[str] | None = None, why: str = "",
                background: bool = True) -> int:
        seats = self.seats_for(depts or self._guess_depts(question))
        now = time.time()
        cid = self.s.x("INSERT INTO conferences(created,updated,project_id,question,seats,rounds,options,status,error) "
                       "VALUES(?,?,?,?,?,?,?,?,?)",
                       (now, now, project_id, question[:2000], json.dumps(seats), json.dumps([]),
                        json.dumps({"options": options or [], "plan": plan or [], "why": why}), "running", None))
        if project_id:
            self.org.projects.set(project_id, status="meeting", critical=1, conference_id=cid)
        if background:
            self.org.pool.submit(self._guarded, cid)
        else:
            self._guarded(cid)
        return cid

    def _guarded(self, cid: int, follow_up: str = "") -> None:
        try:
            self.run(cid, follow_up)
        except Exception as e:
            log.exception("conference %s", cid)
            self._update(cid, status="incomplete", error=f"{type(e).__name__}: {e}"[:300])

    def get(self, cid: int) -> dict | None:
        c = self.s.one("SELECT * FROM conferences WHERE id=?", (cid,))
        if not c:
            return None
        for k in ("seats", "rounds", "options"):
            c[k] = json.loads(c[k]) if c.get(k) else ([] if k != "options" else {})
        return c

    def list(self, limit: int = 30) -> list[dict]:
        return self.s.q("SELECT id,created,updated,project_id,question,status,choice,tokens,calls FROM conferences "
                        "ORDER BY id DESC LIMIT ?", (max(1, min(limit, 100)),))

    def _update(self, cid: int, **f) -> None:
        f["updated"] = time.time()
        for k in ("seats", "rounds", "options"):
            if k in f and not isinstance(f[k], str):
                f[k] = json.dumps(f[k])
        self.s.x(f"UPDATE conferences SET {','.join(k + '=?' for k in f)} WHERE id=?", (*f.values(), cid))

    # ---------- the meeting ----------
    def _brief(self, c: dict) -> str:
        meta = c["options"] or {}
        parts = [f"THE OWNER'S COMMAND (decided: debate how to deliver it, not whether):\n{c['question']}"]
        if meta.get("why"):
            parts.append(f"WHY THIS IS A MEETING: {meta['why']}")
        if meta.get("plan"):
            parts.append("PLAN UNDER DISCUSSION:\n" + "\n".join(
                f"{i}. [{p.get('dept')}] {str(p.get('task'))[:300]}" for i, p in enumerate(meta["plan"])))
        if meta.get("options"):
            parts.append("OPTIONS SO FAR:\n" + "\n".join(f"- {o}" for o in meta["options"]))
        parts.append(f"FACTS:\n{self.org.company_facts()}")
        if kb := self.org.kb.context(c["question"]):
            parts.append(f"KB:\n{kb}")
        return "\n\n".join(parts)

    def _seat_call(self, seat: dict, prompt: str) -> dict:
        role = ("You are the Skeptic in the company's conference room." if not seat["dept"] else
                f"You are {seat['name']}, head of the {self.org.c.depts[seat['dept']].name} department "
                f"({self.org.c.depts[seat['dept']].mission}) in the company's conference room.")
        ctx = self.org.intake.system_text(seat["dept"]) if seat["dept"] else self.org.intake.system_text("")
        system = "\n\n".join(x for x in (role, self.org.c.goals_text(), self.rules(), ctx) if x)
        res = self.org.call(prompt, model=self.org.c.models[seat["tier"]], system=system, dept="_conference",
                            task_id=None, auto=False)
        text = self.org.env.redact(res.text.strip())[:SEAT_CHARS] if res.ok else ""
        m = POS_RE.search(text)
        return {"seat": seat["name"], "text": text, "position": m.group(1).lower() if m else "",
                "ok": res.ok, "error": res.error[:200] if not res.ok else "", "tokens": res.tokens}

    def _round(self, seats: list[dict], prompt_for) -> list[dict]:
        with ThreadPoolExecutor(max_workers=len(seats)) as ex:
            return list(ex.map(lambda s: self._seat_call(s, prompt_for(s)), seats))

    def run(self, cid: int, follow_up: str = "") -> None:
        c = self.get(cid)
        seats, rounds = c["seats"], list(c["rounds"])
        tokens, calls = c["tokens"] or 0, c["calls"] or 0
        ceiling = int(self._set("token_ceiling", 60000) or 60000)
        brief = self._brief(c)

        def budget_ok() -> str:
            ok, why = self.org.budget.check(auto=False)
            if not ok:
                return why
            return "" if tokens < ceiling else f"the meeting reached its {ceiling:,}-token ceiling"

        stop = ""
        if follow_up:                                    # the owner asked a question: one more round, then the memo
            if not (stop := budget_ok()):
                prev = "\n".join(f"- {r['seat']}: {r['text'][:400]}" for r in (rounds[-1]["answers"] if rounds else []))
                ans = self._round(seats, lambda s: f"{brief}\n\nEARLIER POSITIONS:\n{prev}\n\nTHE OWNER ASKS:\n{follow_up}")
                rounds.append({"round": f"owner: {follow_up[:200]}", "answers": ans})
                tokens += sum(a["tokens"] for a in ans)
                calls += len(ans)
        else:
            if not (stop := budget_ok()):
                ans = self._round(seats, lambda s: f"{brief}\n\nRound 1: give your position.")
                rounds.append({"round": "1", "answers": ans})
                tokens += sum(a["tokens"] for a in ans)
                calls += len(ans)
                self._update(cid, rounds=rounds, tokens=tokens, calls=calls)
                positions = {a["position"] for a in ans if a["ok"]}
                agree = len(positions) == 1 and "" not in positions and all(a["ok"] for a in ans)
                if not agree and int(self._set("rounds", 2) or 2) >= 2 and not (stop := budget_ok()):
                    def rebut(s):
                        others = "\n".join(f"- {a['seat']}: {a['text'][:500]}" for a in ans if a["seat"] != s["name"])
                        return f"{brief}\n\nOTHER SEATS (round 1):\n{others}\n\nRound 2: rebut their strongest point."
                    ans2 = self._round(seats, rebut)
                    rounds.append({"round": "2", "answers": ans2})
                    tokens += sum(a["tokens"] for a in ans2)
                    calls += len(ans2)
        self._update(cid, rounds=rounds, tokens=tokens, calls=calls)
        if not rounds or not any(a["ok"] for r in rounds for a in r["answers"]):
            self._update(cid, status="incomplete", memo=f"The meeting couldn't start: {stop or 'no seat could answer'}.",
                         error=stop or "no answers")
            self._after_ready(cid)
            return
        memo, opts, rec, planned = "", [], 0, None
        if not (stop2 := budget_ok()):
            transcript = "\n\n".join(f"ROUND {r['round']}\n" + "\n".join(f"[{a['seat']}]\n{a['text']}" for a in r["answers"])
                                     for r in rounds)
            res = self.org.call(f"{brief}\n\nDEBATE:\n{transcript}\n\nWrite the decision memo.",
                                model=self.org.c.models[int(self._set("chair_tier", 2) or 2)],
                                system="You are the Chair of the company's conference room.\n\n" + self.rules(),
                                dept="_conference", task_id=None, auto=False)
            calls += 1
            tokens += res.tokens
            if res.ok:
                memo, opts, rec = self._parse_memo(self.org.env.redact(res.text))
                planned = self._as_planned(res.text, len(opts))
        meta = c["options"] or {}
        meta["options"] = opts or meta.get("options") or []
        meta["recommend"] = rec
        meta["as_planned"] = planned
        incomplete = stop or stop2 or not memo
        if not memo:
            memo = ("The Chair couldn't write the memo" + (f" ({stop or stop2})" if stop or stop2 else "") +
                    ". Positions so far:\n\n" + "\n\n".join(f"**{a['seat']}**: {a['text']}" for a in rounds[-1]["answers"]))
        self._update(cid, memo=memo, options=meta, tokens=tokens, calls=calls,
                     status="incomplete" if incomplete else "ready", error=(stop or stop2) or None)
        self._after_ready(cid)

    @staticmethod
    def _parse_memo(text: str) -> tuple[str, list[str], int]:
        opts, rec = [], 0
        m = re.search(r"^\s*OPTIONS:\s*(\[.*\])\s*$", text, re.M)
        if m:
            try:
                opts = [str(o)[:160] for o in json.loads(m.group(1)) if str(o).strip()][:4]
            except json.JSONDecodeError:
                opts = []
        r = re.search(r"^\s*RECOMMEND:\s*(\d+)", text, re.M)
        if r:
            rec = min(int(r.group(1)), max(len(opts) - 1, 0))
        memo = re.sub(r"^\s*(OPTIONS|RECOMMEND|AS_PLANNED):.*$", "", text, flags=re.M).strip()
        return memo, opts, rec

    @staticmethod
    def _as_planned(text: str, n: int) -> int | None:
        """The option the Chair says IS the plan as proposed: choosing it releases the held tasks, no re-plan."""
        m = re.search(r"^\s*AS_PLANNED:\s*(\d+)", text or "", re.M)
        return int(m.group(1)) if m and int(m.group(1)) < n else None

    def _after_ready(self, cid: int) -> None:
        c = self.get(cid)
        if c and c["project_id"]:
            self.org.projects.set(c["project_id"], status="meeting")
        notify = getattr(self.org, "notify", None)
        if notify:
            notify.urgent(f"Decision needed: {c['question'][:120]}", f"The conference room has a memo for you. "
                          f"Open the Inbox to approve or pick another option.")

    # ---------- the owner decides ----------
    def decide(self, cid: int, choice: int | None = None, note: str = "") -> dict:
        """choice None = approve the plan as proposed; an option index = go with that option (re-planned)."""
        c = self.get(cid)
        if not c:
            raise KeyError("no such meeting")
        if c["status"] == "running":
            raise ValueError("the meeting is still running")
        if c["status"] == "decided":
            raise ValueError("already decided")
        meta = c["options"] or {}
        opts = meta.get("options") or []
        if choice is not None and not (0 <= choice < len(opts)):
            raise ValueError("no such option")
        label = "the plan as proposed" if choice is None else opts[choice]
        self._update(cid, status="decided", choice=label)
        why = (note or "") + (f" (after the conference room; memo #{cid})")
        did = self.org.log_decision("conference", c["question"][:120], label, why.strip(),
                                    ref=f"project:{c['project_id']}" if c["project_id"] else f"conference:{cid}")
        if c["project_id"]:                          # the newest choice on this project replaces older ones
            older = [d["id"] for d in self.s.q("SELECT id FROM decisions WHERE ref=? AND id<? AND superseded_by IS NULL "
                                                "AND kind IN ('conference','plan')", (f"project:{c['project_id']}", did))]
            self.org.supersede(older, did, f"the owner chose: {label}")
        self.org.kb.add(f"Decision: {c['question'][:80]}", f"Chose: {label}. {note}".strip(), tags="decision")
        out = {"decided": label}
        pid = c["project_id"]
        if pid:
            if choice is None or choice == meta.get("as_planned"):   # the plan as it was debated: no re-plan
                if note.strip():                         # the owner's conditions travel with the held work
                    for t in self.s.q("SELECT id, body FROM tasks WHERE project_id=? AND status='held'", (pid,)):
                        self.s.x("UPDATE tasks SET body=? WHERE id=?",
                                 (f"{t['body']}\n\nOWNER'S DECISION: {label}. {note.strip()}", t["id"]))
                out["started"] = self.org.projects.release(pid)
            else:                                        # an option (even the recommended one) is not the held plan:
                for t in self.s.q("SELECT id FROM tasks WHERE project_id=? AND status='held'", (pid,)):
                    self.org.cancel(t["id"])             # drop it and plan again for what was chosen
                p = self.s.one("SELECT command FROM projects WHERE id=?", (pid,))
                from .pipeline import plan_and_dispatch
                out.update(plan_and_dispatch(self.org, p["command"], None, project_id=pid,
                                             decision=decision_text(label, note, c.get("memo")), gate=False))
        return out

    def follow_up(self, cid: int, question: str) -> None:
        c = self.get(cid)
        if not c:
            raise KeyError("no such meeting")
        if c["status"] not in ("ready", "incomplete"):
            raise ValueError("ask once the memo is ready")
        if not question.strip():
            raise ValueError("ask a question")
        self._update(cid, status="running")
        self.org.pool.submit(self._guarded, cid, question.strip()[:1000])
