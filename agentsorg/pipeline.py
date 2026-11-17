"""The CEO pipeline: route (0 tokens) -> plan (one stronger-model call) -> dispatch (at most MAX_STEPS tasks).

No approval gates and no meetings: the owner asked for it, so it runs. What can't be undone (sending, merging,
deploying, spending) is still gated where it happens: the Outbox, the merge button, the owner's keys.
"""
from __future__ import annotations

import json
import logging
import re
import time

from . import router
from .budget import is_limit

log = logging.getLogger("agentsorg")
MULTI = re.compile(r"\b(and|then|plan|strategy|launch|roadmap|across|campaign|project|compare|research and|"
                   r"set up|figure out|end[- ]to[- ]end)\b|;|\n", re.I)
MAX_STEPS = 3            # one workflow at a time: a bigger plan is cut to its first steps
MAX_DIRECT_CHARS = 240
# keyword -> department: a short command matching exactly one department's words goes straight there (0 tokens)
ROUTES = {"invoice": "finance", "bookkeeping": "finance", "tagline": "marketing", "headline": "marketing",
          "blog post": "marketing", "bug": "engineering", "deploy": "engineering"}
ANSWERS = re.compile(r"^\s*(status|what('?s| is) (running|the status|happening)|show (me )?(the )?(goals|status|"
                     r"services)|what are (our|the) goals)\s*\??\s*$", re.I)
PLAN_FORMAT = """Output ONLY one JSON object (at most 3 tasks; one is best when one team can do it):
{"answer":"<1-3 short sentences for the owner, or empty>","actions":[...],"supersedes":[<decision ids, optional>],
 "tasks":[{"dept":"<id>","member":"<id>","tier":1|2|3,"task":"<self-contained instruction>",
           "after":<index of the task it needs, optional>,"expects":"doc|email|post|code|research|plan|data",
           "parallel_safe":false}]}"""


# ---------- route ----------
def default_route(org, text: str) -> dict:
    m = re.match(r"^@(\w+)\s+(.+)$", text, re.S)
    if m and m.group(1) in org.c.depts:
        return {"dept": m.group(1), "task": m.group(2)}
    if ANSWERS.match(text):
        return {"answer": status_answer(org)}
    if len(text) > MAX_DIRECT_CHARS or MULTI.search(text):
        return {"plan": True}
    low = text.lower()
    named = {d.id for d in org.c.depts.values()
             if re.search(rf"\b({re.escape(d.id)}|{re.escape(d.name.lower())})\b", low)}
    hits = {dept for word, dept in ROUTES.items() if dept in org.c.depts and re.search(rf"\b{re.escape(word)}\b", low)}
    cands = named | hits
    if len(cands) == 1:
        return {"dept": cands.pop(), "task": text}
    return {"plan": True}


def status_answer(org) -> str:
    n = lambda sql, a=(): org.s.one(sql, a)["n"]
    running = n("SELECT COUNT(*) n FROM tasks WHERE status IN ('running','queued','waiting') AND dept!='ceo'")
    review = n("SELECT COUNT(*) n FROM projects WHERE status='review'")
    parts = [f"{running} task(s) in progress", f"{review} result(s) to review"]
    if org.c.goals:
        parts.append("goals: " + "; ".join(org.c.goals[:3]))
    return ". ".join(parts) + "."


def handle(org, text: str) -> dict:
    forced, clean = router.forced_tier(text)
    suffix = f" !t{forced}" if forced else ""
    r, by = default_route(org, clean), "router"
    if r.get("answer"):
        return {"tasks": [], "routing": by, "tokens": 0, "answer": str(r["answer"])}
    if r.get("dept") in org.c.depts:
        pid = org.projects.create(clean, status="running")
        tid = org.submit(r["dept"], str(r.get("task") or clean) + suffix, member=r.get("member"), source="ceo",
                         priority=1, owner=True, project_id=pid, kind="deliverable")
        from . import activity
        activity.record(org, "ceo", "understood", f"sent straight to {r['dept']} (no planning call)", project=pid,
                        asked=clean[:400], handed_out=[f"{r['dept']}: {str(r.get('task') or clean)[:160]}"], routing=by)
        return {"tasks": [tid], "routing": "direct", "tokens": 0, "project": pid, "answer": "", "actions": []}
    return plan_and_dispatch(org, clean, forced)


# ---------- plan ----------
def planner_context(org, command: str) -> str:
    chars = 5000
    parts = [f"FACTS:\n{org.company_facts()}", f"ROSTER:\n{org.c.roster()}"]
    if pack := org.intake.design_brief():
        parts.append(f"PROJECT CONTEXT:\n{pack[:chars]}")
    return "\n\n".join(parts)


def parse_reply(text: str) -> dict:
    m = re.search(r"\{.*\}|\[.*\]", text or "", re.S)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}
    if isinstance(data, list):
        return {"tasks": data}
    return data if isinstance(data, dict) else {}


def plan_and_dispatch(org, command: str, forced: int | None, project_id: int | None = None,
                      decision: str | None = None, gate: bool = True, auto: bool = False,
                      source: str = "ceo") -> dict:
    """Plan the owner's command and start it. `gate` and `auto` are kept for callers of older versions."""
    from .agents import CEO_OPS, log as _l, plan_item  # noqa: F401
    ok, why = org.budget.check(auto=auto, scheduled=auto)
    pid = project_id or org.projects.create(command)
    resume = {"forced": forced, "decision": decision, "gate": gate, "auto": auto, "source": source}
    if not ok:                                   # over budget: the command waits and starts by itself
        return _wait(org, pid, resume, why)
    org.projects.set(pid, status="planning")
    parent = org.s.add_task(dept="ceo", member="ceo", body=command, source="ceo", status="routing", tier=1,
                            model=org.c.models[2], worker=org.worker, lease_until=time.time() + 300, project_id=pid)
    res, reply, plan, answer, done, ids, err = None, {}, [], "", [], [], ""
    tokens, waiting = 0, False
    try:
        ctx = planner_context(org, command)
        cmd_text = command + (f"\n\nDECISION ALREADY MADE BY THE OWNER: {decision}\nPlan for that." if decision else "")
        tier = forced or 2
        model = org.c.models.get(tier) or org.c.models[2]
        res = org.call(f"{ctx}\n\nCOMMAND:\n{cmd_text}", model=model,
                       system=org._read(org.c.prompts_dir / "ceo.md") + "\n\n" + CEO_OPS + "\n" + PLAN_FORMAT,
                       dept="ceo", task_id=parent, auto=auto, cwd=org.home)
        tokens = res.tokens
        if not res.ok and is_limit(res.error):
            org.budget.note_limit(res.error)
            waiting = True
            return _wait(org, pid, resume, res.error[:200])
        if not res.ok:
            err = f"The planner couldn't run: {res.error[:200]}"
            return {"tasks": [], "error": err, "tokens": tokens, "project": pid}
        reply = parse_reply(res.text)
        raw_tasks = reply.get("tasks") if isinstance(reply.get("tasks"), list) else []
        plan = [p for p in (plan_item(x) for x in raw_tasks[:8]) if p]
        if len(plan) > MAX_STEPS:                 # keep the first steps; drop links to the steps that were cut
            plan = [{**p, "after": p["after"] if p.get("after") is not None and p["after"] < MAX_STEPS else None}
                    for p in plan[:MAX_STEPS]]
        answer = str(reply.get("answer") or "").strip()
        done = [org.run_action(a) for a in (reply.get("actions") or [])[:6] if isinstance(a, dict)]
        if sup := [x for x in (reply.get("supersedes") or []) if isinstance(x, (int, str))]:
            did = org.log_decision("command", command[:120], "replaces earlier decision(s)",
                                   ", ".join(f"#{x}" for x in sup), ref=f"project:{pid}")
            if n := org.supersede(sup, did, f"replaced by the command: {command[:160]}", kinds=PLAN_KINDS):
                done.append(f"✓ replaced {n} earlier decision(s) with this command")
        if not plan and not answer and not done:
            err = "The planner's reply had no tasks or answer. Try rephrasing the command."
            return {"tasks": [], "error": err, "tokens": tokens, "project": pid}
        if not plan:
            org.projects.set(pid, status="done", answer=answer, result=answer or "; ".join(done))
            return {"tasks": [], "routing": "ceo", "tokens": tokens, "answer": answer, "actions": done, "project": pid}
        specs = plan
        suffix = f" !t{forced}" if forced else ""
        ids = dispatch(org, specs, pid, parent, suffix, forced, False, done, source)
        org.projects.set(pid, plan=specs, answer=answer, status="running")
        return {"tasks": ids, "routing": "ceo", "tokens": tokens, "plan": specs, "answer": answer, "actions": done,
                "project": pid}
    except Exception as e:
        log.exception("planning failed")
        err = f"Planning failed: {type(e).__name__}: {e}"[:300]
        return {"tasks": ids, "error": err, "project": pid}
    finally:
        ok_run = not err
        org.s.update_task(parent, status="cancelled" if waiting else "done" if ok_run else "failed", worker=None, lease_until=None, tokens=tokens,
                          result=json.dumps({"answer": answer, "actions": done, "tasks": plan}),
                          done_line=(f"planned {len(ids)} task(s)" + (f" · {'; '.join(x for x in [answer, *done] if x)}"
                                                                     if answer or done else ""))[:200],
                          error=err or None)
        if err:
            org.projects.set(pid, status="failed", error=err)
        from . import activity
        activity.record(org, "ceo", "waiting for plan usage" if waiting else "plan failed" if err else "understood",
                        err or answer, task=parent, project=pid, asked=command[:400], tokens=tokens, source=source,
                        handed_out=[f"{p['dept']}: {str(p.get('task'))[:160]}" for p in plan], actions=done)


PLAN_KINDS = ("conference", "plan", "command", "design", "goal", "review", "deliverable", "revision", "project")
# never replaced by a planner: abilities, departments, changes, settings, sends (the owner's own safety calls)

def _wait(org, pid: int, resume: dict, why: str) -> dict:
    org.projects.set(pid, status="waiting", plan=resume, error=why[:300])
    msg = f"Your command is saved and starts by itself when plan usage frees up. ({why[:160]})"
    return {"tasks": [], "held": msg, "project": pid, "routing": "waiting", "answer": "", "actions": []}


def dispatch(org, specs: list[dict], pid: int, parent: int, suffix: str, forced: int | None, hold: bool,
             notes: list, source: str = "ceo") -> list[int]:
    ids: list = []
    last_writer: dict[str, int] = {}             # dept -> index of its latest file-writing task in this plan
    for i, p in enumerate(specs):
        dep = p.get("after")
        dep = dep if dep is not None and 0 <= dep < len(ids) else None
        d = org.c.depts.get(p["dept"])
        writes = bool(d and {"Edit", "Write", "Bash"} & set(d.tools or []))
        if writes and not p.get("parallel_safe") and p["dept"] in last_writer:
            prev = last_writer[p["dept"]]         # one team's builds run in order, each on top of the last
            if dep is None or prev > dep:
                dep = prev
        if writes:
            last_writer[p["dept"]] = i
        dep_id = ids[dep] if dep is not None else None
        if p["dept"] not in org.c.depts:
            notes.append(f"✗ skipped a task for unknown department {p['dept']!r}")
            ids.append(None)
            continue
        ids.append(org.submit(p["dept"], p["task"] + suffix, member=p["member"], tier=forced or p["tier"],
                              source=source, priority=1 if source == "ceo" else 3, parent_id=parent,
                              depends_on=dep_id, owner=source == "ceo", project_id=pid, kind="deliverable", hold=hold))
    real = [i for i in ids if i]
    if len(real) >= 3 and ids[-1] and specs[-1].get("after") is not None:   # a closing step builds on all of it
        org.wait_for_all(ids[-1], real[:-1])
    return real
