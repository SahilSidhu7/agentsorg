"""Live office state for the visual floor + per-member reports. Pure SQL, 0 tokens."""
from __future__ import annotations

import time
from datetime import datetime


RECENT = 90   # seconds a finished task keeps its speech bubble


def _midnight() -> float:
    return datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def office_state(org) -> dict:
    now, day0 = time.time(), _midnight()
    live = org.s.q("SELECT id,dept,member,status,title,done_line,model,updated,parent_id FROM tasks "
                   "WHERE status IN ('running','queued','waiting') OR updated>? ORDER BY id", (now - RECENT,))
    today = {(r["dept"], r["member"]): r for r in org.s.q(
        "SELECT dept, member, SUM(status IN ('done','cached')) done, SUM(status='failed') failed, "
        "SUM(status='backlog') backlog, SUM(tokens) tokens FROM tasks WHERE updated>=? OR status='backlog' "
        "GROUP BY dept, member", (day0,))}
    by_member: dict[tuple, dict] = {}
    for t in live:
        k = (t["dept"], t["member"])
        cur = by_member.setdefault(k, {"state": "idle", "task": None, "last": None})
        if t["status"] == "running":
            cur.update(state="working", task={"id": t["id"], "title": t["title"], "model": t["model"]})
        elif t["status"] in ("queued", "waiting") and cur["state"] == "idle":
            cur.update(state="queued", task={"id": t["id"], "title": t["title"], "model": t["model"]})
        elif t["status"] in ("done", "cached", "failed"):
            cur["last"] = {"id": t["id"], "status": t["status"], "line": t["done_line"] or t["title"], "at": t["updated"]}
    depts = []
    for d in org.c.depts.values():
        members = []
        for i, m in enumerate(d.members):
            k = (d.id, m.id)
            st = today.get(k) or {}
            members.append({"id": m.id, "name": m.name, "role": m.role, "tier": m.tier, "max_tier": m.max_tier,
                            "head": i == 0, **by_member.get(k, {"state": "idle", "task": None, "last": None}),
                            "done": st.get("done") or 0, "failed": st.get("failed") or 0,
                            "backlog": st.get("backlog") or 0, "tokens": st.get("tokens") or 0})
        depts.append({"id": d.id, "name": d.name, "mission": d.mission, "members": members,
                      "tools": bool(d.tools)})
    ceo = by_member.get(("ceo", "ceo"), {"state": "idle", "task": None, "last": None})
    recent_routes = org.s.q("SELECT id, title, result, updated FROM tasks WHERE dept='ceo' AND updated>? "
                            "ORDER BY id DESC LIMIT 3", (now - 20,))
    # dispatches: tasks created in the last 20s by the CEO, for the "memo flying" animation
    dispatches = org.s.q("SELECT id, dept, member, created FROM tasks WHERE parent_id IN "
                         "(SELECT id FROM tasks WHERE dept='ceo') AND created>?", (now - 20,))
    meeting = None
    return {"ceo": ceo, "routes": recent_routes, "dispatches": dispatches, "depts": depts, "now": now,
            "meeting": meeting}


NAMES = {"haiku": "Haiku", "sonnet": "Sonnet", "opus": "Opus"}


def _calls(use: list[dict]) -> str:
    parts = [f"{u['n']} {next((v for k, v in NAMES.items() if k in (u['model'] or '')), 'other')}" for u in use]
    n = sum(u["n"] for u in use)
    return (", ".join(parts) + (" call" if n == 1 else " calls")) if parts else "no calls"


def member_report(org, dept: str, mid: str) -> dict:
    d = org.c.depts[dept]
    m = d.member(mid)
    day0 = _midnight()
    tasks = org.s.q("SELECT id,title,status,model,tokens,done_line,error,updated,source FROM tasks "
                    "WHERE dept=? AND member=? ORDER BY id DESC LIMIT 25", (dept, m.id))
    today = [t for t in tasks if t["updated"] >= day0]
    use = org.s.q("SELECT u.model, COUNT(*) n, SUM(u.input+u.output+u.cache_create) t FROM usage u "
                  "JOIN tasks t ON t.id=u.task_id WHERE t.dept=? AND t.member=? AND u.ts>=? GROUP BY u.model",
                  (dept, m.id, day0))
    lines = [f"✓ {t['done_line'] or t['title']}" for t in today if t["status"] in ("done", "cached")][:4]
    lines += [f"✗ {t['title'][:60]}" for t in today if t["status"] == "failed"][:2]
    backlog = sum(1 for t in tasks if t["status"] == "backlog")
    tok = sum(u["t"] or 0 for u in use)
    lines.append(f"used {tok:,} tokens · {_calls(use)} · backlog {backlog}")
    return {"dept": d.id, "dept_name": d.name, "id": m.id, "name": m.name, "role": m.role, "tier": m.tier,
            "max_tier": m.max_tier, "head": d.members[0].id == m.id, "team": len(d.members),
            "report": lines if len(lines) > 1 else ["idle today", lines[-1]], "tasks": tasks}
