"""`agentsorg` command line."""
from __future__ import annotations

import argparse
import re
import sys
import time
import webbrowser
from pathlib import Path

from .config import init_home, load_registry, register, resolve_home, save_registry


def _home(args) -> Path:
    try:
        return resolve_home(args.home, getattr(args, "company", None))
    except FileNotFoundError as e:
        sys.exit(str(e))


def _org(args):
    from .agents import Org
    try:
        org = Org(_home(args))
    except FileNotFoundError as e:
        sys.exit(str(e))
    # always say which company a command acts on (stderr, so piped output like `files send` stays clean)
    print(f"■ {org.c.slug} · {org.home} · port {org.c.port}", file=sys.stderr)
    for m in org.migrated:
        print(f"  moved {m}", file=sys.stderr)
    if getattr(args, "cmd", "") not in READ_ONLY_CMDS:
        from . import activity
        activity.owner_cli(org, sys.argv[1:])
    return org


READ_ONLY_CMDS = {"ask", "log", "info", "check", "usage", "companies", "start", "service", "tick", None, ""}


def cmd_log(args):
    """agentsorg log: what the owner asked, what the CEO made of it, what each team delivered."""
    import json
    import time as _t
    from . import activity
    org = _org(args)
    since = _t.time() - float(args.hours) * 3600
    rows = activity.read(org, since=since, actor=args.who or "", project=args.project, grep=args.grep or "",
                         limit=args.limit)
    if args.json:
        for e in rows:
            print(json.dumps(e, ensure_ascii=False))
        return
    if not rows:
        print(f"Nothing logged in the last {args.hours} hour(s). The log is {activity.path(org)}")
    for e in rows:
        print(activity.line(e))


def cmd_init(args):
    if args.dir:
        home = Path(args.dir).expanduser().resolve()
    elif (Path.cwd() / "company.yaml").exists():
        home = Path.cwd()
    else:
        sys.exit("usage: agentsorg init <folder>   (each company lives in its own folder, e.g. ~/my-company)")
    made = init_home(home, force=args.force)
    from .agents import Org
    org = Org(home)
    name = register(home, make_default=args.default)
    print(f"Company {name} ready at {home} (port {org.c.port})")
    for m in made:
        print(f"  + {m}")
    desc = args.describe
    if desc is None and sys.stdin.isatty() and not org.s.get("designed", False):
        try:
            desc = input("\nDescribe what your company should do (Enter = keep the default office):\n> ").strip()
        except EOFError:   # some shells report a tty with nothing behind it
            desc = ""
    projects = [str(Path(p).expanduser().resolve()) for p in (args.project or [])]
    desc = _with_outlines(desc or "", (args.scan or []) + projects)
    for p in projects:                              # rules files, README, manifests: the designer sees the real project
        try:
            from .intake import scan_folder
            desc += "\n\nPROJECT FILES (" + p + "):\n" + scan_folder(Path(p))[0][:5000]
        except (OSError, ValueError) as e:
            print(f"skipped reading {p}: {e}")
    if desc:
        _design_flow(org, desc, args.yes, _interview(org, desc, args.yes))
    if projects:
        _link_projects(org, projects)
        print("Reading the project once so agents don't have to explore it (1 Sonnet call)…")
        r = org.intake.build(force=True)
        print(f"  context pack: {org.intake.pack_path()}" + ("" if r.get("llm") else " (plain scan: no model available)"))
    print(f"Dashboard token: {org.c.token()}")
    print("Next: agentsorg check   then   agentsorg service install   (or agentsorg start)")


def cmd_context(args):
    org = _org(args)
    if args.action == "build":
        r = org.intake.build(force=True)
        print(r.get("skipped") or f"context pack written ({r['chars']:,} chars, {'1 model call' if r['llm'] else 'plain scan'})")
    st = org.intake.status()
    print(f"folders read: {', '.join(st['folders']) or 'none (link one: agentsorg files add ...)'}")
    if st["exists"]:
        print(f"pack: {org.intake.pack_path()}\n")
        print(org.intake.pack_path().read_text(encoding="utf-8")[:3000])


def _link_projects(org, projects: list[str]) -> None:
    """Link each project folder live (read-only) unless the design already did."""
    have = {str(org.files.path(s).resolve()) for s in org.files.sources().values()}
    head = next(iter(org.c.depts))
    for p in projects:
        if p in have:
            continue
        try:
            s = org.files.add(Path(p).name, head, f"this-pc {p}")
            print(f"  linked {p} live to {head} (read-only, nothing copied)")
        except (ValueError, KeyError, FileExistsError) as e:
            print(f"  couldn't link {p}: {e}")


def _with_outlines(desc: str, scans: list[str] | None) -> str:
    """Append a deterministic outline of each scanned local folder (0 tokens to build) to the description."""
    from .design import folder_outline
    parts = [desc.strip()] if desc.strip() else []
    for spec in scans or []:
        label, _, path = spec.partition("=") if "=" in spec and not spec[1:3] == ":\\" else ("this-pc", "", spec)
        try:
            parts.append(folder_outline(path, label=label or "this-pc"))
            print(f"scanned {path}")
        except ValueError as e:
            print(f"skipped scan: {e}")
    return "\n\n".join(parts)


def _interview(org, desc: str, yes: bool = False) -> dict:
    """The same short questions the dashboard asks, for whatever the description doesn't already say."""
    from . import design
    if yes or not sys.stdin.isatty():
        return {}
    qs = design.questions(desc, org.intake.design_brief())
    if qs:
        print("\nA few questions so the company fits your business (Enter skips one):")
    answers = {}
    for q in qs:
        try:
            a = input(f"  {q['q']}\n  > ").strip()
        except EOFError:
            break
        if a:
            answers[q["id"]] = a
    return answers


def _design_flow(org, desc: str, yes: bool, answers: dict | None = None) -> bool:
    from . import blueprints, design
    tn = ["", "Haiku", "Sonnet", "Opus"]
    print("Designing the leanest office for that (1 Sonnet call)…")
    try:
        plan = design.propose(org, design.with_answers(desc, answers or {}))
    except (RuntimeError, ValueError) as e:
        print(f"Design failed ({e}). Keeping the current office; retry with `agentsorg design \"...\"`.")
        return False
    print(f"\n■ {plan['name']}: {plan['about']}\n  {plan['summary']} · {plan['tokens']:,} tokens to design")
    for d in plan["departments"]:
        print(f"  {d['name']}{' [files/code]' if d['tools'] else ''}: {d['mission']}"
              + (f"  (does: {', '.join(d['functions'])})" if d.get("functions") else ""))
        for i, m in enumerate(d["members"]):
            esc = f"→{tn[m['max_tier']]}" if m["max_tier"] > m["tier"] else ""
            print(f"    {'👑' if i == 0 else '  '}{m['name']:<10} {m['role']:<40} {tn[m['tier']]}{esc}")
    for x in plan.get("later") or []:
        print(f"  Later, its own team: {blueprints.all()[x['function']]['name']} at {blueprints.when_text(x['when'])}"
              + (f" ({x['why']})" if x.get("why") else ""))
    for g in plan.get("goals") or []:
        print(f"  Goal: {g['goal']}" + (f"  (tracked as {g['metric']}" + (f", target {g['target']:g})" if g.get("target") is not None else ")")
                                       if g.get("metric") else ""))
    for p in plan.get("projects") or []:
        print(f"  First project ({p['dept'] or 'planner'}): {p['title']}")
    for r in plan.get("routines") or []:
        print(f"  Routine ({r['every']}, {r['dept']}): {r['task'][:80]}")
    if plan.get("sources"):
        print("  Folders → departments:")
        for s in plan["sources"]:
            where = s["origin"].split(" ", 1)[-1]
            live = Path(where).expanduser().is_dir()
            target = f"live folder for {s['dept']}" if live else f"{s['dept']}/sources/{s['name']} (synced copy)"
            print(f"    {s['origin']}  →  {target}" + (f"   ({s['why']})" if s["why"] else ""))
    try:
        if not yes and sys.stdin.isatty() and input("\nBuild this office? [Y/n] ").strip().lower() in ("n", "no"):
            print("Kept the current office.")
            return False
    except EOFError:
        pass
    linked = design.apply(org, plan, desc, answers or {})
    print("Office built.")
    if linked:
        live = [n for n in linked if org.files.get(n).mode == "direct"]
        synced = [n for n in linked if n not in live]
        if live:
            print(f"Linked {len(live)} live folder(s), read-only, used in place: {', '.join(live)}")
        if synced:
            print(f"Linked {len(synced)} folder(s) on other computers; copy them in with the push command from "
                  f"`agentsorg files commands <name>`: {', '.join(synced)}")
    return True


def cmd_design(args):
    org = _org(args)
    desc = _with_outlines(" ".join(args.text), args.scan)
    if not desc:
        sys.exit('usage: agentsorg design "what you need, and your folders: PC1 C:\\work\\app = backend code ..." '
                 '[--scan [LABEL=]FOLDER]')
    _design_flow(org, desc, args.yes, _interview(org, desc, args.yes))


def cmd_business(args):
    """Show or set the business profile: agentsorg business [--set field=value ...]"""
    from . import blueprints, design
    org = _org(args)
    fields = {}
    for kv in args.set or []:
        k, _, v = kv.partition("=")
        if k not in design.ANSWER_FIELDS.values():
            sys.exit(f"unknown field {k!r}: use {', '.join(design.ANSWER_FIELDS.values())}")
        fields[k] = design.stage_of(v) if k == "stage" else v
        if k == "stage" and not fields[k]:
            sys.exit(f"stage is one of: {', '.join(blueprints.STAGES)}")
    if fields:
        design.set_business(org, fields)
        blueprints.check_triggers(org)
    print(org.c.business_text() or "No business profile yet: agentsorg business --set customer=\"...\" --set stage=building")
    rm = blueprints.roadmap(org)
    print("\nWho does what:")
    for r in rm["now"]:
        print(f"  {r['name']:<24} {r['dept_name']}" + ("" if r["own_team"] else "  (carried, no team of its own)"))
    for r in rm["later"]:
        print(f"  {r['name']:<24} later: {r['when']}" + (" (proposed: see the Inbox)" if r["state"] == "proposed" else ""))


def cmd_org(args):
    """agentsorg org reorganise: map the departments you have to company functions and plan the missing ones."""
    from . import blueprints
    org = _org(args)
    if args.action == "reorganise":
        r = blueprints.reorganise(org)
        print(f"Kept {len(r['kept'])} department(s): {', '.join(r['kept'])}")
        print(f"Not covered by any team: {', '.join(r['later']) or 'none'} (add a team on the Company page if you need one)")
    args.set = None
    cmd_business(args)


def cmd_clients(args):
    """agentsorg clients [list|import FILE|add EMAIL [--name --org --stage]|revenue EMAIL AMOUNT [--currency --recurring]]"""
    org = _org(args)
    a = args.action or "list"
    try:
        if a == "import":
            r = org.clients.import_csv(Path(args.args[0]).expanduser().read_text(encoding="utf-8", errors="replace"))
            print(f"added {r['added']}, updated {r['updated']}, skipped {r['skipped']} (no email)")
        elif a == "add":
            c = org.clients.upsert(args.args[0], name=args.name or "", org=args.org or "", stage=args.stage or "",
                                   source="owner", force=True)
            print(f"{c['email']}: {c['stage']}")
        elif a == "revenue":
            org.clients.add_revenue(float(args.args[1]), email=args.args[0], currency=args.currency or "",
                                    kind="recurring" if args.recurring else "one-off")
            print("recorded")
    except (IndexError, ValueError, OSError) as e:
        sys.exit(f"{cmd_clients.__doc__}\n{e}")
    counts = org.clients.counts()
    print("Pipeline: " + (" · ".join(f"{s} {n}" for s, n in counts.items() if n) or "no contacts yet"))
    for c in org.clients.all(limit=30):
        print(f"  {c['stage']:<10} {(c['name'] or c['email'])[:28]:<28} {c['email'][:34]:<34} "
              f"{('next ' + c['next_date']) if c['next_date'] else ''}")


def cmd_files(args):
    org = _org(args)
    f, a, rest = org.files, args.action, args.rest
    need = lambda: rest[0] if rest else sys.exit(f"usage: agentsorg files {a} <name>")
    err = lambda *x: print(*x, file=sys.stderr)
    try:
        if a == "list":
            srcs = f.sources()
            if not srcs:
                print("No linked folders. Link one: agentsorg files add <name> --dept <dept> --origin \"PC1 C:\\path\"")
            for s in srcs.values():
                if s.mode == "direct":
                    q = f.quick_status(s.name)
                    print(f"  {s.name:<18} {s.dept:<12} {q['files']:>5} files · LIVE "
                          f"{'writable' if s.writable else 'read-only'} folder {f.path(s)}")
                    continue
                st = f.status(s.name)
                when = time.strftime("%Y-%m-%d %H:%M", time.localtime(st["synced"])) if st["synced"] else "never"
                print(f"  {s.name:<18} {s.dept:<12} {st['files']:>5} files · synced {when} · "
                      f"{len(st['changed_by_agents'])} changed by agents · {len(st['conflicts'])} conflicts\n"
                      f"                     from {s.origin or '?'}")
            print(f"\nlayout: {f.root}/<dept>/sources/<name>, <dept>/outputs/, shared/")
        elif a == "add":
            if not args.dept:
                sys.exit("usage: agentsorg files add <name> --dept <dept> [--origin \"PC1 C:\\path\"]")
            s = f.add(need(), args.dept, args.origin or "", mode=args.mode, writable=args.writable)
            if s.mode == "direct":
                print(f"linked {s.name} as a live {'writable' if s.writable else 'read-only'} folder: {f.path(s)} "
                      f"(agents use it in place; nothing to sync)")
                return
            print(f"linked {s.name} -> {f.path(s)}\n")
            _print_sync_cmds(f.commands(s.name, args.server, args.folder or ""))
        elif a == "commands":
            _print_sync_cmds(f.commands(need(), args.server, args.folder or ""))
        elif a == "status":
            st = f.status(need())
            for k in ("changed_by_agents", "deleted_by_agents", "conflicts"):
                print(f"{k.replace('_', ' ')}: {len(st[k])}")
                for x in st[k][:30]:
                    print(f"  {x}")
        elif a == "receive" and args.from_dir:
            rep = f.receive_dir(need(), args.from_dir)
            print(f"{rest[0]}: +{len(rep['added'])} added, {len(rep['updated'])} updated, {rep['unchanged']} unchanged, "
                  f"{len(rep['deleted'])} deleted, {len(rep['conflicts'])} conflicts")
            for c in rep["conflicts"]:
                print(f"  CONFLICT {c}: changed on both sides; agents' version kept, yours saved as *.from-pc*")
        elif a == "send" and args.to_dir:
            rep = f.send_dir(need(), args.to_dir, everything=args.all)
            print(f"{rest[0]}: wrote {len(rep['sent'])} file(s) changed by agents into {args.to_dir}")
            for c in rep["held_conflicts"]:
                print(f"  held back {c}: resolve the conflict first")
        elif a == "receive":
            if args.file:                      # uploaded with scp (Windows); removed once applied
                p = Path(args.file).expanduser()
                with p.open("rb") as fh:
                    rep = f.receive(need(), fh)
                p.unlink(missing_ok=True)
            else:
                rep = f.receive(need(), sys.stdin.buffer)
            print(f"{rest[0]}: +{len(rep['added'])} added, {len(rep['updated'])} updated, {rep['unchanged']} unchanged, "
                  f"{len(rep['deleted'])} deleted, {len(rep['kept_agent_files'])} agent files kept")
            for c in rep["conflicts"]:
                print(f"  CONFLICT {c}: changed on both sides. Agents' version kept; yours saved as "
                      f"{c.rsplit('.', 1)[0] if '.' in c else c}.from-pc{('.' + c.rsplit('.', 1)[1]) if '.' in c else ''}")
        elif a == "send":
            if args.file:                      # fetched with scp (Windows)
                with Path(args.file).expanduser().open("wb") as fh:
                    rep = f.send(need(), fh, everything=args.all)
            else:
                rep = f.send(need(), sys.stdout.buffer, everything=args.all)
                sys.stdout.buffer.flush()
            err(f"{rest[0]}: sent {len(rep['sent'])} file(s) changed by agents")
            for c in rep["held_conflicts"]:
                err(f"  held back {c}: resolve the conflict on the server first")
            for d in rep["deleted_by_agents"]:
                err(f"  agents deleted {d}: delete it on your PC if you agree")
        elif a == "remove":
            f.remove(need(), delete_files=args.delete_files)
            print("unlinked" + (" and deleted the copy" if args.delete_files else " (the copy on the server is kept)"))
        elif a == "outputs":
            dept = need()
            for o in f.outputs(dept):
                print(f"  {o['name']}")
    except (KeyError, FileExistsError, ValueError) as e:
        sys.exit(f"error: {e}")


def cmd_services(args):
    from .services import Services, supported
    org = _org(args)
    sv, a, rest = org.services, args.action, args.rest
    need = lambda: rest[0] if rest else sys.exit(f"usage: agentsorg services {a} <name>")
    try:
        if a == "list":
            rows = sv.overview()
            if not rows:
                print("No services yet. Find them: agentsorg services discover")
            for r in rows:
                nxt = f" · next {r['next']}" if r["next"] else ""
                print(f"  {'●' if r['state'] == 'active' else '○'} {r['name']:<22} {r['state']:<9} {r['detail']:<22} "
                      f"{(r['dept'] or '-'):<12} {r['about'] or r['unit']}{nxt}")
        elif a == "discover":
            hints = [str(org.files.path(s)) for s in org.files.sources().values() if s.mode == "direct"] + (args.path or [])
            keywords = (args.keyword or []) + [w for w in re.split(r"[^a-z0-9]+", org.c.slug) if len(w) > 3]
            found = sv.discover(hints, keywords)
            if not supported():
                print("Service discovery needs systemd (Linux).")
            for f_ in found:
                print(f"  {f_['unit']:<34} {f_['scope']:<6} {f_['about'][:60]}   (matched {f_['why']})")
            if found and args.add:
                for f_ in found:
                    sv.add(f_["name"], kind="systemd", unit=f_["unit"], scope=f_["scope"], about=f_["about"])
                print(f"added {len(found)} service(s)")
            elif found:
                print("\nAdd them all with --add, or one by one: agentsorg services add <name> --unit <unit> "
                      "[--scope user] [--dept sales]")
            elif supported():
                print("Nothing matched. Point it at folders: --path ~/harvey, or names: --keyword harvey")
        elif a == "add":
            s = sv.add(need(), kind=args.kind, unit=args.unit, scope=args.scope, container=args.container,
                       dept=args.dept, about=args.about)
            print(f"added {s.name}: {sv.status(s)['state']}")
        elif a == "remove":
            sv.remove(need())
            print("removed (the service itself is untouched)")
        elif a in ("start", "stop", "restart"):
            ok, msg = sv.control(sv.get(need()), a)
            print(msg)
            if not ok:
                sys.exit(1)
        elif a == "logs":
            print(sv.logs(sv.get(need()), args.lines))
        elif a == "permissions":
            text, install = sv.permissions()
            if not text:
                print("Nothing needs permission: no system services or containers are registered.")
                return
            path = org.home / "data" / "sudoers-services"
            path.write_text(text, encoding="utf-8")
            print(text)
            print(f"Saved to {path}. To allow exactly these start/stop/restart commands without a password, run once:\n")
            print("  " + install.format(file=path))
    except (KeyError, ValueError) as e:
        sys.exit(f"error: {e}")


def _print_sync_cmds(c: dict) -> None:
    print("Run these on the PC that has the folder (nothing to install there; tar and ssh are built in):\n")
    print(f"  Windows (PowerShell)\n    push: {c['push_powershell']}\n    pull: {c['pull_powershell']}\n")
    print(f"  macOS / Linux\n    push: {c['push_bash']}\n    pull: {c['pull_bash']}\n")
    print(f"  Same machine as the company\n    push: {c['push_local']}\n    pull: {c['pull_local']}\n")
    print(c["note"])


def cmd_check(args):
    from .runtime import check_cli
    org = _org(args)
    ok = True
    if not org.exe:
        print("✗ claude CLI not found. Install Claude Code, or set claude_bin in company.yaml"); ok = False
    else:
        missing = check_cli(org.exe)
        print(f"{'✗' if missing else '✓'} claude CLI {org.exe}" + (f" missing flags {missing}" if missing else ""))
        ok &= not missing
        if not missing:
            r = org.call("Reply with exactly: OK", model=org.c.models[1], system="Reply tersely.", dept="_check",
                         task_id=None, auto=False)
            if r.ok:
                print(f"✓ login works ({org.c.models[1]}, {r.tokens} tokens for the test call)")
            else:
                print(f"✗ test call failed: {r.error[:200]}\n  Fix: run `claude` once and log in (/login).")
                ok = False
    snap = org.budget.snapshot()
    if snap["windows"]:
        w = ", ".join(f"{k} {v['utilization']:.0f}%" for k, v in snap["windows"].items())
        print(f"✓ subscription usage: {w} | auto cap {snap['auto_max_percent']:.0f}%")
    else:
        print(f"! subscription usage unreadable -> fallback daily call caps "
              f"(auto {org.c.budget['auto_max_calls']}, manual {org.c.budget['manual_max_calls']})")
    print(f"  departments: {', '.join(org.c.depts)}")
    sys.exit(0 if ok else 1)


def _port_owner(port: int, token: str) -> str:
    """'' if the port is free, 'this company' if this company already serves it, else a short description."""
    import socket
    with socket.socket() as s:
        s.settimeout(1)
        if s.connect_ex(("127.0.0.1", port)) != 0:
            return ""
    try:
        import httpx
        r = httpx.get(f"http://127.0.0.1:{port}/api/state", headers={"X-Token": token}, timeout=3)
        if r.status_code == 200:
            return "this company"
        if r.status_code == 401:
            return "another agentsorg company (different token)"
    except Exception:
        pass
    return "another program"


def cmd_start(args):
    import uvicorn
    from .api import create_app
    from .heartbeat import Heartbeat
    from .fsutil import setup_logging
    if args.host not in ("127.0.0.1", "localhost", "::1") and not args.insecure:
        sys.exit(f"Refusing to listen on {args.host}: the dashboard is plain HTTP and anyone who can reach the port "
                 f"and sees the token can command your company. Use an SSH tunnel (ssh -L) from other "
                 f"computers, or pass --insecure if this network is private and you accept the risk.")
    org = _org(args)
    setup_logging(org.home, args.log)          # rotating log; in service mode stdout/stderr go there too
    port = args.port or org.c.port
    url = f"http://127.0.0.1:{port}/?token={org.c.token()}"
    shown = url if not args.log else f"http://127.0.0.1:{port}/  (link with token: `agentsorg info`)"
    busy = _port_owner(port, org.c.token())
    if busy == "this company":   # e.g. the background service is already serving it
        print(f"{org.c.name} is already running on port {port} (probably the background service).\n"
              f"Dashboard: {url}\nFrom another PC: ssh -L {port}:127.0.0.1:{port} <you>@<this machine> "
              f"-t ~/.local/bin/agentsorg --company {org.c.slug}   (then open the link above)")
        return
    if busy:
        sys.exit(f"Port {port} is in use by {busy}. Start on another port with `agentsorg start --port {port + 1}`, "
                 f"or set `port:` in {org.home / 'company.yaml'}.")
    org.recover()                              # only the server recovers work a crashed run left behind
    hb = Heartbeat(org)
    hb.start()
    print(f"agents.org running: {shown}")
    print(f"runs only your commands and schedules (commands to {org.c.budget['manual_max_percent']}% of plan usage, "
          f"schedules to {org.budget.auto_percent():.0f}%)")
    if not args.no_browser and not args.log:
        webbrowser.open(url)
    try:
        uvicorn.run(create_app(org, hb), host=args.host, port=port, log_level="warning")
    finally:
        hb.stop()
        org.shutdown()                         # kill running claude processes, hand their tasks back


def _print_tasks(tasks):
    for t in tasks:
        print(f"\n[{t['dept']}/{t['member']}] #{t['id']} {t['status']} · {t['model'] or '-'} · {t['tokens'] or 0} tok")
        print(t["result"] or t["error"] or "")


def cmd_ceo(args):
    org = _org(args)
    r = org.ceo(" ".join(args.text))
    if r.get("error"):
        sys.exit(r["error"])
    if r.get("answer"):
        print(f"CEO: {r['answer']}")
    for a in r.get("actions") or []:
        print(f"  {a}")
    print(f"routed ({r['routing']}, {r['tokens']} tok) -> tasks {r['tasks']}")
    _print_tasks(org.wait(r["tasks"]))


def cmd_ask(args):
    """A quick question about the company: answered from what it knows; nothing starts, nothing is sent."""
    from . import ask
    org = _org(args)
    r = ask.ask(org, " ".join(args.text))
    print(r["answer"])
    if args.outbox and r.get("draft"):
        print(f"\n(put in the Outbox as #{ask.to_outbox(org, r['answer'])}, not sent)")


def cmd_dept(args):
    org = _org(args)
    tid = org.submit(args.dept, " ".join(args.text), member=args.member, source="dept", backlog=args.backlog, owner=True)
    if args.backlog:
        print(f"queued to {args.dept} backlog: #{tid}")
        return
    _print_tasks(org.wait([tid]))


def cmd_tick(args):
    from .heartbeat import Heartbeat
    print(Heartbeat(_org(args)).tick_now())


def cmd_usage(args):
    org = _org(args)
    snap = org.budget.snapshot()
    if snap["windows"]:
        print(" | ".join(f"{k}: {v['utilization']:.0f}% (resets {v['resets_at'][:16]})" for k, v in snap["windows"].items())
              + (f"   (reading {snap['quota_age'] // 60} min old: {snap['quota_reason']})" if snap.get("quota_stale") else ""))
    else:
        print(f"quota: not read ({snap.get('quota_reason') or 'unknown'}); using daily call caps "
              f"({snap['calls_today']} calls today)")
    rows = org.s.q("SELECT model, COUNT(*) n, SUM(input+output+cache_create) t, SUM(cache_read) cr FROM usage "
                   "WHERE day=date('now','localtime') GROUP BY model")
    print("today:" if rows else "today: no calls")
    for r in rows:
        print(f"  {r['model']:<28} calls {r['n']:<4} tokens {r['t'] or 0:<9,} cache-read {r['cr'] or 0:,}")


def cmd_env(args):
    org = _org(args)
    v = org.env
    depts = [d.strip() for d in (args.dept or "").split(",") if d.strip()] or None
    for d in depts or []:
        if d not in org.c.depts:
            raise SystemExit(f"unknown department {d!r} (have: {', '.join(org.c.depts)})")
    if args.action == "list":
        keys = v.public()
        if not keys:
            print("No keys yet. Add one: agentsorg env set RESEND_API_KEY --dept sales --note \"send email\"")
        for k, e in keys.items():
            who = "every Claude call (login)" if e["claude"] else ", ".join(e["depts"]) or "all departments"
            print(f"  {k:<28} {e['masked']:<12} {who}" + (f"  · {e['note']}" if e["note"] else ""))
        print("Values are never shown again. Agents with shell tools get them as environment variables.")
    elif args.action == "set":
        if not args.name:
            raise SystemExit("usage: agentsorg env set NAME [--value V] [--dept a,b] [--note text]")
        value = args.value
        if value is None and args.name.upper() not in v.public():
            import getpass
            value = getpass.getpass(f"value for {args.name.upper()} (hidden): ")
        e = v.set(args.name, value, depts=depts, note=args.note)
        print(f"saved {args.name.upper()} ({e['masked']})")
    elif args.action == "rm":
        print("removed" if v.remove(args.name or "") else "no such key")
    elif args.action == "import":
        text = Path(args.name).expanduser().read_text(encoding="utf-8") if args.name else sys.stdin.read()
        print("imported: " + (", ".join(v.import_dotenv(text, depts)) or "nothing"))


def cmd_routines(args):
    org = _org(args)
    R = org.routines
    if args.action == "add":
        if not (args.dept and args.text and args.every):
            raise SystemExit('usage: agentsorg routines add --dept sales --every "mon 09:00" --text "pipeline review"')
        r = R.add(args.dept, args.text, args.every)
        print(f"added {r['id']}: {r['every']} {r['dept']}: {r['text']}")
    elif args.action in ("rm", "run", "pause", "resume"):
        if not args.id:
            raise SystemExit(f"usage: agentsorg routines {args.action} <id>")
        if args.action == "rm":
            print("removed" if R.remove(args.id) else "no such routine")
        elif args.action == "run":
            print(f"queued task #{R.run_now(args.id)}")
        else:
            R.update(args.id, enabled=args.action == "resume")
            print(f"{args.action}d {args.id}")
    else:
        rows = R.overview()
        if not rows:
            print('No routines. Add one: agentsorg routines add --dept sales --every "mon 09:00" --text "..."')
        for r in rows:
            print(f"  {r['id']}  {r['every']:<16} {r['dept']:<12} {'next ' + r['next'] if r['enabled'] else 'paused':<22} {r['text'][:70]}")


def cmd_projects(args):
    org = _org(args)
    P = org.projects
    if args.action == "list" or not args.id:
        for p in P.list(limit=30):
            print(f"  #{p['id']:<4} {p['status']:<10} {p['tasks_done']}/{p['tasks']} tasks  {p['title']}")
        return
    pid = int(args.id)
    p = P.get(pid)
    if not p:
        raise SystemExit(f"no project #{pid}")
    if args.action == "approve":
        print(f"started {P.approve_plan(pid)} task(s)")
    elif args.action == "accept":
        for n in P.accept(pid):
            print(f"  {n}")
        print("accepted" if P.get(pid)["status"] == "done" else "not accepted: fix the merge above, then try again")
    elif args.action == "discard":
        P.discard(pid)
        print("discarded")
    elif args.action == "cancel":
        print(f"cancelled {P.cancel(pid)} task(s)")
    elif args.action == "revise":
        if not args.notes:
            raise SystemExit('usage: agentsorg projects revise <id> "what to change"')
        print(f"revising as task #{P.revise(pid, ' '.join(args.notes))}")
    else:
        print(f"#{pid} {p['title']}  [{p['status']}]")
        for t in p["tasks"]:
            print(f"  #{t['id']:<4} {t['dept']:<12} {t['status']:<9} {t['title'][:70]}")
        if p.get("result"):
            print("\n" + p["result"])


def cmd_backup(args):
    from . import maintenance
    if args.action == "restore":
        home = _home(args)
        if not args.file:
            raise SystemExit("usage: agentsorg backup restore <file>   (stop the service first)")
        src = Path(args.file).expanduser()
        if not src.is_file():
            raise SystemExit(f"{src} not found")
        from .config import Company
        co = Company(home)
        if _port_owner(co.port, co.token()) == "this company":
            raise SystemExit("The company is running. Stop it first (agentsorg service stop, or close `agentsorg start`).")
        aside = maintenance.restore(home, src)
        print(f"restored {src.name}. The previous database is kept as {aside.name}")
        return
    org = _org(args)
    if args.action == "list":
        for f in sorted(maintenance.backup_dir(org).glob("org-*.db")):
            print(f"  {f.name}  {f.stat().st_size // 1024} KB")
        return
    print(f"backup written: {maintenance.backup(org)}")


def cmd_abilities(args):
    from .agents import ABILITIES, abilities_of
    org = _org(args)
    if args.dept:
        if args.dept not in org.c.depts:
            raise SystemExit(f"unknown department {args.dept!r} (have: {', '.join(org.c.depts)})")
        if args.set is not None:
            want = [a.strip() for a in args.set.split(",") if a.strip()]
            try:
                org.set_abilities(args.dept, want, accept_unsandboxed=args.accept_unsandboxed)
            except ValueError as e:
                raise SystemExit(str(e))
            except PermissionError as e:
                raise SystemExit(f"{e}\nTo accept: agentsorg abilities {args.dept} --set {args.set} --accept-unsandboxed")
    for d in org.c.depts.values():
        if not args.dept or d.id == args.dept:
            print(f"  {d.id:<14} {', '.join(abilities_of(d.tools)) or 'text only'}")
    print("Abilities: " + " · ".join(f"{k} = {label}" for k, (label, _) in ABILITIES.items()))


def cmd_metric(args):
    org = _org(args)
    M = org.metrics
    if args.rm:
        print("removed" if M.remove(args.name) else "no such number")
    elif args.name:
        M.set(args.name, args.value, target=args.target, unit=args.unit)
    rows = M.all()
    for r in rows:
        when = time.strftime("%d %b %H:%M", time.localtime(r["updated"]))
        print(f"  {M.fmt(r):<40} {when} by {r['source']}")
    if not rows:
        print("No numbers yet. Track one: agentsorg metric waitlist_signups 12 --target 50")


def cmd_goals(args):
    org = _org(args)
    from .metrics import objectives, objective_line
    goals = [o if len(o) > 1 else o["text"] for o in org.c.objectives]      # objectives keep their numbers
    if args.add:
        o = {"text": args.add.strip()}
        if getattr(args, "metric", None):
            o["metric"] = args.metric
        if getattr(args, "target", None) is not None:
            o["target"] = args.target
        if getattr(args, "due", None):
            o["due"] = args.due
        goals.append(o if len(o) > 1 else o["text"])
    if args.rm:
        goals = [g for i, g in enumerate(goals, 1)
                 if str(i) != args.rm and (g["text"] if isinstance(g, dict) else g) != args.rm]
    if args.add or args.rm:
        org.c.raw["goals"] = goals
        org.c.save()
    for i, o in enumerate(objectives(org), 1):
        print(f"  {i}. {objective_line(o)}")
    if not org.c.goals:
        print('No goals. Add one: agentsorg goals --add "10 paying customers by 31 Dec"')


def cmd_kb(args):
    org = _org(args)
    if args.add:
        title, _, body = args.add.partition("|")
        print(f"added #{org.kb.add(title, body or title)}")
        return
    for k in (org.kb.search(args.query) if args.query else org.kb.all(30)):
        print(f"#{k['id']} [{k['dept'] or '-'}] {k['title']}: {k['body'][:120]}")


def cmd_info(args):
    import json
    import httpx
    org = _org(args)
    url, tok = f"http://127.0.0.1:{org.c.port}", org.c.token()
    try:
        running = httpx.get(f"{url}/api/state", headers={"X-Token": tok}, timeout=3).status_code == 200
    except httpx.HTTPError:
        running = False
    info = {"name": org.c.slug, "company": org.c.name, "home": str(org.home), "port": org.c.port, "token": tok,
            "running": running}
    if getattr(args, "json", False):
        print(json.dumps(info))
    else:
        print(f"{info['name']} ({info['company']}) at {info['home']}\n"
              f"port {info['port']} · {'running' if running else 'not running'}\ndashboard: {url}/?token={tok}\n"
              f"from another PC: ssh -L {org.c.port}:127.0.0.1:{org.c.port} <you>@<this machine>, then open the "
              f"dashboard link above")
    return info


def cmd_home(args):
    """Bare `agentsorg`: where the company is, its dashboard link, and what needs you. In a terminal (for example
    `ssh -L 5556:127.0.0.1:5556 you@server -t agentsorg`) it stays open, so the SSH tunnel to the dashboard stays
    up, and shows a short status line every minute. Ctrl+C closes it."""
    import httpx
    args.json = False
    info = cmd_info(args)
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return
    url, tok = f"http://127.0.0.1:{info['port']}", info["token"]
    print("\nKeep this window open while you use the dashboard (it holds the SSH tunnel). Ctrl+C closes it.")
    last = ""
    try:
        while True:
            try:
                st = httpx.get(f"{url}/api/state", headers={"X-Token": tok}, timeout=5).json()
                line = (f"needs you {st.get('inbox', 0)} · working on {st.get('running', 0)} task(s)"
                        + (f" · {st['waiting_budget']} waiting for plan usage" if st.get("waiting_budget") else ""))
            except (httpx.HTTPError, ValueError):
                line = "the company isn't running: start it with `agentsorg service install` or `agentsorg start`"
            if line != last:
                print(f"{time.strftime('%H:%M')}  {line}", flush=True)
                last = line
            time.sleep(60)
    except KeyboardInterrupt:
        print()


def cmd_companies(args):
    """Companies on this machine: list | default <name> | forget <name>."""
    import httpx
    import yaml
    from . import service
    reg = load_registry()
    a, rest = args.action, args.rest
    if a in ("default", "forget"):
        if not rest or rest[0] not in reg["companies"]:
            sys.exit(f"usage: agentsorg companies {a} <name>   (known: {', '.join(reg['companies']) or 'none'})")
        if a == "default":
            reg["default"] = rest[0]
            print(f"default company: {rest[0]} (used when you're not inside a company folder)")
        else:
            reg["companies"].pop(rest[0])
            reg["default"] = reg["default"] if reg["default"] in reg["companies"] else next(iter(reg["companies"]), "")
            print(f"forgot {rest[0]} (its folder is untouched; `agentsorg init <folder>` registers it again)")
        save_registry(reg)
        return
    if not reg["companies"]:
        print("No companies yet. Create one: agentsorg init ~/my-company")
        return
    for n, v in reg["companies"].items():
        home = Path(v["path"])
        try:
            port = int((yaml.safe_load((home / "company.yaml").read_text(encoding="utf-8")) or {}).get("port", 5556))
            tok = (home / "data" / "token").read_text().strip()
            up = httpx.get(f"http://127.0.0.1:{port}/api/state", headers={"X-Token": tok}, timeout=2).status_code == 200
        except Exception:
            port, up = "?", False
        svc = "service" if service.is_installed(n) else "no service"
        print(f"  {n:<22} port {port:<6} {'running' if up else 'stopped':<8} {svc:<11} {home}"
              f"{'   (default)' if n == reg['default'] else ''}")


def cmd_service(args):
    from . import service
    org = _org(args)
    name = org.c.slug
    if args.action == "install":
        print(service.install(org.home, name, args.port or org.c.port, args.host))
    elif args.action == "show":
        print(service.show(org.home, name, args.port or org.c.port, args.host))
    elif args.action == "uninstall":
        print(service.uninstall(name))
    elif args.action == "restart":
        print(service.restart(name))
    elif args.action == "logs":
        log = org.home / "data" / "server.log"
        print(log.read_text(encoding="utf-8", errors="replace")[-4000:] if log.exists() else "no log yet")
    else:
        print(service.status(name))


def cmd_update(args):
    from . import update
    r = update.check()
    if r.get("error"):
        print(r["error"])
        return 1
    if not r["newer"]:
        print(f"agents.org {r['current']} is the latest version.")
        return 0
    print(f"agents.org {r['latest']} is out (you have {r['current']}).")
    if args.check:
        print("Run `agentsorg update` to install it. Your companies, clients and history are kept.")
        return 0
    rc = update.run_here()
    if rc == -1:
        print("The installer opened in a new window. Running company services restart by themselves;\n"
              "if you started one with `agentsorg start`, start it again when the installer is done.")
        return 0
    if rc == 0:
        print("If you started a company with `agentsorg start` (not as a service), start it again to use the new version.")
    return rc


def main(argv=None):
    p = argparse.ArgumentParser(prog="agentsorg", description="Token-efficient virtual company on Claude Code.")
    p.add_argument("--home", help="company folder (default: the company folder you're in, else the default company)")
    p.add_argument("--company", help="company name on this machine (see `agentsorg companies`)")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("init", help="create a company"); s.add_argument("dir", nargs="?")
    s.add_argument("--force", action="store_true", help="overwrite prompts/skills/yaml with defaults")
    s.add_argument("--describe", help="what your company should do; designs the leanest office for it")
    s.add_argument("--yes", "-y", action="store_true", help="apply the design without asking")
    s.add_argument("--default", action="store_true", help="make it the default company on this machine")
    s.add_argument("--scan", action="append", metavar="[LABEL=]FOLDER",
                   help="add an outline of a local folder to the description (repeatable)")
    s.add_argument("--project", action="append", metavar="FOLDER",
                   help="an existing project on this machine: linked live, its CLAUDE.md/README/manifests read once "
                        "into a context pack every agent starts from (repeatable)"); s.set_defaults(f=cmd_init)
    s = sub.add_parser("context", help="the project context pack agents start from: show | build")
    s.add_argument("action", choices=["show", "build"], nargs="?", default="show"); s.set_defaults(f=cmd_context)
    s = sub.add_parser("companies", help="companies on this machine: list | default <name> | forget <name>")
    s.add_argument("action", nargs="?", default="list", choices=["list", "default", "forget"])
    s.add_argument("rest", nargs="*"); s.set_defaults(f=cmd_companies)
    s = sub.add_parser("design", help="(re)design the office from a description of your needs and folders")
    s.add_argument("text", nargs="*", default=[]); s.add_argument("--yes", "-y", action="store_true")
    s.add_argument("--scan", action="append", metavar="[LABEL=]FOLDER",
                   help="add an outline of a local folder to the description (repeatable)"); s.set_defaults(f=cmd_design)
    s = sub.add_parser("check", help="verify CLI, login, quota"); s.set_defaults(f=cmd_check)
    s = sub.add_parser("files", help="department folders and folders linked from other PCs (synced over ssh)")
    s.add_argument("action", nargs="?", default="list",
                   choices=["list", "add", "commands", "status", "receive", "send", "remove", "outputs"])
    s.add_argument("rest", nargs="*", help="linked folder name (or dept for outputs)")
    s.add_argument("--dept"); s.add_argument("--origin", help='where it lives, e.g. "PC1 C:\\Users\\me\\app"')
    s.add_argument("--server", default="home", help="how PCs reach this machine over ssh (default: home)")
    s.add_argument("--folder", help="the folder path on the PC, for the printed commands")
    s.add_argument("--all", action="store_true", help="send: every file, not only the agents' changes")
    s.add_argument("--mode", choices=["direct", "sync"], help="add: direct = use a folder on this machine in place "
                   "(default when it exists here); sync = keep a copy")
    s.add_argument("--writable", action="store_true", help="add: let agents edit a direct (live) folder")
    s.add_argument("--from", dest="from_dir", help="receive: read straight from a folder on this machine")
    s.add_argument("--to", dest="to_dir", help="send: write the agents' changes into a folder on this machine")
    s.add_argument("--file", help="receive/send: use this tar.gz file instead of stdin/stdout (for scp on Windows)")
    s.add_argument("--delete-files", action="store_true", help="remove: also delete the server copy")
    s.set_defaults(f=cmd_files)
    s = sub.add_parser("services", help="the company's services on this machine: status, start/stop/restart, logs")
    s.add_argument("action", nargs="?", default="list",
                   choices=["list", "discover", "add", "remove", "start", "stop", "restart", "logs", "permissions"])
    s.add_argument("rest", nargs="*")
    s.add_argument("--add", action="store_true", help="discover: add everything found")
    s.add_argument("--path", action="append", help="discover: also match units that use this folder")
    s.add_argument("--keyword", action="append", help="discover: also match unit names containing this")
    s.add_argument("--kind", default="systemd", choices=["systemd", "docker"]); s.add_argument("--unit")
    s.add_argument("--scope", default="system", choices=["system", "user"]); s.add_argument("--container")
    s.add_argument("--dept"); s.add_argument("--about"); s.add_argument("--lines", type=int, default=80)
    s.set_defaults(f=cmd_services)
    s = sub.add_parser("start", help="dashboard + heartbeat"); s.add_argument("--port", type=int)
    s.add_argument("--host", default="127.0.0.1"); s.add_argument("--no-browser", action="store_true")
    s.add_argument("--insecure", action="store_true", help="allow listening on a non-local address (plain HTTP)")
    s.add_argument("--log", help="write output to this file (used by the service)"); s.set_defaults(f=cmd_start)
    s = sub.add_parser("service", help="run as a background service (systemd / launchd / Windows Task Scheduler)")
    s.add_argument("action", choices=["install", "uninstall", "status", "restart", "logs", "show"], nargs="?", default="status")
    s.add_argument("--port", type=int); s.add_argument("--host", default="127.0.0.1"); s.set_defaults(f=cmd_service)
    s = sub.add_parser("update", help="install the newest agents.org (keeps your companies and history)")
    s.add_argument("--check", action="store_true", help="only say whether a newer version is out")
    s.set_defaults(f=cmd_update)
    s = sub.add_parser("info", help="where the company lives, its port and whether it is running")
    s.add_argument("--json", action="store_true"); s.set_defaults(f=cmd_info)
    s = sub.add_parser("ask", help="ask about your company: prices, a client, what to reply (no work started, nothing sent)")
    s.add_argument("text", nargs="+"); s.add_argument("--outbox", action="store_true", help="put a drafted reply in the Outbox")
    s.set_defaults(f=cmd_ask)
    s = sub.add_parser("ceo", help="command the company as CEO"); s.add_argument("text", nargs="+"); s.set_defaults(f=cmd_ceo)
    s = sub.add_parser("dept", help="command one department"); s.add_argument("dept"); s.add_argument("text", nargs="+")
    s.add_argument("--member"); s.add_argument("--backlog", action="store_true", help="park it in the backlog (runs only when you start it)"); s.set_defaults(f=cmd_dept)
    s = sub.add_parser("tick", help="run one heartbeat tick now"); s.set_defaults(f=cmd_tick)
    s = sub.add_parser("usage", help="quota + today's tokens by model"); s.set_defaults(f=cmd_usage)
    s = sub.add_parser("env", help="keys and tokens the agents may use (API keys, webhooks, Claude token)")
    s.add_argument("action", choices=["list", "set", "rm", "import"], nargs="?", default="list")
    s.add_argument("name", nargs="?", help="key name (or a .env file for import)")
    s.add_argument("--value", help="the value (omit to type it hidden)")
    s.add_argument("--dept", help="only these departments, comma separated (default: all)")
    s.add_argument("--note", help="what it's for; agents see this, never the value"); s.set_defaults(f=cmd_env)
    s = sub.add_parser("routines", help="work that repeats on a schedule (weekly reviews, daily checks)")
    s.add_argument("action", choices=["list", "add", "rm", "run", "pause", "resume"], nargs="?", default="list")
    s.add_argument("id", nargs="?")
    s.add_argument("--dept"); s.add_argument("--text")
    s.add_argument("--every", help="hourly · daily 09:00 · weekdays 09:00 · mon 09:00 · mon,thu 17:30")
    s.set_defaults(f=cmd_routines)
    s = sub.add_parser("projects", help="what your commands became: list | show | accept | revise | discard | cancel")
    s.add_argument("action", choices=["list", "show", "approve", "accept", "revise", "discard", "cancel"], nargs="?", default="list")
    s.add_argument("id", nargs="?"); s.add_argument("notes", nargs="*"); s.set_defaults(f=cmd_projects)
    s = sub.add_parser("backup", help="back up the company database (daily backups run by themselves)")
    s.add_argument("action", choices=["now", "list", "restore"], nargs="?", default="now")
    s.add_argument("file", nargs="?"); s.set_defaults(f=cmd_backup)
    s = sub.add_parser("abilities", help="what each department may do: read, edit, web, shell")
    s.add_argument("dept", nargs="?"); s.add_argument("--set", help='comma list, e.g. "read,web" ("" = text only)')
    s.add_argument("--accept-unsandboxed", action="store_true",
                   help="allow shell where no sandbox is available (agents can then run any command as you)")
    s.set_defaults(f=cmd_abilities)
    s = sub.add_parser("metric", help="company numbers with targets (waitlist signups, MRR...)")
    s.add_argument("name", nargs="?"); s.add_argument("value", nargs="?", type=float)
    s.add_argument("--target", type=float); s.add_argument("--unit"); s.add_argument("--rm", action="store_true")
    s.set_defaults(f=cmd_metric)
    s = sub.add_parser("goals", help="company goals every agent works toward")
    s.add_argument("--add"); s.add_argument("--rm", help="number or exact text")
    s.add_argument("--metric", help="the number that measures it, e.g. customers")
    s.add_argument("--target", type=float); s.add_argument("--due", help="YYYY-MM-DD"); s.set_defaults(f=cmd_goals)
    s = sub.add_parser("business", help="the business profile every agent works from (customer, offer, price, stage)")
    s.add_argument("--set", action="append", metavar="FIELD=VALUE"); s.set_defaults(f=cmd_business)
    s = sub.add_parser("org", help="who does what now, and which teams come later; `org reorganise` for an existing company")
    s.add_argument("action", nargs="?", choices=["show", "reorganise"], default="show"); s.set_defaults(f=cmd_org)
    s = sub.add_parser("clients", help="the pipeline: list, import a CSV, add a contact, record revenue")
    s.add_argument("action", nargs="?", choices=["list", "import", "add", "revenue"]); s.add_argument("args", nargs="*")
    s.add_argument("--name"); s.add_argument("--org"); s.add_argument("--stage"); s.add_argument("--currency")
    s.add_argument("--recurring", action="store_true"); s.set_defaults(f=cmd_clients)
    s = sub.add_parser("log", help="activity log: what you asked, what the company understood, what each team did")
    s.add_argument("--hours", type=float, default=24); s.add_argument("--who", choices=["owner", "ceo", "agent", "system"])
    s.add_argument("--project", type=int); s.add_argument("--grep"); s.add_argument("--limit", type=int, default=200)
    s.add_argument("--json", action="store_true"); s.set_defaults(f=cmd_log)
    s = sub.add_parser("kb", help="search/add shared knowledge"); s.add_argument("query", nargs="?")
    s.add_argument("--add", help='"title | fact"'); s.set_defaults(f=cmd_kb)

    args = p.parse_args(argv)
    if not args.cmd:            # bare `agentsorg` = where the company is and whether it runs
        args.f = cmd_home
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    rc = args.f(args)
    if type(rc) is int and rc:  # commands that report failure as an exit code (e.g. update)
        sys.exit(rc)


if __name__ == "__main__":
    main()
