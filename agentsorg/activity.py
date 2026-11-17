"""The activity log: one timeline of what the owner asked, what the company made of it, and what came of it.

Append-only JSON lines at data/activity.jsonl (rotated at 5 MB, 3 kept). Written without a model call:
- owner:  every change made from the dashboard or the CLI (the command text, an answer, a click), secrets removed
- ceo:    how a command was understood: the CEO's reply and the tasks it handed out
- agent:  each task that ended: team, what it delivered (DONE line), tokens, and the questions it raised
- system: what the company started by itself (weekly plan, follow-ups, decisions handed to Research, inbound mail)

Read it with `agentsorg log`, GET /api/activity, or the Log page.
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

MAX_BYTES, KEEP = 5_000_000, 3
TEXT = 1200
_lock = threading.Lock()
SECRET_KEYS = re.compile(r"value|pass|secret|token|api_?key|password|auth", re.I)
NOT_LOGGED = re.compile(r"^/api/(env\b|hooks/)")


def path(org) -> Path:
    return Path(org.home) / "data" / "activity.jsonl"


def record(org, actor: str, event: str, text: str = "", **extra) -> None:
    """Never raises: a log line must not break the work it describes."""
    try:
        e = {"at": round(time.time(), 1), "actor": actor, "event": event, "text": str(text or "")[:TEXT]}
        e.update({k: v for k, v in extra.items() if v not in (None, "", [], {})})
        line = json.dumps(e, ensure_ascii=False, default=str)
        if env := getattr(org, "env", None):
            line = env.redact(line)                      # a key's value never reaches the log
        p = path(org)
        with _lock:
            p.parent.mkdir(parents=True, exist_ok=True)
            if p.exists() and p.stat().st_size > MAX_BYTES:
                for i in range(KEEP - 1, 0, -1):
                    src = p.with_suffix(f".jsonl.{i}")
                    if src.exists():
                        src.replace(p.with_suffix(f".jsonl.{i + 1}"))
                p.replace(p.with_suffix(".jsonl.1"))
            with p.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        pass


def body_text(body: bytes) -> tuple[str, dict]:
    """What the owner sent: the human text first (command, answer, note), other fields as short key=value."""
    try:
        d = json.loads(body or b"{}")
    except Exception:
        return "", {}
    if not isinstance(d, dict):
        return str(d)[:300], {}
    d = {k: ("***" if SECRET_KEYS.search(k) else v) for k, v in d.items()}
    main = next((str(d[k]) for k in ("text", "command", "answer", "note", "question", "task", "body", "why")
                 if isinstance(d.get(k), str) and d[k].strip()), "")
    rest = {k: (v if isinstance(v, (int, float, bool)) else str(v)[:120]) for k, v in d.items()
            if not (isinstance(v, str) and str(v) == main)}
    return main, rest


def owner_api(org, method: str, url_path: str, body: bytes, status: int) -> None:
    if NOT_LOGGED.match(url_path):
        what = "key changed" if url_path.startswith("/api/env") else "webhook event received"
        record(org, "owner" if "env" in url_path else "system", what, "", path=re.sub(r"/hooks/.*", "/hooks/…", url_path),
               status=status)
        return
    text, fields = body_text(body)
    record(org, "owner", f"{method} {url_path}", text, fields=fields, status=status)


def owner_cli(org, argv: list[str]) -> None:
    args = list(argv)
    if args and args[0] in ("env", "keys"):
        args = args[:3]                                  # `env set NAME VALUE`: never the value
    record(org, "owner", "cli", " ".join(args))


def task_ended(org, tid: int, status: str) -> None:
    t = org.s.task(tid) or {}
    if not t:
        return
    res = t.get("result") or ""
    asks = re.findall(r"(?m)^ASK:\s*(.+)$", res)[:2]
    nxt = re.findall(r"(?m)^NEXT:\s*(.+)$", res)[:1]
    if t.get("dept") == "ceo":
        return                                            # the planner logs how it understood the command
    record(org, "agent", f"task {status}", t.get("done_line") or t.get("summary") or (t.get("error") or "")[:300],
           task=tid, project=t.get("project_id"), dept=t.get("dept"), member=t.get("member"), source=t.get("source"),
           title=(t.get("title") or "")[:140], tokens=t.get("tokens"), asks=asks, next=nxt,
           file=t.get("output_path"))


# ---------- reading ----------
def read(org, since: float = 0, actor: str = "", project: int | None = None, grep: str = "",
         limit: int = 300) -> list[dict]:
    p = path(org)
    files = [x for x in [p.with_suffix(f".jsonl.{i}") for i in range(KEEP, 0, -1)] + [p] if x.exists()]
    out = []
    for f in files:
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                e = json.loads(line)
            except Exception:
                continue
            if e.get("at", 0) < since or (actor and e.get("actor") != actor):
                continue
            if project is not None and e.get("project") != project and f"/projects/{project}/" not in e.get("event", "") \
                    and not str(e.get("event", "")).endswith(f"/projects/{project}"):
                continue
            if grep and grep.lower() not in json.dumps(e, ensure_ascii=False).lower():
                continue
            out.append(e)
    return out[-limit:]


def line(e: dict) -> str:
    when = time.strftime("%m-%d %H:%M", time.localtime(e.get("at", 0)))
    who = e.get("actor", "?")
    bits = [f"{when} {who:<6} {e.get('event', '')}"]
    if e.get("dept"):
        bits.append(f"[{e['dept']}#{e.get('task')}]")
    elif e.get("task"):
        bits.append(f"[#{e['task']}]")
    if e.get("project"):
        bits.append(f"(p{e['project']})")
    if e.get("tokens"):
        bits.append(f"{e['tokens']:,} tok")
    s = " ".join(bits)
    if e.get("asked"):
        s += f"\n    asked: {e['asked'][:300]}"
    if e.get("text"):
        s += f"\n    {'reply' if who == 'ceo' else ''}{': ' if who == 'ceo' else ''}{e['text'][:400]}"
    for t in e.get("handed_out") or []:
        s += f"\n    -> {t}"
    if e.get("fields"):
        s += "\n    " + " ".join(f"{k}={v}" for k, v in e["fields"].items())
    for a in e.get("asks") or []:
        s += f"\n    ASK {a[:200]}"
    for n in e.get("next") or []:
        s += f"\n    NEXT {n[:200]}"
    return s
