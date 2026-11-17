"""FastAPI JSON API + static dashboard. Token-protected (header X-Token or ?token=)."""
from __future__ import annotations

import json
import logging
import re
import secrets
import time
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import outbox_send
from .agents import ABILITIES, abilities_of
from .config import PKG_DATA
from .fsutil import atomic_write
from .office import member_report, office_state

from .state import today

WEB = PKG_DATA / "web"
log = logging.getLogger("agentsorg")


class Cmd(BaseModel):
    text: str
    member: str | None = None
    tier: int | None = None
    backlog: bool = False


class KBItem(BaseModel):
    title: str
    body: str
    dept: str = ""
    tags: str = ""


class MemberIn(BaseModel):
    id: str
    name: str
    role: str
    tier: int = 2
    max_tier: int = 2


class Settings(BaseModel):
    auto: bool | None = None
    paused: bool | None = None
    auto_max_percent: float | None = None


class EnvSet(BaseModel):
    value: str | None = None
    depts: list[str] | None = None
    note: str | None = None


class EnvImport(BaseModel):
    text: str
    depts: list[str] | None = None


class RoutineIn(BaseModel):
    dept: str | None = None
    text: str | None = None
    every: str | None = None
    member: str | None = None
    enabled: bool | None = None


class GoalsIn(BaseModel):
    goals: list          # a line of text, or an objective {text, metric, target, due, dept}


class ReviseIn(BaseModel):
    notes: str
    task_id: int | None = None


class MeetIn(BaseModel):
    question: str
    depts: list[str] = []


class DecideIn(BaseModel):
    choice: int | None = None
    note: str = ""


class AskIn(BaseModel):
    question: str


class AbilitiesIn(BaseModel):
    abilities: list[str]
    accept_unsandboxed: bool = False


class MetricIn(BaseModel):
    value: float | None = None
    target: float | None = None
    unit: str | None = None


class OutboxEdit(BaseModel):
    body: str


def create_app(org, heartbeat=None) -> FastAPI:
    app = FastAPI(title="agents.org", docs_url=None, redoc_url=None)
    token = org.c.token()

    def auth(request: Request):
        got = request.headers.get("x-token") or request.query_params.get("token") or ""
        if not secrets.compare_digest(got.encode("utf-8", "replace"), token.encode()):
            raise HTTPException(401, "bad token")

    A = [Depends(auth)]

    from . import activity

    class OwnerLog:
        """Every change the owner makes from the dashboard goes to the activity log (secrets removed)."""
        def __init__(self, inner):
            self.inner = inner

        async def __call__(self, scope, receive, send):
            if scope["type"] != "http" or scope["method"] not in ("POST", "PUT", "PATCH", "DELETE") \
                    or not scope["path"].startswith("/api/"):
                return await self.inner(scope, receive, send)
            chunks, status = [], {"code": 0}

            async def rec():
                m = await receive()
                if m["type"] == "http.request":
                    chunks.append(m.get("body") or b"")
                return m

            async def snd(m):
                if m["type"] == "http.response.start":
                    status["code"] = m["status"]
                await send(m)
            try:
                await self.inner(scope, rec, snd)
            finally:
                if status["code"] != 401:
                    activity.owner_api(org, scope["method"], scope["path"], b"".join(chunks)[:20000], status["code"])

    app.add_middleware(OwnerLog)

    @app.get("/")
    def index():
        return FileResponse(WEB / "index.html")

    app.mount("/static", StaticFiles(directory=str(WEB)), name="static")

    @app.get("/api/state", dependencies=A)
    def state():
        return {
            "company": org.c.name,
            "about": org.c.about,
            "designed": bool(org.s.get("designed", False)) or bool(org.s.one("SELECT 1 x FROM tasks LIMIT 1")),
            "models": org.c.models,
            "depts": [{"id": d.id, "name": d.name, "mission": d.mission, "tools": d.tools, "functions": d.functions,
                       "abilities": abilities_of(d.tools), "keys": [k for k, _ in org.env.names_for(d.id)],
                       "members": [m.__dict__ for m in d.members],
                       "counts": {r["status"]: r["n"] for r in org.s.q(
                           "SELECT status, COUNT(*) n FROM tasks WHERE dept=? GROUP BY status", (d.id,))}}
                      for d in org.c.depts.values()],
            "budget": org.budget.snapshot(),
            "heartbeat": org.s.get("heartbeat", "not run yet"),
            "cli": bool(org.exe),
            "outbox_pending": org.s.one("SELECT COUNT(*) n FROM outbox WHERE status='pending'")["n"],
            "backlog": org.s.one("SELECT COUNT(*) n FROM tasks WHERE status='backlog'")["n"],
            "inbox": len(__import__("agentsorg.inbox", fromlist=["all_items"]).all_items(org)),
            "running": org.s.one("SELECT COUNT(*) n FROM tasks WHERE status IN ('running','queued','waiting') "
                                 "AND dept!='ceo'")["n"],
            "waiting_budget": org.s.one("SELECT COUNT(*) n FROM tasks WHERE status IN ('queued_blocked',"
                                        "'routine_blocked')")["n"] + org.s.one(
                "SELECT COUNT(*) n FROM projects WHERE status='waiting'")["n"],
            "projects_active": org.s.one("SELECT COUNT(*) n FROM projects WHERE status IN "
                                         "('planning','running','combining','meeting','awaiting','waiting')")["n"],
            "sandbox": org.sandbox,
            "context": bool(org.intake.pack_path().exists()),
            "goals": len(org.c.goals),
            "stage": org.c.business.get("stage", ""),
            "business": bool(org.c.business),
            "clients": sum(org.clients.counts().values()),
        }

    @app.post("/api/ask", dependencies=A)
    def ask_q(body: dict):
        """A quick question about the company: one short call, no work started, nothing sent."""
        from . import ask
        try:
            return ask.ask(org, str(body.get("question") or ""), body.get("history") or [])
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/ask/outbox", dependencies=A)
    def ask_to_outbox(body: dict):
        from . import ask
        try:
            return {"outbox": ask.to_outbox(org, str(body.get("text") or ""))}
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/ceo", dependencies=A)
    def ceo(c: Cmd):
        if c.backlog:
            return {"tasks": [org.submit(org._guess_dept(c.text), c.text, source="ceo", backlog=True, priority=2, owner=True)]}
        return org.ceo(c.text)

    @app.post("/api/dept/{dept}", dependencies=A)
    def dept_cmd(dept: str, c: Cmd):
        if dept not in org.c.depts:
            raise HTTPException(404, "no such dept")
        return {"tasks": [org.submit(dept, c.text, member=c.member, tier=c.tier, source="dept",
                                     backlog=c.backlog, priority=2 if c.backlog else 3, owner=True)]}

    @app.post("/api/dept/{dept}/delegate", dependencies=A)
    def delegate(dept: str, c: Cmd):
        if dept not in org.c.depts:
            raise HTTPException(404, "no such dept")
        return org.delegate(dept, c.text)

    @app.post("/api/design/questions", dependencies=A)
    def design_questions(c: Cmd):
        from . import design
        return {"questions": design.questions(c.text, org.intake.design_brief())}

    @app.post("/api/design", dependencies=A)
    def design_propose(body: dict):
        from . import design
        text = design.with_answers(str(body.get("text") or ""), body.get("answers") or {})
        if not text.strip():
            raise HTTPException(400, "describe what the company should do")
        try:
            return design.propose(org, text)
        except (RuntimeError, ValueError) as e:
            raise HTTPException(400, f"The designer couldn't finish ({e}). Nothing was changed; try again.")

    @app.post("/api/design/apply", dependencies=A)
    def design_apply(body: dict):
        from . import design
        try:
            linked = design.apply(org, body.get("plan") or {}, body.get("description", ""), body.get("answers") or {})
        except (ValueError, TypeError, KeyError) as e:
            raise HTTPException(400, str(e))
        return {"ok": True, "linked": linked}

    @app.post("/api/design/skip", dependencies=A)
    def design_skip(body: dict | None = None):
        """Start without describing: a lean 3-department office (product, growth, operations), not the big template."""
        from . import design
        if (body or {}).get("lean", True) and not org.s.q("SELECT id FROM tasks LIMIT 1"):
            design.apply(org, {**design.LEAN, "name": org.c.name, "about": org.c.about})
        org.s.set("designed", True)
        return {"ok": True}

    # inbox: everything that waits for the owner
    @app.get("/api/inbox", dependencies=A)
    def inbox_get():
        from . import inbox
        return {"items": inbox.items(org)}

    @app.post("/api/inbox/heartbeat/dismiss", dependencies=A)
    def inbox_hb_dismiss():
        org.s.set("heartbeat_error", None)
        return {"ok": True}

    # company settings in plain words
    SETTINGS = {"report_time": ("time", None), "weekly_review_day": ("day", None),
                "heartbeat_minutes": ("int", (5, 240)), "concurrency": ("int", (1, 6)),
                "max_chain_depth": ("int", (0, 4)), "backlog_cap": ("int", (1, 200)), "keep_days": ("int", (14, 3650)),
                "task_timeout": ("int", (60, 3600)), "auto_max_percent": ("int", (0, 90)),
                "manual_max_percent": ("int", (10, 100)), "quiet_hours": ("list", None)}

    @app.get("/api/company/settings", dependencies=A)
    def company_settings():
        b = org.c.budget
        return {"report_time": org.c.report_time, "weekly_review_day": org.c.raw.get("weekly_review_day", "mon"),
                **{k: b.get(k) for k in SETTINGS if k in b or k in ("backlog_cap", "keep_days", "task_timeout")},
                "backlog_cap": b.get("backlog_cap", 20), "keep_days": b.get("keep_days", 90),
                "task_timeout": b.get("task_timeout", 900), "name": org.c.name, "about": org.c.about}

    @app.put("/api/company/settings", dependencies=A)
    def company_settings_put(body: dict):
        from .heartbeat import parse_hhmm
        DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
        changes = {}
        for k, v in body.items():
            if k in ("name", "about"):
                changes[k] = str(v).strip()[:40 if k == "name" else 240]
                continue
            if k not in SETTINGS:
                raise HTTPException(400, f"unknown setting {k!r}")
            kind, rng = SETTINGS[k]
            if kind == "time":
                if not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", str(v).strip()):
                    raise HTTPException(400, "time looks like 18:00")
                h, m = parse_hhmm(str(v).strip())
                changes[k] = f"{h:02d}:{m:02d}"
            elif kind == "day":
                if str(v).lower()[:3] not in DAYS:
                    raise HTTPException(400, "day is mon, tue, ... sun")
                changes[k] = str(v).lower()[:3]
            elif kind == "int":
                try:
                    n = int(v)
                except (TypeError, ValueError):
                    raise HTTPException(400, f"{k} must be a whole number")
                if not rng[0] <= n <= rng[1]:
                    raise HTTPException(400, f"{k} must be between {rng[0]} and {rng[1]}")
                changes[k] = n
            elif kind == "list":
                if not isinstance(v, list) or not all(isinstance(x, str) and re.fullmatch(r"\d{1,2}:\d{2}-\d{1,2}:\d{2}", x)
                                                      for x in v):
                    raise HTTPException(400, "quiet hours look like [\"22:00-07:00\"]")
                changes[k] = v
        with org.c.lock:
            raw = org.c.raw
            for k, v in changes.items():
                if k in ("report_time", "weekly_review_day", "name", "about"):
                    raw[k] = v
                else:
                    raw.setdefault("budget", {})[k] = v
            org.c.save()
        org.log_decision("settings", ", ".join(changes), "changed", json.dumps(changes)[:300])
        return company_settings()

    # departments: create / edit / remove
    @app.post("/api/depts", dependencies=A)
    def dept_create(body: dict):
        from . import design
        name = str(body.get("name") or "").strip()
        if not name:
            raise HTTPException(400, "a department needs a name")
        members = body.get("members") or [{"id": "lead", "name": str(body.get("head") or "Lead")[:20],
                                           "role": f"{name} lead", "tier": 1, "max_tier": 2}]
        try:
            did = design.add_dept(org, {"id": body.get("id") or name, "name": name, "mission": body.get("mission", ""),
                                        "members": members, "rules": body.get("instructions", "")})
        except ValueError as e:
            raise HTTPException(400, str(e))
        org.log_decision("department", did, "created", "by you")
        return {"id": did}

    @app.patch("/api/depts/{did}", dependencies=A)
    def dept_update(did: str, body: dict):
        from . import design
        try:
            design.update_dept(org, did, **{k: body.get(k) for k in ("name", "mission", "instructions", "skill")})
        except KeyError as e:
            raise HTTPException(404, str(e))
        return {"ok": True}

    @app.get("/api/depts/{did}/files", dependencies=A)
    def dept_files(did: str):
        if did not in org.c.depts:
            raise HTTPException(404)
        ins, sk = org.c.instructions_path(did), org.c.dept_skills_dir(did) / "domain.md"
        return {"instructions": ins.read_text(encoding="utf-8") if ins.exists() else "",
                "skill": sk.read_text(encoding="utf-8") if sk.exists() else ""}

    @app.delete("/api/depts/{did}", dependencies=A)
    def dept_remove(did: str):
        from . import design
        try:
            return {"archived_as": design.remove_dept(org, did)}
        except KeyError as e:
            raise HTTPException(404, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.get("/api/files", dependencies=A)
    def files_overview(server: str = "home"):
        f = org.files
        by_dept = {d: {"id": d, "name": org.c.depts[d].name, "sources": [], "outputs": f.outputs(d, 12)}
                   for d in org.c.depts}
        for s in f.sources().values():
            if s.dept in by_dept:
                local = f.local_path(s)
                lp = f.link_path(s)
                from .changes import detect_test, is_repo
                path = f.path(s)
                by_dept[s.dept]["sources"].append({**f.status(s.name), "commands": f.commands(s.name, server),
                                                   "local": str(local) if local else "",
                                                   "link": str(lp.relative_to(f.root)) if f._is_link(lp) else "",
                                                   "git": s.mode == "direct" and is_repo(path),
                                                   "test": org.changes.test_command(s.name),
                                                   "test_suggested": detect_test(path) if s.mode == "direct" else "",
                                                   "test_last": org.s.get(f"test:{s.name}")})
        return {"root": str(f.root), "depts": list(by_dept.values())}

    @app.post("/api/files", dependencies=A)
    def files_add(body: dict):
        try:
            s = org.files.add(str(body.get("name", "")), str(body.get("dept", "")), str(body.get("origin", "")))
        except KeyError as e:
            raise HTTPException(400, str(e))
        except FileExistsError as e:
            raise HTTPException(409, str(e))
        return {"name": s.name}

    @app.post("/api/files/{name}/sync", dependencies=A)
    def files_sync(name: str):
        """Copy a linked folder that lives on this machine into the company (originals untouched)."""
        try:
            s = org.files.get(name)
            path = org.files.local_path(s)
            if not path:
                raise HTTPException(400, f"{name} is on another computer: run its push command there")
            rep = org.files.receive_dir(name, path)
        except KeyError as e:
            raise HTTPException(404, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"added": len(rep["added"]), "updated": len(rep["updated"]), "conflicts": rep["conflicts"],
                "deleted": len(rep["deleted"])}

    @app.delete("/api/files/{name}", dependencies=A)
    def files_remove(name: str):
        try:
            org.files.remove(name)
        except KeyError as e:
            raise HTTPException(404, str(e))
        return {"ok": True}

    @app.get("/api/files/{dept}/outputs/{name}", dependencies=A)
    def files_output(dept: str, name: str):
        if dept not in org.c.depts:
            raise HTTPException(404)
        try:
            return {"name": name, "text": org.files.read_output(dept, name)}
        except KeyError:
            raise HTTPException(404, "no such output")

    # services: the company's own systemd units / timers / containers on this machine
    @app.get("/api/services", dependencies=A)
    def services_list():
        sv = org.services
        rows = sv.overview()
        for r in rows:
            r["can_control"] = sv.can_control(sv.get(r["name"]))
        text, install = sv.permissions()
        return {"services": rows, "permissions": {"text": text, "install": install.format(
            file=str(org.home / "data" / "sudoers-services")) if install else ""}}

    @app.post("/api/services/discover", dependencies=A)
    def services_discover(body: dict | None = None):
        body = body or {}
        hints = [str(org.files.path(s)) for s in org.files.sources().values() if s.mode == "direct"]
        hints += [str(p) for p in body.get("paths") or []]
        keywords = list(body.get("keywords") or []) + [w for w in org.c.slug.split("-") if len(w) > 3]
        return {"found": org.services.discover(hints, keywords)}

    @app.post("/api/services", dependencies=A)
    def services_add(body: dict):
        name = str(body.get("name") or "").strip()
        if not name or not (body.get("unit") or body.get("container")):
            raise HTTPException(400, "name and unit (or container) required")
        try:
            s = org.services.add(name, kind=str(body.get("kind", "systemd")), unit=str(body.get("unit", "")),
                                 scope=str(body.get("scope", "system")), container=str(body.get("container", "")),
                                 dept=str(body.get("dept", "")), about=str(body.get("about", ""))[:200])
        except ValueError as e:
            raise HTTPException(400, str(e))
        if body.get("permissions_file") is not False:          # keep the sudoers suggestion current
            text, _ = org.services.permissions()
            if text:
                atomic_write(org.home / "data" / "sudoers-services", text, mode=0o600)
        return {"name": s.name}

    @app.delete("/api/services/{name}", dependencies=A)
    def services_remove(name: str):
        try:
            org.services.remove(name)
        except KeyError as e:
            raise HTTPException(404, str(e))
        return {"ok": True}

    @app.patch("/api/services/{name}", dependencies=A)
    def services_edit(name: str, body: dict):
        try:
            s = org.services.get(name)
        except KeyError as e:
            raise HTTPException(404, str(e))
        org.services.add(name, kind=s.kind, unit=s.unit, scope=s.scope, container=s.container,
                         dept=body.get("dept", s.dept), about=body.get("about", s.about))
        return {"ok": True}

    @app.post("/api/services/{name}/{action}", dependencies=A)
    def services_control(name: str, action: str):
        try:
            ok, msg = org.services.control(org.services.get(name), action)
        except KeyError as e:
            raise HTTPException(404, str(e))
        if not ok:
            raise HTTPException(409, msg)
        return {"ok": True, "message": msg}

    @app.get("/api/services/{name}/logs", dependencies=A)
    def services_logs(name: str, lines: int = 120):
        try:
            return {"name": name, "text": org.services.logs(org.services.get(name), lines)}
        except KeyError as e:
            raise HTTPException(404, str(e))

    @app.get("/api/office", dependencies=A)
    def office():
        return office_state(org)

    @app.get("/api/member/{dept}/{mid}", dependencies=A)
    def member(dept: str, mid: str):
        if dept not in org.c.depts:
            raise HTTPException(404, "no such dept")
        return member_report(org, dept, mid)

    @app.get("/api/tasks", dependencies=A)
    def tasks(dept: str | None = None, limit: int = 60, status: str | None = None):
        sql, args = "SELECT id,created,updated,dept,member,title,source,status,tier,model,done_line,tokens,error," \
                    "depends_on,parent_id FROM tasks WHERE 1=1", []
        if dept:
            sql += " AND dept=?"; args.append(dept)
        if status:
            sql += " AND status=?"; args.append(status)
        sql += " ORDER BY id DESC LIMIT ?"; args.append(max(1, min(limit, 500)))
        return org.s.q(sql, tuple(args))

    @app.get("/api/task/{tid}", dependencies=A)
    def task(tid: int):
        t = org.s.task(tid)
        if not t:
            raise HTTPException(404)
        t["usage"] = org.s.q("SELECT model,input,output,cache_read,cache_create,cost_usd FROM usage WHERE task_id=?",
                             (tid,))
        return t

    @app.post("/api/task/{tid}/retry", dependencies=A)
    def retry(tid: int, c: Cmd | None = None):
        t = org.s.task(tid)
        if not t:
            raise HTTPException(404)
        if t["status"] in ("running", "routing", "queued", "waiting"):
            raise HTTPException(409, "it's already running or waiting to run")
        if t["dept"] not in org.c.depts:
            raise HTTPException(409, "that department no longer exists")
        if t["depends_on"] and (dep := org.s.task(t["depends_on"])) and dep["status"] not in ("done", "cached"):
            raise HTTPException(409, f"it needs task #{dep['id']} first, which is {dep['status']}")
        tier = c.tier if c else None
        n = org.s.xc("UPDATE tasks SET status='queued', tier=?, tier_forced=?, error=NULL, hash=?, source=?, "
                     "updated=? WHERE id=? AND status NOT IN ('running','routing','queued','waiting')",
                     (tier or t["tier"], int(bool(tier)), f"retry-{tid}-{secrets.token_hex(3)}",
                      t["source"] if t["source"] in ("ceo", "dept") else "dept", time.time(), tid))
        if not n:
            raise HTTPException(409, "it changed while you clicked; refresh")
        if t.get("project_id"):                       # its project isn't finished anymore: the result gets rebuilt
            org.s.x("UPDATE projects SET status='running', updated=? WHERE id=? AND status IN ('review','failed')",
                    (time.time(), t["project_id"]))
        org.pump()
        return {"ok": True}

    @app.delete("/api/task/{tid}", dependencies=A)
    def cancel(tid: int):
        if not org.s.task(tid):
            raise HTTPException(404)
        return {"ok": org.cancel(tid)}

    # KB
    @app.get("/api/kb", dependencies=A)
    def kb_list(q: str = ""):
        return org.kb.search(q, 50) if q else org.kb.all()

    @app.post("/api/kb", dependencies=A)
    def kb_add(k: KBItem):
        return {"id": org.kb.add(k.title, k.body, k.dept, k.tags)}

    @app.put("/api/kb/{kid}", dependencies=A)
    def kb_put(kid: int, k: KBItem):
        org.kb.update(kid, title=k.title, body=k.body, dept=k.dept, tags=k.tags)
        return {"ok": True}

    @app.delete("/api/kb/{kid}", dependencies=A)
    def kb_del(kid: int):
        org.kb.delete(kid)
        return {"ok": True}

    @app.get("/api/usage", dependencies=A)
    def usage(days: int = 7):
        return {
            "by_model": org.s.q("SELECT model, COUNT(*) calls, SUM(input) input, SUM(output) output, "
                                "SUM(cache_read) cache_read, SUM(cache_create) cache_create, SUM(cost_usd) cost "
                                "FROM usage WHERE day>=date('now', ?) GROUP BY model", (f"-{days} day",)),
            "by_dept": org.s.q("SELECT dept, COUNT(*) calls, SUM(input+output+cache_create) tokens, SUM(cost_usd) cost, "
                               "SUM(auto) auto_calls FROM usage WHERE day>=date('now', ?) GROUP BY dept",
                               (f"-{days} day",)),
            "by_day": org.s.q("SELECT day, COUNT(*) calls, SUM(input+output+cache_create) tokens FROM usage "
                              "GROUP BY day ORDER BY day DESC LIMIT ?", (days,)),
            "budget": org.budget.snapshot(),
        }

    @app.post("/api/settings", dependencies=A)
    def settings(s: Settings):
        for k, v in s.model_dump(exclude_none=True).items():
            org.s.set(k, v)
        return org.budget.snapshot()

    @app.post("/api/heartbeat", dependencies=A)
    def tick():
        if not heartbeat:
            from .heartbeat import Heartbeat
            return {"msg": Heartbeat(org).tick_now()}
        return {"msg": heartbeat.tick_now()}

    # members
    @app.post("/api/dept/{dept}/members", dependencies=A)
    def add_member(dept: str, m: MemberIn):
        if dept not in org.c.depts:
            raise HTTPException(404, "unknown department")
        mid = re.sub(r"[^a-z0-9_]+", "_", (m.id or m.name or "").lower()).strip("_")[:30]
        if not mid:
            raise HTTPException(400, "member needs an id or a name")
        tier = min(max(int(m.tier or 1), 1), 3)
        org.c.upsert_member(dept, mid, {"name": (m.name or mid.title())[:30], "role": (m.role or "")[:80],
                                        "tier": tier, "max_tier": min(max(int(m.max_tier or tier), tier), 3)})
        org._sys_cache.clear()
        return {"ok": True, "id": mid}

    @app.delete("/api/dept/{dept}/members/{mid}", dependencies=A)
    def del_member(dept: str, mid: str):
        if dept not in org.c.depts:
            raise HTTPException(404, "unknown department")
        if mid not in {x.id for x in org.c.depts[dept].members}:
            raise HTTPException(404, "unknown member")
        if len(org.c.depts[dept].members) <= 1:
            raise HTTPException(400, "a department needs at least one member")
        org.c.remove_member(dept, mid)
        org._sys_cache.clear()
        return {"ok": True}

    # outbox
    @app.get("/api/abilities", dependencies=A)
    def abilities_list():
        return {k: label for k, (label, _) in ABILITIES.items()}

    @app.put("/api/dept/{dept}/abilities", dependencies=A)
    def abilities_set(dept: str, b: AbilitiesIn):
        try:
            return {"abilities": org.set_abilities(dept, b.abilities, b.accept_unsandboxed)}
        except KeyError as e:
            raise HTTPException(404, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))
        except PermissionError as e:
            raise HTTPException(428, str(e))          # the dashboard asks the owner to confirm, then resends

    # goals + routines (recurring work)
    # projects: one command -> one result
    @app.get("/api/projects", dependencies=A)
    def projects_list(status: str | None = None, limit: int = 50):
        return org.projects.list(status, limit)

    @app.get("/api/projects/{pid}", dependencies=A)
    def project_get(pid: int):
        p = org.projects.get(pid)
        if not p:
            raise HTTPException(404)
        p["board"] = org.projects.board(pid)
        p["questions"] = [q for q in p.get("suggestions") or [] if q.get("kind") == "question"]
        return p

    @app.post("/api/projects/{pid}/{action}", dependencies=A)
    def project_act(pid: int, action: str, b: dict | None = None):
        p = org.s.one("SELECT status FROM projects WHERE id=?", (pid,))
        if not p:
            raise HTTPException(404)
        why = str((b or {}).get("why") or "")[:300]
        if action == "approve":                          # a plan held by an older version
            if p["status"] != "awaiting":
                raise HTTPException(409, "this plan isn't waiting for approval")
            return {"started": org.projects.approve_plan(pid, why)}
        if action == "discard":
            if p["status"] != "review":
                raise HTTPException(409, "there's no result to discard")
            org.projects.discard(pid, why or "discarded by you")
            return {"ok": True}
        if action == "cancel":
            if p["status"] in ("done", "cancelled"):
                raise HTTPException(409, f"already {p['status']}")
            return {"cancelled": org.projects.cancel(pid, why or "cancelled by you")}
        if action == "accept":
            if p["status"] != "review":
                raise HTTPException(409, "there's no result to accept yet")
            notes = org.projects.accept(pid, why)
            done = org.s.one("SELECT status FROM projects WHERE id=?", (pid,))["status"] == "done"
            if not done:
                raise HTTPException(409, "Not accepted: " + " ".join(notes))
            return {"ok": True, "merged": notes}
        if action == "retry":
            try:
                org.projects.retry(pid)
            except ValueError as e:
                raise HTTPException(409, str(e))
            return {"ok": True}
        if action == "revise":
            try:
                r = ReviseIn(**(b or {}))
                return {"task": org.projects.revise(pid, r.notes, r.task_id)}
            except KeyError as e:
                raise HTTPException(404, str(e))
            except (ValueError, TypeError) as e:
                raise HTTPException(400, str(e).splitlines()[0])
        raise HTTPException(400, "accept, revise, discard, cancel or retry")

    @app.get("/api/deliverables", dependencies=A)
    def deliverables(q: str = "", dept: str = "", project: int | None = None, final: bool = False, limit: int = 100):
        sql = ("SELECT t.id, t.dept, t.title, t.done_line, t.summary, t.output_path, t.final, t.updated, t.project_id, "
               "t.kind, p.title AS project FROM tasks t LEFT JOIN projects p ON p.id=t.project_id "
               "WHERE t.status IN ('done','cached') AND t.dept!='ceo' AND COALESCE(t.kind,'') != 'question' "
               "AND t.result IS NOT NULL")
        args: list = []
        if q.strip():
            sql += " AND (t.title LIKE ? OR t.result LIKE ?)"
            args += [f"%{q.strip()}%"] * 2
        if dept:
            sql += " AND t.dept=?"; args.append(dept)
        if project:
            sql += " AND t.project_id=?"; args.append(project)
        if final:
            sql += " AND t.final=1"
        sql += " ORDER BY t.final DESC, t.id DESC LIMIT ?"
        args.append(max(1, min(limit, 300)))
        items = org.s.q(sql, tuple(args))
        results = org.s.q("SELECT id, title, final, updated FROM projects WHERE result IS NOT NULL AND status IN "
                          "('review','done') ORDER BY final DESC, id DESC LIMIT 50") if not (dept or q or final) else []
        return {"items": items, "projects": results}

    @app.post("/api/task/{tid}/final", dependencies=A)
    def task_final(tid: int, b: dict | None = None):
        t = org.s.task(tid)
        if not t or t["status"] not in ("done", "cached"):
            raise HTTPException(404, "only finished work can be marked final")
        on = bool((b or {}).get("final", True))
        org.s.update_task(tid, final=int(on))
        org.log_decision("deliverable", f"#{tid} {t['title'][:80]}", "final" if on else "draft", ref=f"task:{tid}")
        return {"final": on}

    @app.post("/api/task/{tid}/outbox", dependencies=A)
    def task_to_outbox(tid: int):
        t = org.s.task(tid)
        if not t or not t.get("result"):
            raise HTTPException(404, "nothing to send")
        oid = org.s.x("INSERT INTO outbox(created,task_id,dept,body,status,project_id) VALUES(?,?,?,?,?,?)",
                      (time.time(), tid, t["dept"], t["result"], "pending", t.get("project_id")))
        return {"id": oid}

    # change requests: the project's branch of a real repo, merged only on the owner's click
    @app.get("/api/changes", dependencies=A)
    def changes_list(project: int | None = None):
        sql, args = "SELECT id,project_id,source,branch,status,files,created,updated FROM changes", ()
        if project:
            sql, args = sql + " WHERE project_id=?", (project,)
        return org.s.q(sql + " ORDER BY id DESC LIMIT 100", args)

    @app.get("/api/changes/{cid}", dependencies=A)
    def change_get(cid: int):
        try:
            return org.changes.diff(cid)
        except KeyError as e:
            raise HTTPException(404, str(e))

    @app.post("/api/changes/{cid}/{action}", dependencies=A)
    def change_act(cid: int, action: str):
        try:
            if action == "merge":
                return {"message": org.changes.merge(cid)}
            if action == "discard":
                org.changes.discard(cid)
                org.log_decision("change", f"change #{cid}", "discarded")
                return {"message": "Thrown away: the branch and its working copy are gone."}
        except KeyError as e:
            raise HTTPException(404, str(e))
        except ValueError as e:
            raise HTTPException(409, str(e))
        raise HTTPException(400, "merge or discard")

    @app.post("/api/projects/{pid}/suggestions/{sid}/{action}", dependencies=A)
    def project_suggestion(pid: int, sid: int, action: str, b: dict | None = None):
        if action not in ("run", "dismiss", "answer"):
            raise HTTPException(400, "run, dismiss or answer")
        try:
            if action == "answer":
                b = b or {}
                return org.projects.answer_suggestion(pid, sid, str(b.get("text") or ""), bool(b.get("save", True)),
                                                      bool(b.get("run", False)))
            return {"task": org.projects.act_suggestion(pid, sid, action)}
        except KeyError as e:
            raise HTTPException(404, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/files/{name}/track", dependencies=A)
    def files_track(name: str):
        try:
            return {"message": org.changes.track(name)}
        except KeyError as e:
            raise HTTPException(404, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.put("/api/files/{name}/test", dependencies=A)
    def files_test_set(name: str, b: dict):
        try:
            org.changes.set_test(name, str(b.get("command") or ""))
        except KeyError as e:
            raise HTTPException(404, str(e))
        return {"command": org.changes.test_command(name)}

    @app.post("/api/files/{name}/test", dependencies=A)
    def files_test_run(name: str):
        if name not in org.files.sources():
            raise HTTPException(404)
        return org.changes.run_tests(name)

    @app.get("/api/decisions", dependencies=A)
    def decisions(limit: int = 100):
        return org.s.q("SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (max(1, min(limit, 500)),))

    @app.get("/api/context", dependencies=A)
    def context_get():
        st = org.intake.status()
        p = org.intake.pack_path()
        st["pack"] = p.read_text(encoding="utf-8") if p.exists() else ""
        return st

    @app.post("/api/context/build", dependencies=A)
    def context_build():
        r = org.intake.build(force=True)
        if r.get("skipped") and "no project" in r["skipped"]:
            raise HTTPException(400, "Link your project folder first (Files page), then read it.")
        return r

    @app.get("/api/goals", dependencies=A)
    def goals_get():
        from .metrics import objectives
        return {"goals": org.c.goals, "objectives": objectives(org)}

    @app.put("/api/goals", dependencies=A)
    def goals_put(b: GoalsIn):
        from .metrics import objectives
        from .config import clean_objective
        old = {o["text"]: o for o in org.c.objectives}
        out = []
        for g in b.goals[:12]:
            o = clean_objective(g)
            if not o:
                continue
            if isinstance(g, str) and o["text"] in old:       # editing the text list keeps each goal's number and date
                o = old[o["text"]]
            if o.get("dept") and o["dept"] not in org.c.depts:
                o.pop("dept")
            out.append(o if len(o) > 1 else o["text"])
            if o.get("metric") and o.get("target") is not None:
                try:
                    org.metrics.set(o["metric"], None, target=o["target"])
                except ValueError:
                    pass
        with org.c.lock:
            org.c.raw["goals"] = out
            org.c.save()
        org._sys_cache.clear()
        return {"goals": org.c.goals, "objectives": objectives(org)}

    # ---------- the business: strategy, org roadmap, clients, what comes in, the weekly plan ----------
    @app.get("/api/business", dependencies=A)
    def business_get():
        from . import blueprints
        from .metrics import objectives
        return {"business": org.c.business, "stages": blueprints.STAGES, "stage_names": blueprints.STAGE_NAMES,
                "roadmap": blueprints.roadmap(org), "objectives": objectives(org)}

    @app.put("/api/business", dependencies=A)
    def business_put(body: dict):
        from . import blueprints, design
        fields = {k: body[k] for k in design.ANSWER_FIELDS.values() if k in body}
        if "stage" in fields and fields["stage"] and design.stage_of(str(fields["stage"])) == "":
            raise HTTPException(400, f"stage is one of: {', '.join(blueprints.STAGES)}")
        if fields.get("stage"):
            fields["stage"] = design.stage_of(str(fields["stage"]))
        before = org.c.business.get("stage")
        biz = design.set_business(org, fields)
        if biz.get("stage") != before:
            org.log_decision("stage", org.c.name, biz.get("stage") or "unset", f"was {before or 'unset'}")
        org._sys_cache.clear()
        return business_get()

    @app.get("/api/activity", dependencies=A)
    def activity_get(hours: float = 48, actor: str = "", project: int | None = None, q: str = "", limit: int = 300):
        return {"entries": activity.read(org, since=time.time() - hours * 3600, actor=actor, project=project, grep=q,
                                         limit=max(1, min(limit, 2000)))}

    @app.get("/api/decisions/open", dependencies=A)
    def decisions_open():
        from . import decisions
        return {"decisions": decisions.grouped(org)}

    @app.post("/api/decisions/open/{key}/{action}", dependencies=A)
    def decisions_act(key: str, action: str, body: dict | None = None):
        from . import decisions
        try:
            if action == "answer":
                return decisions.answer(org, key, str((body or {}).get("text") or ""), bool((body or {}).get("save", True)))
            if action == "dismiss":
                return {"dismissed": decisions.dismiss(org, key)}
        except KeyError as e:
            raise HTTPException(404, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))
        raise HTTPException(400, "answer or dismiss")

    @app.get("/api/clients", dependencies=A)
    def clients_list(stage: str | None = None):
        from .clients import STAGES
        return {"contacts": org.clients.all(stage), "counts": org.clients.counts(), "stages": STAGES,
                "numbers": org.clients.numbers(), "due": [c["id"] for c in org.clients.due()],
                "revenue": org.clients.revenue(30)}

    @app.post("/api/clients", dependencies=A)
    def clients_add(body: dict):
        try:
            c = org.clients.upsert(str(body.get("email") or ""), name=str(body.get("name") or ""),
                                   org=str(body.get("org") or ""), source=str(body.get("source") or "owner"),
                                   stage=str(body.get("stage") or ""), next_step=body.get("next_step"),
                                   next_date=body.get("next_date"), notes=body.get("notes"), force=True)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return c

    @app.post("/api/clients/import", dependencies=A)
    def clients_import(body: dict):
        return org.clients.import_csv(str(body.get("text") or ""), source="import")

    @app.get("/api/clients/{cid}", dependencies=A)
    def client_get(cid: int):
        c = org.clients.get(cid)
        if not c:
            raise HTTPException(404)
        return {"contact": c, "timeline": org.clients.timeline(cid),
                "revenue": [r for r in org.clients.revenue(500) if r["contact_id"] == cid]}

    @app.patch("/api/clients/{cid}", dependencies=A)
    def client_patch(cid: int, body: dict):
        c = org.clients.get(cid)
        if not c:
            raise HTTPException(404)
        kw = {k: body[k] for k in ("name", "org", "stage", "next_step", "next_date", "notes", "dept") if k in body}
        if "value" in body:
            try:
                kw["value"] = float(body["value"]) if str(body["value"]).strip() else None
            except ValueError:
                raise HTTPException(400, "value must be a number")
        out = org.clients.upsert(c["email"], force=True, **{k: ("" if v is None else v) for k, v in kw.items()
                                                           if k != "value"}, value=kw.get("value"))
        if body.get("note"):
            org.clients.log(cid, "note", str(body["note"])[:1000])
        return out

    @app.delete("/api/clients/{cid}", dependencies=A)
    def client_del(cid: int):
        org.clients.remove(cid)
        return {"ok": True}

    @app.post("/api/clients/{cid}/revenue", dependencies=A)
    def client_revenue(cid: int, body: dict):
        if not org.clients.get(cid):
            raise HTTPException(404)
        try:
            return org.clients.add_revenue(float(body.get("amount")), contact_id=cid,
                                           currency=str(body.get("currency") or ""),
                                           kind=str(body.get("kind") or "one-off"), source="owner")
        except (TypeError, ValueError):
            raise HTTPException(400, "amount must be a number")

    @app.get("/api/metrics", dependencies=A)
    def metrics_list():
        return {"metrics": org.metrics.all()}

    @app.put("/api/metrics/{name}", dependencies=A)
    def metrics_set(name: str, b: MetricIn):
        try:
            return org.metrics.set(name, b.value, target=b.target, unit=b.unit)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.delete("/api/metrics/{name}", dependencies=A)
    def metrics_del(name: str):
        try:
            return {"ok": org.metrics.remove(name)}
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.get("/api/routines", dependencies=A)
    def routines_list():
        return {"routines": org.routines.overview()}

    @app.post("/api/routines", dependencies=A)
    def routines_add(b: RoutineIn):
        try:
            return org.routines.add(b.dept or "", b.text or "", b.every or "", b.member or "")
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.patch("/api/routines/{rid}", dependencies=A)
    def routines_patch(rid: str, b: RoutineIn):
        try:
            return org.routines.update(rid, **b.model_dump())
        except KeyError as e:
            raise HTTPException(404, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/routines/{rid}/run", dependencies=A)
    def routines_run(rid: str):
        try:
            return {"task": org.routines.run_now(rid)}
        except KeyError as e:
            raise HTTPException(404, str(e))

    @app.delete("/api/routines/{rid}", dependencies=A)
    def routines_del(rid: str):
        return {"ok": org.routines.remove(rid)}

    # keys & tokens: values go in, never come back out
    @app.get("/api/env", dependencies=A)
    def env_list():
        return {"keys": org.env.public()}

    @app.put("/api/env/{name}", dependencies=A)
    def env_set(name: str, b: EnvSet):
        try:
            return org.env.set(name, b.value or None, depts=b.depts, note=b.note)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/env/import", dependencies=A)
    def env_import(b: EnvImport):
        return {"added": org.env.import_dotenv(b.text, b.depts)}

    @app.delete("/api/env/{name}", dependencies=A)
    def env_del(name: str):
        return {"ok": org.env.remove(name)}

    # pending -> approved (an email waiting to be sent) -> sent; or rejected.
    # Approving something that isn't an email (a post, a doc) files it straight to done: there is nothing to send.
    OUTBOX_VIEWS = {"pending": ("pending",), "approved": ("approved",), "done": ("sent", "rejected", "done")}
    NOT_EMAIL = "approved; not an email (no To: line), so nothing is sent from here: copy it where it goes"

    def _file_non_emails():
        for it in org.s.q("SELECT id, body FROM outbox WHERE status='approved'"):
            if not outbox_send.parse_draft(it["body"])["to"]:
                org.s.x("UPDATE outbox SET status='done', note=? WHERE id=?", (NOT_EMAIL, it["id"]))

    @app.get("/api/outbox", dependencies=A)
    def outbox(view: str = "pending"):
        _file_non_emails()
        st = OUTBOX_VIEWS.get(view, OUTBOX_VIEWS["pending"])
        items = org.s.q(f"SELECT * FROM outbox WHERE status IN ({','.join('?' * len(st))}) ORDER BY id DESC LIMIT 100", st)
        counts = {k: org.s.one(f"SELECT COUNT(*) n FROM outbox WHERE status IN ({','.join('?' * len(v))})", v)["n"]
                  for k, v in OUTBOX_VIEWS.items()}
        env = org.env.values()
        for it in items:
            if it["status"] in ("pending", "approved"):
                d, problem = outbox_send.check(it["body"], env)
                it["send"] = {"to": d["to"], "subject": d["subject"], "problem": problem, "email": bool(d["to"])}
        return {"view": view, "items": items, "counts": counts, "channel": outbox_send.channel(env)}

    @app.post("/api/outbox/{oid}/send", dependencies=A)
    def outbox_send_now(oid: int):
        """The owner's click: send now. Works from review too (Approve & send)."""
        it = org.s.one("SELECT * FROM outbox WHERE id=?", (oid,))
        if not it:
            raise HTTPException(404)
        if it["status"] not in ("pending", "approved"):
            raise HTTPException(409, f"already {it['status']}")
        # claim first: a second click (or tab) finds it 'sending' and stops, so nobody gets the email twice
        if not org.s.xc("UPDATE outbox SET status='sending' WHERE id=? AND status IN ('pending','approved')", (oid,)):
            raise HTTPException(409, "it's already being sent")
        try:
            note = outbox_send.send(it["body"], org.env.values())
        except Exception as e:                       # network errors included: put it back, say why
            msg = org.env.redact(str(e))[:300]
            org.s.x("UPDATE outbox SET status=?, note=? WHERE id=?", (it["status"], f"not sent: {msg}", oid))
            raise HTTPException(400 if isinstance(e, ValueError) else 502, msg)
        org.s.x("UPDATE outbox SET status='sent', note=? WHERE id=?", (note, oid))
        org.log_decision("send", f"outbox #{oid}", "sent", note)
        try:                                         # the pipeline remembers who was written to, and when to follow up
            d = outbox_send.parse_draft(it["body"])
            for to in d["to"][:5]:
                org.clients.sent(to, d["subject"], outbox_id=oid, dept=it.get("dept") or "")
        except Exception:                            # a record problem must never look like a failed send
            log.exception("could not record the sent email on the client record")
        return {"ok": True, "message": note}

    @app.post("/api/outbox/{oid}/test", dependencies=A)
    def outbox_test(oid: int):
        """Send the draft to OWNER_EMAIL only (subject marked TEST), so the owner sees what the recipient would get."""
        it = org.s.one("SELECT * FROM outbox WHERE id=?", (oid,))
        if not it:
            raise HTTPException(404)
        env = org.env.values()
        if not env.get("OWNER_EMAIL"):
            raise HTTPException(400, "Add OWNER_EMAIL on Keys & tokens to send tests to yourself.")
        d = outbox_send.parse_draft(it["body"])
        if not d["to"]:
            raise HTTPException(400, "This draft isn't an email (no To: line).")
        body = f"To: {env['OWNER_EMAIL']}\nSubject: [TEST] {d['subject']}\n\n(would go to: {', '.join(d['to'])})\n\n{d['text']}"
        try:
            return {"message": outbox_send.send(body, env)}
        except Exception as e:
            raise HTTPException(400, org.env.redact(str(e))[:300])

    @app.post("/api/outbox/{oid}/{action}", dependencies=A)
    def outbox_act(oid: int, action: str):
        if action not in ("approved", "rejected", "sent", "pending", "done"):
            raise HTTPException(400)
        it = org.s.one("SELECT body FROM outbox WHERE id=?", (oid,))
        if not it:
            raise HTTPException(404)
        if action == "approved" and not outbox_send.parse_draft(it["body"])["to"]:
            org.s.x("UPDATE outbox SET status='done', note=? WHERE id=?", (NOT_EMAIL, oid))
            return {"ok": True, "status": "done"}
        org.s.x("UPDATE outbox SET status=? WHERE id=?", (action, oid))
        return {"ok": True}

    @app.patch("/api/outbox/{oid}", dependencies=A)
    def outbox_edit(oid: int, b: OutboxEdit):
        org.s.x("UPDATE outbox SET body=? WHERE id=?", (b.body, oid))
        return {"ok": True}

    return app
