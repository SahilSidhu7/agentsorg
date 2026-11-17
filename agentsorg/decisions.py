"""The owner's decision list: every open question from every project, grouped by what it waits on.

Teams end their work with lines like "once the owner gives the domain, …" — five projects can wait on the same
decision. A founder should see each decision once, answer it once, and have every waiting team continue. Grouping is
deterministic (0 tokens): known kinds of decision first (prices, domain, trial terms…), then word overlap.
"""
from __future__ import annotations

import hashlib
import json
import re
import time

TOPICS = [   # (key, what the owner is asked, words that mark it)
    ("pricing", "Prices, currency and billing type", r"\bpric|currenc|billing|\$\d|£\d|€\d|per month|/mo\b|one-off"),
    ("domain", "The public domain and sign-up link", r"\bdomain|sign-?up url|public url|sign-up path|\burl\b|<domain>"),
    ("trial", "The free-trial terms", r"trial terms|day-30|day 30|trial-terms|continue email"),
    ("staging", "A staging environment and Docker access for the agents", r"staging|docker|\.env\.staging"),
    ("email_setup", "Email sending checks (SPF, DKIM, DMARC, inbox placement)",
     r"\bdkim|\bspf\b|dmarc|brevo|deliverab|pre-send check|inbox (placement|check)"),
    ("legal", "Legal text: terms, privacy, refunds", r"\blegal|privacy|terms of service|refund polic|gdpr"),
    ("access", "File or system access for a team", r"chown|chmod|write access|permission"),
]
STOP = {"owner", "founder", "once", "then", "until", "after", "when", "confirms", "confirm", "gives", "give", "decides",
        "decide", "approves", "approve", "sets", "the", "and", "for", "with", "from", "this", "that", "their", "them",
        "engineering", "sales", "marketing", "research", "delivery", "client", "team", "context", "asked", "answered"}


def _clean(text: str) -> str:
    return re.sub(r"\s*\(Context:.*$", "", str(text or ""), flags=re.S).strip()


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9$£€.]+", text.lower()) if len(w) > 3 and w not in STOP}


def topic(text: str) -> tuple[str, str]:
    low = _clean(text).lower()
    for key, label, pat in TOPICS:
        if re.search(pat, low):
            return key, label
    return "", ""


def open_items(org) -> list[dict]:
    out = []
    for p in org.s.q("SELECT id, title, suggestions FROM projects WHERE suggestions IS NOT NULL "
                     "AND status NOT IN ('cancelled', 'done') ORDER BY id DESC LIMIT 40"):
        for s in json.loads(p["suggestions"]):
            if s["kind"] == "question" and s["state"] == "open":
                out.append({**s, "project": p["id"], "project_title": p["title"]})
    return out


def grouped(org) -> list[dict]:
    """[{key, title, items: [...], projects: [...]}] most-waited-on first."""
    groups: list[dict] = []
    for it in open_items(org):
        k, label = topic(it["text"])
        g = next((g for g in groups if k and g["topic"] == k), None)
        if not g and not k:
            w = _words(_clean(it["text"]))
            g = next((g for g in groups if not g["topic"] and w and
                      len(w & g["words"]) / max(1, len(w | g["words"])) >= 0.35), None)
        if not g:
            g = {"topic": k, "title": label or _clean(it["text"])[:140], "items": [], "words": set()}
            groups.append(g)
        g["items"].append(it)
        g["words"] |= _words(_clean(it["text"]))
    for g in groups:
        g["projects"] = sorted({i["project"] for i in g["items"]})
        g["key"] = g["topic"] or hashlib.sha1(g["title"].encode()).hexdigest()[:10]
        g["asks"] = list(dict.fromkeys(_clean(i["text"])[:220] for i in g["items"]))[:6]
        del g["words"]
    return sorted(groups, key=lambda g: (-len(g["items"]), g["title"]))


DECIDED = "decided"            # settings: [{topic, q, words, answer, by, at}] newest last


def remember(org, question: str, answer: str, by: str = "owner") -> None:
    """Every answered question, so the same question is applied next time instead of asked again."""
    q = _clean(question)
    k, _ = topic(q)
    rows = [r for r in (org.s.get(DECIDED) or []) if not (k and r["topic"] == k) and r["q"] != q[:300]]
    rows.append({"topic": k, "q": q[:300], "words": sorted(_words(q)), "answer": answer[:600], "by": by,
                 "at": time.time()})
    org.s.set(DECIDED, rows[-60:])


def already_decided(org, question: str) -> dict | None:
    """The stored answer to this question (same topic, or mostly the same words), or None."""
    q = _clean(question)
    k, _ = topic(q)
    w = _words(q)
    for r in reversed(org.s.get(DECIDED) or []):
        if k and r["topic"] == k:
            return r
        rw = set(r.get("words") or [])
        if not k and not r["topic"] and w and len(w & rw) / max(1, len(w | rw)) >= 0.5:
            return r
    return None


def answer(org, key: str, text: str, save: bool = True) -> dict:
    """One answer for every question in the group; each waiting team continues with it (one task per project)."""
    g = next((g for g in grouped(org) if g["key"] == key), None)
    if not g:
        raise KeyError("no such decision")
    started, saved = [], False
    for it in g["items"]:
        r = org.projects.answer_suggestion(it["project"], it["id"], text, save=save and not saved,
                                           run=it["project"] not in {x["project"] for x in started})
        saved = saved or r["saved"]
        if r["task"]:
            started.append({"project": it["project"], "task": r["task"]})
    return {"answered": len(g["items"]), "started": started}


def dismiss(org, key: str) -> int:
    g = next((g for g in grouped(org) if g["key"] == key), None)
    if not g:
        raise KeyError("no such decision")
    for it in g["items"]:
        org.projects.act_suggestion(it["project"], it["id"], "dismiss")
    return len(g["items"])
