"""`agentsorg console`: the company in your terminal (works locally or through `agentsorg join HOST --terminal`)."""
from __future__ import annotations

import os
import sys
import threading
import time

import httpx

from pathlib import Path

from .config import company_name
from .files import remote_bin

ESC = "\033["
C = {"o": "38;5;173", "g": "32", "r": "31", "y": "33", "d": "2", "b": "1", "c": "36", "m": "35"}
DONE = ("done", "cached", "failed", "cancelled", "backlog", "queued_blocked")


def col(s: str, *k: str) -> str:
    if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        return s
    return f"{ESC}{';'.join(C[x] for x in k)}m{s}{ESC}0m"


def mshort(m: str | None) -> str:
    m = m or ""
    return col("H", "g") if "haiku" in m else col("S", "c") if "sonnet" in m else col("O", "m") if "opus" in m else col("-", "d")


HELP = f"""{col('Commands', 'b')}
  <text>                          command the company as CEO (Haiku router splits it)
  @dept <text>                    send straight to a department (0 routing tokens)
  /head <dept> <text>             command a department through its head
  /ask <dept> <member> <text>     give one employee work
  /backlog <dept> <text>          queue for autonomous work
  /status   /watch                office overview (watch = live, Ctrl+C to stop)
  /me <dept> <member>             one employee's report
  /tasks [dept]   /task <id>      recent tasks / full result
  /report   /usage   /kb [q]      daily reports / quota + tokens / knowledge base
  /auto on|off [percent]          autonomous mode and its quota cap
  /dash                           dashboard link + ssh tunnel command
  /help   /quit
  add !haiku, !sonnet or !opus to any command to force a model"""


class Client:
    def __init__(self, url: str, token: str):
        self.h = httpx.Client(base_url=url, headers={"X-Token": token}, timeout=60)

    def get(self, p: str, **params):
        r = self.h.get(p, params=params or None)
        r.raise_for_status()
        return r.json()

    def post(self, p: str, body: dict | None = None):
        r = self.h.post(p, json=body or {})
        r.raise_for_status()
        return r.json()


def _alive(url: str, token: str) -> bool:
    try:
        return httpx.get(f"{url}/api/state", headers={"X-Token": token}, timeout=3).status_code == 200
    except httpx.HTTPError:
        return False


def connect(org_home) -> tuple[Client, str]:
    """Use the running service if there is one; otherwise run an embedded one for this session."""
    from .config import Company
    c = Company(org_home)
    url, token = f"http://127.0.0.1:{c.port}", c.token()
    if _alive(url, token):
        return Client(url, token), "service"
    import uvicorn
    from .agents import Org
    from .api import create_app
    from .heartbeat import Heartbeat
    org = Org(org_home)
    hb = Heartbeat(org)
    hb.start()
    server = uvicorn.Server(uvicorn.Config(create_app(org, hb), host="127.0.0.1", port=c.port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(50):
        if _alive(url, token):
            break
        time.sleep(0.1)
    return Client(url, token), "embedded (stops when you quit; install the service to keep it running)"


def status(cl: Client) -> str:
    o = cl.get("/api/office")
    s = cl.get("/api/state")["budget"]
    w = s.get("windows") or {}
    label = {"five_hour": "5h", "seven_day": "7d"}
    q = " · ".join(f"{label.get(k, k)} {v['utilization']:.0f}%" for k, v in w.items()) or "unreadable"
    lines = [col(f"quota {q} · auto {'ON' if s['auto'] else 'off'} (cap {s['auto_max_percent']:.0f}%)"
                 f"{' · PAUSED' if s['paused'] else ''}", "d")]
    ceo = o["ceo"]
    lines.append(f"{col('CEO cabin', 'b', 'o')}  " + (col(f"routing: {ceo['task']['title']}", "y") if ceo["state"] == "working"
                                                      else col("idle", "d")))
    for d in o["depts"]:
        tok = sum(m["tokens"] for m in d["members"])
        meta = f"({d['id']}) · {tok:,} tok today"
        lines.append(f"{col(d['name'], 'b')} {col(meta, 'd')}")
        for m in d["members"]:
            crown = "👑" if m["head"] else "  "
            if m["state"] == "working":
                st = col("● ", "o") + f"{mshort(m['task']['model'])} {m['task']['title'][:60]}"
            elif m["state"] == "queued":
                st = col("◌ queued ", "y") + m["task"]["title"][:55]
            elif m["last"]:
                ok = m["last"]["status"] != "failed"
                st = col(("✓ " if ok else "✗ ") + (m["last"]["line"] or "")[:60], "g" if ok else "r")
            else:
                st = col("· idle", "d")
            lines.append(f"  {crown}{m['name']:<8} {col(m['id'], 'd'):<18} {st}   "
                         + col(f"done {m['done']} · backlog {m['backlog']}", "d"))
    return "\n".join(lines)


def dashboard_hint(cl: Client, company: str = "") -> str:
    """Dashboard URL, plus the one ssh command a client PC needs (nothing to install there)."""
    import getpass
    url = str(cl.h.base_url).rstrip("/")
    port = cl.h.base_url.port
    out = col("dashboard: ", "d") + f"{url}/?token={cl.h.headers['X-Token']}"
    ssh_conn = os.environ.get("SSH_CONNECTION", "").split()
    if len(ssh_conn) == 4:   # we are inside an ssh session: tell the user how to tunnel from their PC
        out += "\n" + col(f"  open it from your PC (only ssh needed): ssh -L {port}:127.0.0.1:{port} "
                          f"{getpass.getuser()}@{ssh_conn[2]} -t {remote_bin()}{(' --company ' + company) if company else ''}   then open the link above", "d")
    return out


def follow(cl: Client, ids: list[int]) -> None:
    """Live progress for dispatched tasks, then print results."""
    if not ids:
        return
    seen: dict[int, str] = {}
    try:
        while True:
            pending = 0
            for i in ids:
                t = cl.get(f"/api/task/{i}")
                key = f"{t['status']}|{t['model']}"
                if seen.get(i) != key:
                    seen[i] = key
                    c = "y" if t["status"] not in DONE else "g" if t["status"] in ("done", "cached") else "r"
                    print(f"  #{i} {t['dept']}/{t['member']} {col(t['status'], c)} {mshort(t['model'])}  {t['title'][:70]}")
                if t["status"] not in DONE:
                    pending += 1
            if not pending:
                break
            time.sleep(1.5)
    except KeyboardInterrupt:
        print(col("  (detached: tasks keep running; /tasks to check)", "d"))
        return
    for i in ids:
        t = cl.get(f"/api/task/{i}")
        head = "━━ #{} {}/{} · {} · {} tok".format(i, t["dept"], t["member"], t["model"] or "-", t["tokens"] or 0)
        print("\n" + col(head, "b", "o"))
        print(t["result"] or col(t["error"] or "(no output)", "r"))


def run(org_home) -> None:
    if os.name == "nt":
        os.system("")                     # enable ANSI colours on Windows terminals
    try:
        import readline  # noqa: F401  (history + arrow keys where available)
    except ImportError:
        pass
    cl, mode = connect(org_home)
    st = cl.get("/api/state")
    depts = [d["id"] for d in st["depts"]]
    company = company_name(Path(org_home))
    print(col(f"■ {company}", "b", "o") + col(f"  ({st['company']}) · connected to {mode}", "d"))
    print(dashboard_hint(cl, company))
    print(status(cl))
    print(col("Type a command for the company, or /help.", "d"))
    while True:
        try:
            line = input(col("ceo› ", "o", "b")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        try:
            if not line.startswith("/"):
                r = cl.post("/api/ceo", {"text": line})
                if r.get("error"):
                    print(col(r["error"], "r")); continue
                if r.get("answer"):
                    print(col("CEO: ", "o", "b") + r["answer"])
                for a in r.get("actions") or []:
                    print("  " + col(a, "g" if a.startswith("✓") else "r"))
                print(col(f"routed ({r['routing']}, {r.get('tokens', 0)} tok) → {len(r['tasks'])} task(s)", "d"))
                follow(cl, r["tasks"]); continue
            cmd, *rest = line[1:].split()
            arg = line[len(cmd) + 2:].strip()
            if cmd in ("q", "quit", "exit"):
                break
            elif cmd in ("h", "help"):
                print(HELP)
            elif cmd in ("s", "status"):
                print(status(cl))
            elif cmd in ("dash", "url"):
                print(dashboard_hint(cl, company))
            elif cmd == "watch":
                try:
                    while True:
                        sys.stdout.write("\033[2J\033[H" + status(cl) + col("\n\n(live · Ctrl+C to stop)", "d") + "\n")
                        sys.stdout.flush()
                        time.sleep(2)
                except KeyboardInterrupt:
                    print()
            elif cmd == "head" and rest and rest[0] in depts:
                r = cl.post(f"/api/dept/{rest[0]}/delegate", {"text": arg[len(rest[0]):].strip()})
                if r.get("error"):
                    print(col(r["error"], "r")); continue
                print(col(f"head delegated ({r.get('tokens', 0)} tok) → {len(r['tasks'])} task(s)", "d"))
                follow(cl, r["tasks"])
            elif cmd == "ask" and len(rest) >= 3 and rest[0] in depts:
                text = arg.split(None, 2)[2]
                r = cl.post(f"/api/dept/{rest[0]}", {"text": text, "member": rest[1]})
                follow(cl, r["tasks"])
            elif cmd == "backlog" and len(rest) >= 2 and rest[0] in depts:
                r = cl.post(f"/api/dept/{rest[0]}", {"text": arg[len(rest[0]):].strip(), "backlog": True})
                print(col(f"queued #{r['tasks'][0]} for autonomous work", "g"))
            elif cmd == "me" and len(rest) == 2:
                r = cl.get(f"/api/member/{rest[0]}/{rest[1]}")
                print(col(f"{r['name']}{' 👑' if r['head'] else ''} · {r['role']} · {r['dept_name']}", "b"))
                print("\n".join("  " + l for l in r["report"]))
            elif cmd == "tasks":
                for t in cl.get("/api/tasks", **({"dept": rest[0]} if rest else {}), limit=15):
                    print(f"  #{t['id']:<4} {t['dept'] + '/' + t['member']:<20} {t['status']:<8} {mshort(t['model'])} {t['title'][:60]}")
            elif cmd == "task" and rest:
                t = cl.get(f"/api/task/{rest[0]}")
                print(col(f"#{t['id']} {t['dept']}/{t['member']} · {t['status']} · {t['model']} · {t['tokens']} tok", "b"))
                print(t["result"] or t["error"] or "")
            elif cmd == "report":
                rep = cl.post("/api/reports/run")
                for d, body in rep.items():
                    print(col("■ " + ("Company" if d == "_company" else d), "b"))
                    print("  " + body.replace("\n", "\n  "))
            elif cmd == "usage":
                u = cl.get("/api/usage")
                for r in u["by_model"]:
                    print(f"  {mshort(r['model'])} {r['model']:<26} calls {r['calls']:<4} in {r['input'] or 0:,} out {r['output'] or 0:,}")
                print(col(f"  auto: {u['budget']['auto_ok'][1]} · manual: {u['budget']['manual_ok'][1]}", "d"))
            elif cmd == "kb":
                for k in cl.get("/api/kb", q=arg)[:20]:
                    print(f"  #{k['id']} {col(k['title'], 'b')}: {k['body'][:110]}")
            elif cmd == "auto" and rest:
                body = {"auto": rest[0] == "on"}
                if len(rest) > 1:
                    body["auto_max_percent"] = float(rest[1])
                s = cl.post("/api/settings", body)
                print(col(f"auto {'ON' if s['auto'] else 'off'} · cap {s['auto_max_percent']:.0f}% · {s['auto_ok'][1]}", "g"))
            else:
                print(col("unknown command or bad arguments. /help", "r"))
        except httpx.HTTPError as e:
            print(col(f"error: {e}", "r"))
