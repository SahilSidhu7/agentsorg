"""Needs you: the few things only the owner can do, at most MAX_ITEMS, most urgent first. Deterministic (0 tokens).

Four kinds, nothing else:
  result    a finished project to accept (merges its code), redo with a note, or discard
  send      drafts waiting in the Outbox (one line, however many)
  question  a team can't finish without the owner; answered once, saved as a company fact
  problem   login, config or background errors, and failed work, each with a fix
Quota and running work are shown as status on Today, never as items. Results close by themselves after
projects.REVIEW_DAYS, so the list can't pile up.

Each item: {kind, id, title, detail, urgency (1 = now, 2 = today), actions: [{label, do, ...}]}.
"""
from __future__ import annotations

import time

AUTH_WORDS = ("login", "auth", "credential", "oauth", "not logged in", "invalid api key", "401")
MAX_ITEMS = 5


def _problems(org) -> list[dict]:
    s, out = org.s, []
    if org.c.load_error:
        out.append({"kind": "problem", "id": "yaml", "urgency": 1, "title": "company.yaml has an error",
                    "detail": org.c.load_error, "actions": []})
    if (err := s.get("heartbeat_error")) and time.time() - (err.get("at") or 0) < 3600:
        out.append({"kind": "problem", "id": "heartbeat", "urgency": 1,
                    "title": f"Background work hit an error ({err['step']})", "detail": err["error"],
                    "actions": [{"label": "Dismiss", "do": "dismiss_heartbeat"}]})
    auth = s.one("SELECT id FROM tasks WHERE status='failed' AND updated>? AND (" +
                 " OR ".join("lower(error) LIKE ?" for _ in AUTH_WORDS) + ") ORDER BY id DESC LIMIT 1",
                 (time.time() - 6 * 3600, *[f"%{w}%" for w in AUTH_WORDS]))
    if auth:
        out.append({"kind": "problem", "id": "login", "urgency": 1, "title": "Claude login needs attention",
                    "detail": "Agents can't sign in. On this machine run `claude` once (or `claude setup-token` and "
                              "save the token as CLAUDE_CODE_OAUTH_TOKEN under Company → Keys), then retry.",
                    "actions": [{"label": "Retry", "do": "retry", "id": auth["id"], "primary": True}]})
    since = time.time() - 3 * 86400
    for p in s.q("SELECT id, title, error FROM projects WHERE status='failed' AND updated>? ORDER BY id DESC LIMIT 3",
                 (since,)):
        out.append({"kind": "problem", "id": f"p{p['id']}", "urgency": 2, "title": f"Didn't finish: {p['title']}",
                    "detail": plain_error(p["error"]), "project": p["id"],
                    "actions": [{"label": "Try again", "do": "retry_project", "id": p["id"], "primary": True},
                                {"label": "Drop it", "do": "cancel_project", "id": p["id"]}]})
    for t in s.q("SELECT id, dept, title, error FROM tasks t WHERE status='failed' AND updated>? AND dept!='ceo' "
                 "AND (project_id IS NULL OR project_id NOT IN (SELECT id FROM projects WHERE status='failed')) "
                 "AND NOT EXISTS (SELECT 1 FROM tasks r WHERE r.hash=t.hash AND r.id>t.id AND r.status IN "
                 "('done','cached')) ORDER BY id DESC LIMIT 3", (since,)):
        out.append({"kind": "problem", "id": t["id"], "urgency": 2, "title": f"Failed: {t['dept']} · {t['title'][:80]}",
                    "detail": plain_error(t["error"]),
                    "actions": [{"label": "Retry", "do": "retry", "id": t["id"], "primary": True},
                                {"label": "Dismiss", "do": "dismiss", "id": t["id"]}]})
    return out


def _questions(org) -> list[dict]:
    from . import decisions
    out = []
    for g in decisions.grouped(org):                 # each question once, however many projects wait on it
        n = len(g["items"])
        out.append({"kind": "question", "id": g["key"], "urgency": 1 if n > 1 else 2,
                    "title": g["title"] if not g["topic"] else f"Decide: {g['title']}",
                    "detail": (f"{n} steps wait on this. " if n > 1 else "") + " · ".join(g["asks"][:3]),
                    "asks": g["asks"], "projects": g["projects"],
                    "actions": [{"label": "Answer", "do": "decision", "id": g["key"], "primary": True},
                                {"label": "Not needed", "do": "decision_drop", "id": g["key"]}]})
    return out


def _results(org) -> list[dict]:
    from .projects import REVIEW_DAYS
    out = []
    for p in org.s.q("SELECT id, title, updated FROM projects WHERE status='review' ORDER BY id"):
        code = org.s.one("SELECT COUNT(*) n, COALESCE(SUM(files),0) f FROM changes WHERE project_id=? AND "
                         "status='ready'", (p["id"],))
        left = max(0, REVIEW_DAYS - int((time.time() - p["updated"]) // 86400))
        what = (f"Includes code changes ({code['f']} file(s)): Accept merges them. " if code["n"] else "")
        out.append({"kind": "result", "id": p["id"], "urgency": 2, "title": f"Result: {p['title']}",
                    "detail": what + f"Closes by itself in {left} day(s) if you don't open it"
                              + (" (code is then thrown away, never merged)." if code["n"] else "."),
                    "project": p["id"],
                    "actions": [{"label": "Open", "do": "project", "id": p["id"], "primary": True}]})
    return out


def _send(org) -> list[dict]:
    n = org.s.one("SELECT COUNT(*) n FROM outbox WHERE status IN ('pending','approved')")["n"]
    if not n:
        return []
    return [{"kind": "send", "id": "outbox", "urgency": 2, "title": f"{n} draft(s) in the Outbox",
             "detail": "Read, edit, then send. Nothing goes out without your click.",
             "actions": [{"label": "Open the Outbox", "do": "view", "view": "outbox", "primary": True}]}]


def all_items(org) -> list[dict]:
    """Every item, before the MAX_ITEMS cut (the count on Today shows how many more wait)."""
    out = _problems(org) + _questions(org) + _results(org) + _send(org)
    return sorted(out, key=lambda x: x["urgency"])


def items(org) -> list[dict]:
    return all_items(org)[:MAX_ITEMS]


def counts(org) -> list[str]:
    by: dict[str, int] = {}
    for it in all_items(org):
        by[it["kind"]] = by.get(it["kind"], 0) + 1
    names = {"result": "result(s) to review", "question": "question(s) for you", "send": "Outbox reminder",
             "problem": "problem(s)"}
    return [f"{n} {names[k]}" for k, n in by.items()]


def plain_error(err: str | None) -> str:
    e = (err or "").lower()
    if any(w in e for w in AUTH_WORDS):
        return "Claude couldn't sign in. Run `claude` on this machine once, then retry."
    if "max_turns" in e:
        return "It ran out of working steps before finishing. Any files it wrote are kept. Retry, or split it into smaller steps."
    if "timeout" in e:
        return "It took longer than the time limit and was stopped. Retry, or split the task into smaller steps."
    if "limit" in e or "quota" in e or "429" in e:
        return "Your Claude plan limit was reached. It can run again once usage frees up."
    if "earlier step" in e or "dependency" in e:
        return "A step it needed failed or was cancelled. Retry that step first."
    if "not found" in e and "claude" in e:
        return "The claude command isn't installed on this machine."
    return (err or "Unknown error")[:240]
