"""The company's operating rhythm: objectives -> this week's priorities -> daily duties -> the owner's brief.

Once a week (after the weekly review) one planner call turns the gap between the objectives and the numbers into
at most 5 priorities. What happens next is the owner's `autonomy` setting:
  run      (default) each priority starts as a project by itself, under the autonomous budget cap
  approve  the plan waits in the Inbox for one click
  manual   the plan is only a suggestion
Every gate still applies to a project the company starts itself: critical plans meet in the conference room, new
departments and abilities need approval, nothing leaves the Outbox without the owner's click.
Brakes: no new plan while more than 3 results wait for review; a weekly token ceiling; the pause switch and cap.
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import date, datetime, timedelta

log = logging.getLogger("agentsorg")
KEY = "week_plan"
MAX_PRIORITIES = 5
REVIEW_BRAKE = 3
WEEK_TOKENS = 600_000
MAX_DAILY_FOLLOWUPS = 5
WEEK_PROMPT = """You are the company's Chief of Staff, planning the coming week. Pick the few pieces of work that
move the OBJECTIVES most, given the BUSINESS, the NUMBERS, the PIPELINE and last week's REVIEW.
Rules:
- At most 5 priorities; fewer is better. Each one is a concrete outcome a department can finish this week, tied to
  one objective. Order them by how much they move revenue or the nearest objective.
- Favour the shortest path to paying customers: fix what blocks selling or delivering before building new things.
- Cover the functions the numbers say are weakest (no leads -> marketing and research; leads but no replies ->
  sales; customers waiting -> delivery). Don't repeat work a routine already does or an open project covers.
- Never invent prices, customers or figures. If an owner decision blocks an objective, make ONE priority that
  prepares that decision (options, evidence, a recommendation) instead of deciding it.
- Write each command as the owner would give it: self-contained, with what done looks like.
Output ONLY JSON:
{"note":"<=2 sentences: the week's focus and why",
 "priorities":[{"title":"<=8 words","objective":"<the objective it serves, or 'none'>","command":"<the command>"}]}"""


def week_id(now: datetime | None = None) -> str:
    """The week a plan is for. From Saturday on, a new plan is for the coming week, not the two days left."""
    now = now or datetime.now()
    if now.weekday() >= 5:
        now = now + timedelta(days=7 - now.weekday())
    return now.strftime("%G-W%V")


# ---------- objectives ----------
def objectives(org) -> list[dict]:
    """Each objective with where its number stands today."""
    out = []
    for o in org.c.objectives:
        row = dict(o)
        m = org.metrics.get(o["metric"]) if o.get("metric") else None
        row["value"] = m["value"] if m else None
        tgt = o.get("target") if o.get("target") is not None else (m or {}).get("target")
        row["target"] = tgt
        row["percent"] = (round(100 * min(max((row["value"] or 0) / tgt, 0), 1)) if tgt and row["value"] is not None
                          else None)
        if o.get("due"):
            try:
                row["days_left"] = (date.fromisoformat(o["due"]) - date.today()).days
            except ValueError:
                pass
        out.append(row)
    return out


def objective_line(o: dict) -> str:
    s = o["text"]
    if o.get("metric") and o.get("target") is not None:
        s += f" [{o['metric']}: {'?' if o['value'] is None else format(o['value'], 'g')} of {o['target']:g}]"
    if o.get("days_left") is not None:
        s += f" [{o['days_left']} days left]" if o["days_left"] >= 0 else f" [{-o['days_left']} days overdue]"
    if o.get("dept"):
        s += f" (owner: {o['dept']})"
    return s


def facts(org) -> str:
    obs = objectives(org)
    return ("OBJECTIVES (the owner's goals, with where each number stands):\n"
            + "\n".join(f"- {objective_line(o)}" for o in obs)) if obs else ""


# ---------- the weekly plan ----------
def current(org) -> dict:
    p = org.s.get(KEY) or {}
    for it in p.get("items") or []:
        if it.get("project"):
            row = org.s.one("SELECT status FROM projects WHERE id=?", (it["project"],))
            it["status"] = row["status"] if row else "gone"
    return p


def week_tokens(org, plan: dict | None = None) -> int:
    ids = [it["project"] for it in (plan or org.s.get(KEY) or {}).get("items") or [] if it.get("project")]
    if not ids:
        return 0
    marks = ",".join("?" * len(ids))
    return int(org.s.one(f"SELECT COALESCE(SUM(tokens),0) t FROM tasks WHERE project_id IN ({marks})", tuple(ids))["t"])


def brake(org) -> str:
    """Why the company should not start more work by itself right now ('' = go)."""
    if org.c.autonomy == "manual":
        return "autonomy is set to manual"
    n = org.s.one("SELECT COUNT(*) n FROM projects WHERE status='review'")["n"]
    if n > REVIEW_BRAKE:
        return f"{n} results are waiting for your review; the company doesn't start more until you've looked"
    ok, why = org.budget.check(auto=True, scheduled=True)
    if not ok:
        return why
    ceiling = int(org.c.budget.get("weekly_plan_tokens", WEEK_TOKENS))
    if week_tokens(org) >= ceiling:
        return f"this week's plan has used its {ceiling:,} token allowance"
    return ""


def context(org) -> str:
    since = time.time() - 7 * 86400
    review = org.s.one("SELECT body FROM reports WHERE dept='_weekly' ORDER BY id DESC LIMIT 1")
    open_p = org.s.q("SELECT title, status FROM projects WHERE status NOT IN ('done','failed','cancelled') "
                     "OR (final=0 AND status='review') ORDER BY id DESC LIMIT 12")
    done = org.s.q("SELECT title FROM projects WHERE status IN ('done','review') AND updated>? ORDER BY id DESC LIMIT 8",
                   (since,))
    sugg = []
    for p in org.s.q("SELECT suggestions FROM projects WHERE suggestions IS NOT NULL ORDER BY id DESC LIMIT 6"):
        sugg += [x["text"][:140] for x in json.loads(p["suggestions"]) if x["kind"] == "next" and x["state"] == "open"]
    parts = [f"FACTS:\n{org.company_facts()}", f"ROSTER:\n{org.c.roster()}"]
    if review:
        parts.append(f"LAST WEEK'S REVIEW:\n{review['body'][:1500]}")
    parts.append("OPEN PROJECTS (don't repeat):\n" + ("\n".join(f"- {p['title'][:90]} [{p['status']}]" for p in open_p)
                                                      or "none"))
    # what recent projects actually delivered, so the plan doesn't ask again for work that exists (on a branch too)
    built = []
    for p in org.s.q("SELECT id, title, status, review FROM projects WHERE status IN ('review','done') AND review IS NOT NULL "
                     "AND updated>? ORDER BY id DESC LIMIT 4", (time.time() - 30 * 86400,)):
        try:
            reqs = json.loads(p["review"]).get("requirements") or []
        except (TypeError, ValueError):
            continue
        ch = org.s.one("SELECT branch, status FROM changes WHERE project_id=? ORDER BY id DESC LIMIT 1", (p["id"],))
        where = f" (code on {ch['branch']}, {'merged' if ch['status'] == 'merged' else 'waiting for the owner to merge'})" \
            if ch and ch["status"] in ("ready", "merged") else ""
        built.append(f"- #{p['id']} {p['title'][:70]}{where}: " + "; ".join(
            f"{r['need'][:70]} [{r['status']}]" for r in reqs[:10]))
    if built:
        parts.append("ALREADY BUILT OR DECIDED IN RECENT PROJECTS (build on it; never plan it again; a partial item "
                     "may get a priority for its missing part only):\n" + "\n".join(built))
    if done:
        parts.append("FINISHED LAST WEEK:\n" + "\n".join(f"- {p['title'][:90]}" for p in done))
    if sugg:
        parts.append("FOLLOW-UPS TEAMS SUGGESTED:\n" + "\n".join(f"- {s}" for s in sugg[:8]))
    return "\n\n".join(parts)


def parse(text: str) -> dict:
    m = re.search(r"\{.*\}", text or "", re.S)
    try:
        data = json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        data = {}
    items = [{"title": str(p.get("title") or p.get("command"))[:80], "objective": str(p.get("objective") or "")[:160],
              "command": str(p["command"])[:1500], "state": "proposed", "project": None}
             for p in (data.get("priorities") or []) if isinstance(p, dict) and str(p.get("command") or "").strip()]
    return {"note": str(data.get("note") or "")[:400], "items": items[:MAX_PRIORITIES]}


def make(org, force: bool = False) -> dict:
    """Write this week's plan (one planner call). Returns the stored plan; `skipped` says why there is none."""
    wk = week_id()
    cur = org.s.get(KEY) or {}
    if cur.get("week") == wk and cur.get("items") and not force:
        return cur
    if not org.c.objectives and not org.c.business:
        return _skip(org, wk, "no objectives or business profile yet: add them on the Company page")
    n = org.s.one("SELECT COUNT(*) n FROM projects WHERE status='review'")["n"]
    if n > REVIEW_BRAKE and not force:
        return _skip(org, wk, f"{n} results are waiting for your review")
    ok, why = org.budget.check(auto=not force, scheduled=True)
    if not ok:
        return _skip(org, wk, why)
    res = org.call(context(org), model=org.c.models[2], system=WEEK_PROMPT, dept="_week", task_id=None,
                   auto=not force, cwd=org.home)
    if not res.ok:
        return _skip(org, wk, f"the planner couldn't run: {(res.error or '')[:160]}")
    plan = {"week": wk, "made": time.time(), "tokens": res.tokens, **parse(res.text)}
    if not plan["items"]:
        return _skip(org, wk, "the planner found nothing worth starting this week")
    org.s.set(KEY, plan)
    org.log_decision("plan", f"week {wk}", f"{len(plan['items'])} priorities", plan["note"])
    return plan


def _skip(org, wk: str, why: str) -> dict:
    plan = {"week": wk, "made": time.time(), "items": [], "skipped": why[:300]}
    org.s.set(KEY, plan)
    return plan


def start(org, i: int, by_owner: bool = False) -> dict:
    """Start one priority as a project. By the company itself it runs under the autonomous cap."""
    from .pipeline import plan_and_dispatch
    plan = org.s.get(KEY) or {}
    items = plan.get("items") or []
    if not (0 <= i < len(items)):
        raise KeyError("no such priority")
    it = items[i]
    if it.get("project"):
        return {"project": it["project"]}
    if not by_owner and (why := brake(org)):
        return {"held": why}
    out = plan_and_dispatch(org, it["command"], None, auto=not by_owner, source="ceo" if by_owner else "weekly")
    it["project"], it["state"] = out.get("project"), "started"
    org.s.set(KEY, plan)
    from . import activity
    activity.record(org, "owner" if by_owner else "system", "weekly priority started", it["title"],
                    project=out.get("project"), command=it["command"][:400])
    return out


def start_all(org, by_owner: bool = False) -> list[dict]:
    return [start(org, i, by_owner) for i, it in enumerate((org.s.get(KEY) or {}).get("items") or [])
            if not it.get("project") and it.get("state") != "dropped"]


def drop(org, i: int) -> None:
    """Stop a priority: one not started is dropped; a running one is cancelled."""
    plan = org.s.get(KEY) or {}
    it = (plan.get("items") or [])[i]
    if it.get("project"):
        try:
            org.projects.cancel(it["project"])
        except (KeyError, ValueError):
            pass
    it["state"] = "dropped"
    org.s.set(KEY, plan)


def run_week(org) -> dict:
    """The weekly step: plan, then start it unless the owner wants to approve first."""
    plan = make(org)
    if plan.get("items") and org.c.autonomy == "run":
        start_all(org)
    return org.s.get(KEY) or plan


# ---------- daily duties that cost no planning ----------
def follow_ups(org) -> list[int]:
    """Contacts whose follow-up date has passed get a drafted follow-up in the Outbox (one short task each)."""
    from . import blueprints
    from .clients import FOLLOW_UP_DAYS, MAX_FOLLOW_UPS
    if brake(org):
        return []
    d = blueprints.dept_for(org.c, "sales") or blueprints.dept_for(org.c, "marketing")
    if not d:
        return []
    ids = []
    for c in org.clients.due()[:MAX_DAILY_FOLLOWUPS]:
        sent = org.s.one("SELECT COUNT(*) n FROM interactions WHERE contact_id=? AND kind='email_out'", (c["id"],))["n"]
        nxt = (date.today() + timedelta(days=FOLLOW_UP_DAYS)).isoformat()
        if sent > MAX_FOLLOW_UPS:
            org.clients.upsert(c["email"], next_date="", force=True)       # stop chasing; the owner decides
            org.clients.log(c["id"], "note", "no reply after the follow-ups: no more automatic ones")
            continue
        if org.s.one("SELECT 1 x FROM outbox WHERE status IN ('pending','approved') AND body LIKE ?",
                     (f"%{c['email']}%",)):
            continue                                                         # a draft to them already waits
        dept = c["dept"] if c.get("dept") in org.c.depts else d.id
        kind = "follow-up" if sent else "first"
        task = (f"Draft the {kind} email to {c['name'] or c['email']} <{c['email']}> as ONE email (first lines "
                f"`To: {c['email']}` and `Subject:`), under 90 words, and mark it OUTBOX: yes. "
                + ("Add something new (a result, an example, an answer to a likely doubt); don't repeat the last "
                   "message. " if sent else "Open with one specific observation about them; one ask. ")
                + "Never quote a price the BUSINESS facts don't confirm.\n\n"
                f"THEIR RECORD:\n{org.clients.card(c)}")
        ids.append(org.submit(dept, task, source="duty", priority=4, tier=1, kind="deliverable"))
        from . import activity
        activity.record(org, "system", "follow-up drafted", c["email"], task=ids[-1], dept=dept)
        org.clients.upsert(c["email"], next_date=nxt, force=True)
    return ids


def stage_check(org) -> str:
    """The first recorded customer means the business is past 'launched': say so once, the owner confirms."""
    from . import blueprints
    stage = org.c.business.get("stage") or ""
    n = org.clients.counts().get("won", 0)
    want = "growing" if n >= 10 else "first_customers" if n >= 1 else ""
    if not want or (stage in blueprints.STAGES and blueprints.STAGES.index(stage) >= blueprints.STAGES.index(want)):
        return ""
    if org.s.get("stage_proposed") in (want, f"-{want}"):      # already asked (or the owner said not yet)
        return ""
    org.s.set("stage_proposed", want)
    return want


def jobs(org) -> list:
    """Once a day: department triggers, the stage check and the day's follow-ups."""
    from . import blueprints
    from .maintenance import once
    if not once(org, "last_rhythm_day"):
        return []

    def daily():
        stage_check(org)
        blueprints.check_triggers(org)
        org.clients.refresh_numbers()
        follow_ups(org)
        from . import decisions
        decisions.delegate(org)                       # questions the company may settle itself go to Research
        wk = org.s.get(KEY) or {}
        if org.c.autonomy == "run" and wk.get("week") == week_id() and not brake(org):
            start_all(org)                            # priorities a brake held back start once it clears
    return [("daily duties", daily)]
