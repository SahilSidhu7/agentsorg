"""End-of-day reports. Deterministic when small (0 tokens); one Haiku call per busy dept otherwise."""
from __future__ import annotations

import time
from datetime import datetime

from .state import today

MAX_BULLETS = 5
SHORT = {"haiku": "H", "sonnet": "S", "opus": "O"}


def _short_model(m: str) -> str:
    for k, v in SHORT.items():
        if k in (m or ""):
            return v
    return "?"


NAMES = {"H": "Haiku", "S": "Sonnet", "O": "Opus"}


def _calls(use: list[dict]) -> str:
    parts = [f"{u['n']} {NAMES.get(_short_model(u['model']), 'other')}" for u in use]
    n = sum(u["n"] for u in use)
    return (", ".join(parts) + (" call" if n == 1 else " calls")) if parts else "no calls"


def _day_bounds(day: str) -> tuple[float, float]:
    start = datetime.strptime(day, "%Y-%m-%d").timestamp()
    return start, start + 86400


def dept_report(org, dept: str, day: str) -> tuple[str, bool]:
    a, b = _day_bounds(day)
    done = org.s.q("SELECT * FROM tasks WHERE dept=? AND status IN ('done','cached') AND updated>=? AND updated<? "
                   "ORDER BY id", (dept, a, b))
    failed = org.s.q("SELECT title FROM tasks WHERE dept=? AND status='failed' AND updated>=? AND updated<?",
                     (dept, a, b))
    backlog = org.s.one("SELECT COUNT(*) n FROM tasks WHERE dept=? AND status='backlog'", (dept,))["n"]
    use = org.s.q("SELECT model, SUM(input+output+cache_create) t, COUNT(*) n FROM usage WHERE dept=? AND day=? "
                  "GROUP BY model", (dept, day))
    stats = f"{_calls(use)} · {backlog} waiting in the backlog"
    if not done and not failed:
        return f"idle. {stats}", False
    lines = [t["done_line"] or t["title"] for t in done]
    llm = False
    # summarising is optional work: only when the scheduled budget allows and nothing is paused
    can_llm = not org.s.get("paused", False) and org.budget.check(auto=True, scheduled=True)[0]
    if len(lines) > MAX_BULLETS - 1 and not can_llm:
        lines = lines[:3] + [f"+{len(lines) - 3} more"]
    elif len(lines) > MAX_BULLETS - 1:
        res = org.call("Compress into max 4 bullets, <=12 words each, no intro:\n" + "\n".join(f"- {l}" for l in lines),
                       model=org.c.models[1], system="You write ultra-terse status bullets.", dept=dept,
                       task_id=None, auto=False)
        if res.ok and res.text.strip():
            lines = [l.lstrip("-• ").strip() for l in res.text.strip().splitlines() if l.strip()][:4]
            llm = True
        else:
            lines = lines[:3] + [f"+{len(lines) - 3} more"]
    out = [f"✓ {l}" for l in lines]
    if failed:
        out.append(f"✗ {len(failed)} failed: " + "; ".join(f["title"][:40] for f in failed[:2]))
    out.append(stats)
    return "\n".join(out[:MAX_BULLETS + 1]), llm


WEEKLY_PROMPT = """Write the company's weekly review for its owner. Plain words, no jargon, under 250 words.
Sections (markdown headings): Wins, Misses against the goals, Risks, Decisions needed (exactly 3, each one line,
most important first). Use only the facts given; say "none" where there is nothing."""
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def weekly_review(org) -> str:
    """One Haiku call over the week's facts (deterministic list when the budget doesn't allow it)."""
    since = time.time() - 7 * 86400
    done = org.s.q("SELECT dept, done_line, title FROM tasks WHERE status IN ('done','cached') AND dept!='ceo' "
                   "AND updated>? ORDER BY id DESC LIMIT 60", (since,))
    failed = org.s.q("SELECT dept, title, error FROM tasks WHERE status='failed' AND dept!='ceo' AND updated>? LIMIT 20",
                     (since,))
    projects = org.s.q("SELECT title, status FROM projects WHERE updated>? ORDER BY id DESC LIMIT 20", (since,))
    dec = org.s.q("SELECT kind, subject, choice FROM decisions WHERE at>? ORDER BY id DESC LIMIT 20", (since,))
    nums = org.metrics.all()
    facts = "\n".join([
        "GOALS: " + ("; ".join(org.c.goals) or "none set"),
        "NUMBERS: " + (" · ".join(f"{org.metrics.fmt(m)} (week ago {m['week_ago']:g})" if m.get("week_ago") is not None
                                  else org.metrics.fmt(m) for m in nums) or "none tracked"),
        f"DONE ({len(done)}):", *[f"- {t['dept']}: {(t['done_line'] or t['title'])[:120]}" for t in done[:40]],
        f"FAILED ({len(failed)}):", *[f"- {t['dept']}: {t['title'][:80]}" for t in failed[:10]],
        "PROJECTS:", *[f"- {p['title'][:80]} [{p['status']}]" for p in projects],
        "DECISIONS:", *[f"- {d['kind']}: {d['subject'][:60]} -> {d['choice'][:60]}" for d in dec],
    ])
    body = ""
    if org.exe and not org.s.get("paused", False) and org.budget.check(auto=True, scheduled=True)[0]:
        res = org.call(facts, model=org.c.models[1], system=WEEKLY_PROMPT, dept="_report", task_id=None, auto=True)
        if res.ok and res.text.strip():
            body = res.text.strip()
    if not body:
        body = (f"## This week\n{len(done)} task(s) done, {len(failed)} failed, {len(projects)} project(s) touched.\n\n"
                + "\n".join(f"- {t['dept']}: {(t['done_line'] or t['title'])[:100]}" for t in done[:10]))
    org.s.x("INSERT INTO reports(day,dept,body,created,llm) VALUES(?,?,?,?,?) "
            "ON CONFLICT(day,dept) DO UPDATE SET body=excluded.body, created=excluded.created",
            (today(), "_weekly", body, time.time(), 1))
    return body


def weekly_jobs(org) -> list:
    from .heartbeat import past
    from .maintenance import once
    day = str(org.c.raw.get("weekly_review_day", "mon")).lower()[:3]
    if day not in DAYS or DAYS[datetime.now().weekday()] != day or not past(org.c.report_time):
        return []
    if not once(org, "last_weekly_review", period="week"):
        return []

    def job():
        from . import rhythm
        body = weekly_review(org)
        plan = rhythm.run_week(org)                   # the review feeds next week's priorities
        if plan.get("items"):
            body += "\n\n## This week's plan\n" + (plan.get("note") or "") + "\n" + "\n".join(
                f"- {it['title']}" + (f" (for: {it['objective']})" if it.get("objective") else "") for it in plan["items"])
        if getattr(org, "notify", None) and org.notify.settings().get("weekly_review"):
            org.notify.send("Weekly review", body.replace("**", ""))
    return [("weekly review", job)]


def run_reports(org, day: str | None = None) -> dict:
    day = day or today()
    org.s.set("last_report_day", day)        # first: a crash halfway must not re-bill every 20 seconds
    out = {}
    for dept in org.c.depts:
        body, llm = dept_report(org, dept, day)
        org.s.x("INSERT INTO reports(day,dept,body,created,llm) VALUES(?,?,?,?,?) "
                "ON CONFLICT(day,dept) DO UPDATE SET body=excluded.body, created=excluded.created, llm=excluded.llm",
                (day, dept, body, time.time(), int(llm)))
        out[dept] = body
    tot = org.s.one("SELECT COALESCE(SUM(input+output+cache_create),0) t, COUNT(*) n FROM usage WHERE day=?", (day,))
    depts = list(org.c.depts)
    n_done = org.s.one(f"SELECT COUNT(*) n FROM tasks WHERE status IN ('done','cached') "
                       f"AND dept IN ({','.join('?' * len(depts))}) AND updated>=? AND updated<?",
                       (*depts, *_day_bounds(day)))["n"]
    busy = [org.c.depts[d].name for d, b in out.items() if not b.startswith("idle")]
    a, b = _day_bounds(day)
    lines = [f"**Done:** {n_done} task(s)" + (f" in {', '.join(busy)}" if busy else "") + "."]
    needs = []
    if n := org.s.one("SELECT COUNT(*) n FROM outbox WHERE status='pending'")["n"]:
        needs.append(f"{n} draft(s) to review")
    if n := org.s.one("SELECT COUNT(*) n FROM tasks WHERE status='failed' AND dept!='ceo' AND updated>=? AND updated<?",
                      (a, b))["n"]:
        needs.append(f"{n} failed task(s)")
    if n := len(org.proposals.all()):
        needs.append(f"{n} proposed department(s)")
    if hasattr(org, "inbox_count"):
        needs.extend(org.inbox_count())
    lines.append("**Needs you:** " + ("; ".join(needs) if needs else "nothing.") )
    from . import rhythm
    moves = org.s.q("SELECT c.name, c.email, i.kind, i.summary FROM interactions i JOIN contacts c ON c.id=i.contact_id "
                    "WHERE i.at>=? AND i.at<? AND i.kind IN ('email_in','stage','payment') ORDER BY i.id DESC LIMIT 8",
                    (a, b))
    if moves:
        words = {"email_in": "replied", "payment": "paid", "stage": "moved"}
        lines.append("**Clients today:** " + "; ".join(
            f"{m['name'] or m['email']} {words[m['kind']]}" + (f" ({' '.join(m['summary'].split())[:40]})"
                                                               if m["kind"] != "email_in" else "") for m in moves))
    if obs := rhythm.objectives(org):
        lines.append("**Objectives:** " + "; ".join(rhythm.objective_line(o) for o in obs[:4]))
    if nums := [r for r in org.metrics.all() if r["value"] is not None]:
        lines.append("**Numbers:** " + " · ".join(org.metrics.fmt(r) for r in nums))
    wk = rhythm.current(org)
    if wk.get("week") == rhythm.week_id() and wk.get("items"):
        lines.append("**This week's plan:** " + "; ".join(
            f"{it['title']} [{it.get('status') or it.get('state')}]" for it in wk["items"]))
    lines.append(f"Plan usage: {tot['n']} model call(s) today.")
    roll = "\n".join(lines)
    org.s.x("INSERT INTO reports(day,dept,body,created,llm) VALUES(?,?,?,?,0) "
            "ON CONFLICT(day,dept) DO UPDATE SET body=excluded.body, created=excluded.created",
            (day, "_company", roll, time.time()))
    out["_company"] = roll
    return out
