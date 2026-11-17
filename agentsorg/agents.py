"""The company engine: CEO fan-out, member execution, escalation, cache, KB, dependencies."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import router
from .budget import Budget, is_limit
from .config import Company, migrate_layout, resolve_home, upgrade_defaults
from .kb import KB
from .runtime import RunResult, find_claude, kill_tree, run_claude, sandbox_available, secret_paths, supports
from .state import State
from .files import Files, too_broad
from .services import Services
from .envvault import EnvVault
from .routines import Routines
from .metrics import Metrics, objectives, objective_line
from .intake import Intake
from .projects import Projects
from .changes import Changes
from .clients import Clients
from . import blueprints
from .wsmap import workspace_map

SKILL_CHARS = 4000          # cap per-dept skills in system prompt
MAX_TIMEOUT = 3600          # a timed-out task gets one retry with double the time, up to this
TIMEOUT_NOTE = ("\n\nNOTE: your previous attempt at this task was stopped by the time limit. Files you wrote are "
                "kept: check what exists, continue from it, finish the remaining parts first, and skip (and name) "
                "slow steps that can't finish.")
TEXT_INPUT_CHARS = 6000     # a text-only agent gets the step before it in full up to this; readers get a file path
CACHE_SECONDS = 24 * 3600
MANUAL = ("ceo", "dept")
MAX_TURNS = 60
JUNK_DIRS = {"__pycache__", "node_modules", ".pytest_cache", ".mypy_cache", "dist", "build", ".venv", "venv"}
JUNK_EXT = (".pyc", ".pyo", ".log", ".tmp")
LEASE_SECONDS = 300          # a running task whose worker stops renewing for this long is recovered
LEASE_RENEW = 60
NO_CACHE_SOURCES = ("routine",)   # scheduled work must really run
# the only work that runs without a fresh command: schedules the owner created, under the autonomous budget cap
SCHEDULED = ("routine",)
QUESTION_RE = re.compile(r"(?im)^\s*QUESTION:\s*(.{8,600})$")
WRITE_TOOLS = {"Edit", "Write", "Bash"}
STOP_ERRORS = ("auth", "login", "session limit", "max_turns", "not found", "could not start", "timeout", "rate limit", "rate_limit", "usage limit",
               "limit reached", "429", "credit", "quota", "overloaded")
log = logging.getLogger("agentsorg")
UNSANDBOXED = ("Shell commands can't be sandboxed on this machine (Claude Code's sandbox needs macOS, or Linux with bubblewrap and socat). Unsandboxed, this department's agents can run any command as your user and read any file your user can, including other projects. Confirm only if you trust every task it gets.")
READ_TOOLS = ["Read", "Glob", "Grep"]   # for departments that have linked folders but no file tools
# what the owner can switch on per department (dashboard: department page). Off by default except as designed.
ABILITIES = {
    "read": ("read files", ["Read", "Glob", "Grep"]),
    "edit": ("edit files in its folder", ["Read", "Edit", "Write", "Glob", "Grep"]),
    "web": ("search and read the web", ["WebSearch", "WebFetch"]),
    "shell": ("run shell commands (needed to use API keys)", ["Bash"]),
}


REPORT_RE = re.compile(r"(?im)^\s*(files? written:|i (updated|created|wrote|added|changed|saved|reviewed|checked)\b|"
                       r"created `|updated `|\*\*what changed|\*\*owner decisions?|nothing (was )?sent)")


def outgoing(text: str) -> bool:
    """Is this ONE message to send (an email with To:/Subject:, or a post), not a report about drafts?
    Agents sometimes mark a report OUTBOX: yes; the owner's Outbox must hold only what can go out as it is."""
    from .outbox_send import parse_draft
    t = (text or "").strip()
    if not t:
        return False
    if parse_draft(t)["to"]:
        return True
    return len(t) <= 3000 and not REPORT_RE.search(t)


def abilities_of(tools: list[str]) -> list[str]:
    t = set(tools)
    return [k for k, (_, need) in ABILITIES.items() if set(need) <= t and not (k == "read" and "Edit" in t)]
FILE_WORDS = re.compile(r"\b(files?|folders?|docs?|documents?|readme|inventory|repo|codebase|sources?|csv|notes|"
                        r"leads?|templates?|playbook|config|read)\b", re.I)
READABLE_MAP_CHARS = 9000

CEO_OPS = """COMPANY OPERATIONS (this overrides any earlier output format):
You see the company's real state in FACTS. Agents can only use files that are synced into the company.
You can do these yourself, instantly, with no agent and no extra tokens:
- {"action":"link_local","name":"<short-slug>","dept":"<department id>","path":"<path on this machine>"}  give a
  department live read-only access to a folder on this machine (no copies; use for paths the CEO mentions)
- {"action":"sync_local","source":"<linked folder name>"}  copy a synced folder from this machine (LIVE folders need nothing)
- {"action":"add_routine","dept":"<department id>","every":"hourly|daily HH:MM|weekdays HH:MM|mon HH:MM|mon,thu HH:MM",
  "task":"<self-contained instruction>"}  repeat work on a schedule (use when the CEO says every day/week/morning...)
- {"action":"add_goal","goal":"<one measurable company goal>"}  when the CEO states a goal or target
If the work fits no department, or a department lacks an ability it needs, say so in "answer" (the owner adds teams
and switches abilities on the Company page) and plan only what the existing teams can do.
SERVICES in FACTS shows what's running; answer questions about them from FACTS. You can't start or stop them: the CEO
does that on the dashboard's Services page.
Never create tasks that only plan how to move or organise files: use actions. Folders on OTHER computers can't be
copied by you: tell the CEO, in "answer", to run that folder's push command (Files page, or
`agentsorg files commands <name>`) on that computer.
Only departments whose DEPARTMENTS line says so can search the web or run shell commands; the rest work from text and
files. A "text only" department's reply is saved as a file for it automatically: never tell it to write, save or edit
a file at a path, and never route file-writing to it. Give web research only to a department that searches the web. Nobody sends email or messages: drafts go to the Outbox and the CEO sends them. If the command needs an ability
no department has, say so in "answer" (the CEO can switch it on, on that department's page) and create tasks only for
the parts agents can do.
A task that depends on files you are copying in this same reply will see them: actions run first."""
LEAD_PROMPT = """You are the head of a department. Split the command into the fewest tasks for your team (max 4); \
use 1 task if one person can do it. Output ONLY JSON, no prose:
[{"member":"<member id>","tier":1|2|3,"task":"<self-contained instruction>","after":<index of input task, optional>}]
tier 1 = short/simple/formulaic, 2 = real work (code, long-form, analysis), 3 = only hard high-stakes reasoning."""


TEXT_EXT = {".md", ".txt", ".html", ".htm", ".css", ".js", ".ts", ".py", ".json", ".yaml", ".yml", ".csv", ".sql",
            ".sh", ".toml", ".jsx", ".tsx", ".svg"}


def _with_files(result: str, folder: Path, files: list[Path], budget: int = 8000) -> str:
    """Make a deliverable that went into files visible where the owner looks (task result, outputs/)."""
    names = [f.relative_to(folder).as_posix() for f in files]
    if len(result.strip()) >= 300:                       # the reply already explains it: just name the files
        return result.rstrip() + "\n\nFiles written: " + ", ".join(names)
    parts = [result.strip()] if result.strip() else []
    for f, n in zip(files, names):
        if f.suffix.lower() not in TEXT_EXT or budget <= 0:
            parts.append(f"Wrote `{n}`")
            continue
        try:
            body = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        fence = "md" if f.suffix.lower() == ".md" else f.suffix.lower().lstrip(".")
        clip = body[:budget] + ("\n…(truncated)" if len(body) > budget else "")
        budget -= len(clip)
        parts.append(f"### {n}\n\n{clip}" if fence == "md" else f"### {n}\n\n```{fence}\n{clip}\n```")
    return "\n\n".join(parts)


def today_line() -> str:
    """Nobody guesses dates."""
    return time.strftime("TODAY: %Y-%m-%d (%A)")


def plan_item(p) -> dict | None:
    """One task from a model-written plan, coerced to safe types. None if it has no usable task text."""
    if not isinstance(p, dict):
        return None
    task = p.get("task")
    if not isinstance(task, str) or not task.strip():
        return None
    dept = p.get("dept")
    member = p.get("member")
    after = p.get("after")
    try:
        after = int(after) if after is not None and not isinstance(after, bool) else None
    except (TypeError, ValueError):
        after = None
    return {"dept": dept.strip() if isinstance(dept, str) else "",
            "member": member.strip() if isinstance(member, str) and member.strip() else None,
            "tier": router.clamp_tier(p.get("tier")), "task": task.strip()[:4000], "after": after,
            **{k: p[k] for k in ("id", "expects", "inputs", "critical", "parallel_safe") if k in p}}


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def _line(tag: str, text: str) -> str:
    m = re.search(rf"^\s*{tag}:\s*(.+)$", text, re.M | re.I)
    return m.group(1).strip() if m else ""


def _strip_footer(text: str) -> str:
    return re.sub(r"^\s*(DONE|KB\+|NEXT|OUTBOX|METRIC|ASK|LEAD|CLIENT|QUESTION):.*$", "", text, flags=re.M).strip()


class Org:
    def __init__(self, home: str | Path | None = None, serve: bool = False):
        """`serve=True` only in the long-running server: it alone recovers work interrupted by a crash.
        CLI processes never touch tasks another process is running (they hold a lease)."""
        self.home = resolve_home(str(home) if home else None)
        self.migrated = migrate_layout(self.home)       # 0.2 -> one folder per department (moves only)
        upgrade_defaults(self.home)                     # untouched old prompt copies -> current ones
        self.c = Company(self.home)
        self.s = State(self.c.db_path)
        self.kb = KB(self.s)
        self.budget = Budget(self.c, self.s)
        self.exe = find_claude(self.c.claude_bin)
        self.sandbox = sandbox_available()           # shell departments run inside Claude Code's sandbox when possible
        self._effort_ok: bool | None = None
        self.pool = ThreadPoolExecutor(max_workers=int(self.c.budget["concurrency"]))
        self._running: set[int] = set()
        self._lock = threading.Lock()
        self._sys_cache: dict[tuple, tuple[object, str]] = {}
        self.files = Files(self.c)
        self.files.ensure_layout()
        self.services = Services(self.c)
        self.env = EnvVault(self.home)
        self.routines = Routines(self)
        self.intake = Intake(self)
        self.projects = Projects(self)
        self.changes = Changes(self)
        self.metrics = Metrics(self.s)
        self.clients = Clients(self)                    # the pipeline, what was said, the money that came in
        self.worker = f"{socket.gethostname()}:{os.getpid()}:{int(time.time())}"
        self.job_providers: list = [self.intake.jobs]
        self._procs: dict[int, object] = {}          # task id -> running claude process (cancel / shutdown)
        self._stopping = False
        self._lease_stop = threading.Event()
        threading.Thread(target=self._renew_leases, daemon=True, name="leases").start()
        if serve:
            self.recover()
        self.archive_orphans()

    # ---------- task lifecycle ----------
    def _renew_leases(self) -> None:
        while not self._lease_stop.wait(LEASE_RENEW):
            try:
                self.s.x("UPDATE tasks SET lease_until=? WHERE worker=? AND status IN ('running','routing')",
                         (time.time() + LEASE_SECONDS, self.worker))
            except Exception:                        # never let the lease thread die
                log.exception("lease renewal failed")

    def recover(self) -> int:
        """Work whose worker died (lease expired) goes back where it can run again; planning steps fail
        (re-running them would duplicate the command). Safe to call often."""
        now, stale = time.time(), "(lease_until IS NULL OR lease_until < ?)"
        why = "interrupted while planning; run the command again"
        n = self.s.xc(f"UPDATE tasks SET status='failed', error=?, worker=NULL WHERE status='routing' AND {stale}",
                      (why, now))
        n += self.s.xc(f"UPDATE tasks SET status='failed', error=?, worker=NULL WHERE status='running' AND {stale} "
                       "AND (dept='ceo' OR title LIKE 'Delegating:%')", (why, now))
        n += self.s.xc(f"UPDATE tasks SET status='backlog', worker=NULL, error='interrupted; back in the backlog' "
                       f"WHERE status='running' AND {stale} AND source IN ('next','auto')", (now,))
        n += self.s.xc(f"UPDATE tasks SET status='queued', worker=NULL, error='interrupted; restarted' "
                       f"WHERE status='running' AND {stale}", (now,))
        if n:
            log.warning("recovered %d interrupted task(s)", n)
            self.pump()
        return n

    def close(self) -> None:
        """Wait for running workers (used by tests and short-lived CLI processes)."""
        self.pool.shutdown(wait=True)
        self._lease_stop.set()

    def shutdown(self) -> None:
        """Server stop: kill running claude processes and hand their tasks back without waiting."""
        self._stopping = True
        self._lease_stop.set()
        for p in list(self._procs.values()):
            kill_tree(p)
        self.pool.shutdown(wait=False, cancel_futures=True)
        self.s.x("UPDATE tasks SET status=CASE WHEN source IN ('next','auto') THEN 'backlog' ELSE 'queued' END, "
                 "worker=NULL, lease_until=NULL, error='interrupted by a restart' WHERE status='running' AND worker=?",
                 (self.worker,))
        self.s.x("UPDATE tasks SET status='failed', worker=NULL, error='interrupted while planning; run the command "
                 "again' WHERE status='routing' AND worker=?", (self.worker,))

    def _cancelled(self, tid: int) -> bool:
        r = self.s.one("SELECT status FROM tasks WHERE id=?", (tid,))
        return bool(r) and r["status"] == "cancelled"

    def _finish(self, tid: int, status: str, **fields) -> dict:
        """The one way a task ends. Cancel wins; dependents are always released or failed."""
        if self._stopping:
            return self.s.task(tid)              # shutdown already handed the task back
        if self._cancelled(tid):
            status = "cancelled"
            fields = {k: v for k, v in fields.items() if k == "tokens"}
        if status in ("done", "cached"):
            fields.setdefault("error", None)         # a task that waited on a limit and then succeeded is clean
        self.s.update_task(tid, status=status, worker=None, lease_until=None, **fields)
        self._procs.pop(tid, None)
        if status in ("done", "cached"):
            self._release(tid)
        elif status in ("failed", "cancelled"):
            self._fail_dependents(tid, *(("an earlier step failed", "failed") if status == "failed"
                                         else ("an earlier step was cancelled", "cancelled")))
            self._release(tid, fan_only=True)        # a final step still reports when a sibling failed
        self.on_task_end(tid, status)
        return self.s.task(tid)

    def on_task_end(self, tid: int, status: str) -> None:
        """A task ended: its project may be complete (board + one combined result)."""
        try:
            self.projects.on_task_end(tid, status)
        except Exception:
            log.exception("project update after task #%s", tid)
        try:
            from . import activity
            activity.task_ended(self, tid, status)
        except Exception:
            log.exception("activity update after task #%s", tid)

    def inbox_count(self) -> list[str]:
        from . import inbox
        return inbox.counts(self)

    def daily_jobs(self) -> list:
        """(name, fn) pairs the heartbeat runs once per period: backups, reviews, digests, context refresh."""
        from . import maintenance
        jobs = maintenance.daily_jobs(self)
        for provider in self.job_providers:
            jobs += provider()
        return jobs

    def _fail_dependents(self, tid: int, why: str, status: str = "failed") -> None:
        for k in self.s.q("SELECT id FROM tasks WHERE status='waiting' AND depends_on=?", (tid,)):
            self.s.update_task(k["id"], status=status, error=why)
            self._fail_dependents(k["id"], why, status)
            self.on_task_end(k["id"], status)

    def set_abilities(self, dept: str, abilities: list[str], accept_unsandboxed: bool = False) -> list[str]:
        """Switch a department's abilities. Shell without a sandbox needs the owner's explicit acceptance."""
        if dept not in self.c.depts:
            raise KeyError(f"unknown department {dept!r}")
        bad = [a for a in abilities if a not in ABILITIES]
        if bad:
            raise ValueError(f"unknown abilities: {', '.join(bad)} (choose from {', '.join(ABILITIES)})")
        had_shell = "Bash" in (self.c.depts[dept].tools or [])
        if "shell" in abilities and not had_shell and not self.sandbox and not accept_unsandboxed:
            raise PermissionError(UNSANDBOXED)
        tools: list[str] = []
        for a in abilities:
            tools += [t for t in ABILITIES[a][1] if t not in tools]
        with self.c.lock:
            d = self.c.raw["departments"][dept]
            if tools:
                d["tools"] = tools
                d["max_turns"] = max(int(d.get("max_turns", 1)), 8)
            else:
                d.pop("tools", None)
            self.c.save()
        self._sys_cache.clear()
        now = abilities_of(self.c.depts[dept].tools)
        self.log_decision("abilities", dept, ", ".join(now) or "text only",
                          "shell without sandbox accepted" if "shell" in now and not self.sandbox else "")
        return now

    def supersede(self, ids, by: int, why: str = "", kinds: tuple[str, ...] | None = None) -> int:
        """Older decisions the owner replaced: out of FACTS, and their KB facts marked, so nobody argues from them.
        `kinds` limits which decisions may be replaced (a planner may replace plans, never the owner's safety calls)."""
        n = 0
        for i in ids or []:
            try:
                i = int(str(i).lstrip("#"))
            except ValueError:
                continue
            d = self.s.one("SELECT * FROM decisions WHERE id=? AND superseded_by IS NULL AND id!=?", (i, by))
            if not d or (kinds and d["kind"] not in kinds):
                continue
            self.s.x("UPDATE decisions SET superseded_by=? WHERE id=?", (by, i))
            day = time.strftime("%Y-%m-%d")
            for k in self.s.q("SELECT id, title, body FROM kb WHERE tags='decision' AND title=?",
                              (f"Decision: {d['subject'][:80]}",)):
                self.kb.update(k["id"], title=f"{k['title'][:90]} (superseded {day})",
                               body=f"SUPERSEDED on {day}: {why or 'the owner gave a newer command'}. Was: {k['body']}"[:600])
            n += 1
        return n

    def log_decision(self, kind: str, subject: str, choice: str, why: str = "", ref: str = "") -> int:
        """Every owner decision is kept: what, when, why. The CEO sees the latest ones."""
        return self.s.x("INSERT INTO decisions(at,kind,subject,choice,why,ref) VALUES(?,?,?,?,?,?)",
                        (time.time(), kind, str(subject)[:200], str(choice)[:200], str(why or "")[:500], str(ref or "")))

    def cancel(self, tid: int) -> bool:
        t = self.s.task(tid)
        if not t or t["status"] in ("done", "failed", "cached", "cancelled", "archived"):
            return False
        self.s.update_task(tid, status="cancelled", error="cancelled by you", worker=None, lease_until=None)
        if p := self._procs.get(tid):
            kill_tree(p)
        self._fail_dependents(tid, "an earlier step was cancelled", "cancelled")
        self.on_task_end(tid, "cancelled")
        return True

    def archive_orphans(self) -> int:
        """Pending work of departments removed by a redesign can never run: archive it (history is kept)."""
        keep = list(self.c.depts) + ["ceo"]
        marks = ",".join("?" * len(keep))
        before = self.s.one(f"SELECT COUNT(*) n FROM tasks WHERE status IN ('queued','backlog','waiting','queued_blocked','held','routine_blocked') "
                            f"AND dept NOT IN ({marks})", tuple(keep))["n"]
        if before:
            self.s.x(f"UPDATE tasks SET status='archived', error='department removed in a redesign' "
                     f"WHERE status IN ('queued','backlog','waiting','queued_blocked','held','routine_blocked') AND dept NOT IN ({marks})", tuple(keep))
        return before

    # ---------- prompts ----------
    def _read(self, p: Path) -> str:
        return p.read_text(encoding="utf-8").strip() if p.exists() else ""

    def _mtime(self, *paths: Path) -> float:
        return max([p.stat().st_mtime for p in paths if p.exists()] or [0.0])

    def system_prompt(self, dept_id: str, member_id: str) -> str:
        """Stable prefix (cache-friendly): base -> dept -> member -> skills. Variable task goes in stdin."""
        dept = self.c.depts[dept_id]
        m = dept.member(member_id)
        sk_dir = self.c.dept_skills_dir(dept_id)
        instr = self.c.instructions_path(dept_id)
        files = [self.c.prompts_dir / "base.md", instr, self.c.path, *sorted(sk_dir.glob("*.md")),
                 *self.intake.files()]
        key = (dept_id, m.id)
        mt = self._mtime(*files)
        if key in self._sys_cache and self._sys_cache[key][0] == mt:
            return self._sys_cache[key][1]
        skills = "\n\n".join(self._read(f) for f in sorted(sk_dir.glob("*.md")))[:SKILL_CHARS]
        parts = [self._read(self.c.prompts_dir / "base.md"),
                 f"Company: {self.c.name}. {self.c.about} Department: {dept.name}. Mission: {dept.mission}",
                 self.c.business_text(),
                 self.c.goals_text(),
                 f"You are {m.name}, {m.role}.",
                 self.team_text(dept_id, m.id),
                 self._read(instr),
                 f"Skills:\n{skills}" if skills else "",
                 self.intake.system_text(dept_id)]
        sp = "\n\n".join(p for p in parts if p)
        self._sys_cache[key] = (mt, sp)
        return sp

    def team_text(self, dept_id: str, member_id: str) -> str:
        """Who you work with and for: the org chart every employee knows. Stable text, so the prompt stays cached."""
        dept = self.c.depts[dept_id]
        mates = [f"{x.name} ({x.role})" for x in dept.members if x.id != member_id]
        lines = ["YOUR COMPANY: you are an employee. You work for the owner (a human founder) through the CEO "
                 "(Chief of Staff), who plans the work and hands it to departments.",
                 f"- Your department: {dept.name}" + (f"; colleagues: {', '.join(mates)}" if mates else "; you are its only member")]
        others = []
        for d in self.c.depts.values():
            if d.id == dept_id:
                continue
            does = f", does {', '.join(d.functions)}" if d.functions else ""
            others.append(f"  {d.id}: {d.name} ({', '.join(x.role or x.name for x in d.members)}{does})")
        if others:
            lines.append("- Other departments:\n" + "\n".join(others))
        lines.append("- Decide ordinary questions yourself with a sensible default and list it under Assumed. Write a "
                     "QUESTION: line only when you truly cannot finish without the owner (prices, money, access and "
                     "keys, deploying or publishing, legal terms, the domain).")
        lines.append("- Never ask again what the KB already answers (owner decision): apply it.")
        return "\n".join(lines)

    def tools_for(self, dept, member, body: str = "", kind: str | None = None) -> list[str]:
        if kind == "question":                    # a one-line answer from what the team knows: no tools, no maps
            return []
        if member.tools or dept.tools:
            tools = list(member.tools or dept.tools)
            if dept.id in self.files.depts_with_sources():          # web/shell-only depts still read their folders
                tools += [t for t in READ_TOOLS if t not in tools]
            return tools
        if dept.id in self.files.depts_with_sources():
            return READ_TOOLS
        # a text department gets read-only access when the task is about the company's (synced) files
        return READ_TOOLS if body and FILE_WORDS.search(body) and self.readable_dirs(dept.id) else []

    def readable_dirs(self, dept_id: str) -> list[Path]:
        """Linked folders of OTHER departments that have files (synced copies or live folders)."""
        out = []
        for s in self.files.sources().values():
            p = self.files.path(s)
            if s.dept != dept_id and p.is_dir() and any(p.iterdir()):
                out.append(p)
        return out

    def own_live_dirs(self, dept_id: str) -> list[tuple[Path, bool]]:
        """This department's live (direct) folders: (path, writable). They are outside its folder."""
        return [(self.files.path(s), s.writable) for s in self.files.sources().values()
                if s.dept == dept_id and s.mode == "direct" and self.files.path(s).is_dir()]

    def company_facts(self) -> str:
        """What the CEO needs to route honestly: abilities and linked-folder state. Deterministic, no hashing."""
        with_src = self.files.depts_with_sources()
        lines = [today_line(), f"COMPANY FOLDER: {self.home}", "DEPARTMENTS:"]
        for d in self.c.depts.values():
            tools = d.tools or [t for m in d.members for t in m.tools]
            can = ("edits files in its folder" if {"Edit", "Write"} & set(tools) else
                   "reads its linked folders" if d.id in with_src else
                   "text only (reads company files when a task is about them)")
            if "WebSearch" in tools:
                can += "; searches and reads the web"
            if "Bash" in tools:
                can += "; runs shell commands" + (f" with keys {', '.join(k for k, _ in self.env.names_for(d.id))}"
                                                  if self.env.names_for(d.id) else "")
            lines.append(f"- {d.id} ({d.name}): {can}" + (f" · does: {', '.join(d.functions)}" if d.functions else ""))
        if biz := self.c.business_text():
            lines.append(biz.replace("Business (the owner's strategy; work inside it):", "BUSINESS:"))
        srcs = self.files.sources()
        lines.append("LINKED FOLDERS:" if srcs else "LINKED FOLDERS: none")
        for s in srcs.values():
            q = self.files.quick_status(s.name)
            if s.mode == "direct":
                tracked = s.name in {x.name for x in self.changes.repo_sources(s.dept)}
                lines.append(f"- {s.name} -> {s.dept} · LIVE folder {self.files.path(s)} "
                             f"({'writable' if s.writable else 'read-only'}) · {q['files']} files, always current"
                             + (" · CHANGE-TRACKED REPO: product code for it goes INTO this repo (agents work on a "
                                "project branch automatically; the owner merges). Name paths relative to the repo, "
                                "never a copy in the company folder." if tracked else ""))
                if tracked and (tl := self.changes.test_fact(s.name)):
                    lines.append(f"  {tl}")
                continue
            where = "on this machine" if self.files.local_path(s) else "on another computer"
            state = f"{q['files']} files, synced {q['synced']}" if q["files"] else "EMPTY: never synced"
            lines.append(f"- {s.name} -> {s.dept} · from {s.origin} ({where}) · {state}")
        if svc := self.services.facts():
            lines.append(svc)
        if nums := self.metrics.facts():
            lines.append(nums)
        if pipe := self.clients.facts():
            lines.append(pipe)
        if owner := self.s.q("SELECT title, body FROM kb WHERE tags LIKE '%owner%' ORDER BY updated DESC LIMIT 8"):
            lines.append("OWNER'S ANSWERS (already decided: never ask these again):\n"
                         + "\n".join(f"- {o['title'][:80]}: {o['body'][:200]}" for o in owner))
        if dec := self.s.q("SELECT id, at, kind, subject, choice, why FROM decisions WHERE superseded_by IS NULL "
                           "ORDER BY id DESC LIMIT 10"):
            lines.append("RECENT DECISIONS (by the owner, still in force; a newer command replaces any of them):\n"
                         + "\n".join(f"- #{d['id']} {time.strftime('%Y-%m-%d', time.localtime(d['at']))} {d['kind']}: "
                                      f"{d['subject'][:80]} -> {d['choice'][:80]}" + (f" ({d['why'][:80]})" if d["why"] else "")
                                      for d in dec))
        if self.c.goals:
            lines.append("GOALS:\n" + "\n".join(f"- {objective_line(o)}" for o in objectives(self)))
        if rs := self.routines.all():
            lines.append("ROUTINES:\n" + "\n".join(f"- {r['every']} {r['dept']}: {r['text'][:80]}"
                                                   + ("" if r.get("enabled", True) else " (paused)") for r in rs))
        return "\n".join(lines)

    def run_action(self, a: dict) -> str:
        """The CEO's direct actions. Copy-in only: originals are never changed."""
        kind = a.get("action")
        try:
            if kind == "add_routine":
                r = self.routines.add(str(a.get("dept", "")), str(a.get("task", "")), str(a.get("every", "")))
                return f"✓ routine: {r['every']} {r['dept']}: {r['text'][:60]}"
            if kind == "add_goal":
                g = str(a.get("goal", "")).strip()[:200]
                if not g:
                    return "✗ add_goal: empty goal"
                if g not in self.c.goals:
                    self.c.raw["goals"] = [*(self.c.raw.get("goals") or []), g]     # objectives keep their numbers
                    self.c.save()
                return f"✓ goal added: {g}"
            if kind == "sync_local":
                s = self.files.get(str(a.get("source", "")))
                if s.mode == "direct":
                    return f"✓ {s.name} is a live folder ({self.files.path(s)}): nothing to copy, agents use it directly"
                path = self.files.local_path(s)
                if not path:
                    return f"✗ {s.name} is on another computer: run its push command there (agentsorg files commands {s.name})"
                rep = self.files.receive_dir(s.name, path)
                return f"✓ copied {s.name} from {path}: +{len(rep['added'])} new, {len(rep['updated'])} updated"
            if kind == "link_local":
                dept = str(a.get("dept", ""))
                if dept not in self.c.depts:
                    return f"✗ unknown department {dept!r}"
                path = Path(str(a.get("path", ""))).expanduser().resolve()
                if not path.is_dir():
                    return f"✗ {path} is not a folder on this machine"
                if why := too_broad(path, self.home):
                    return f"✗ {why}"
                name = re.sub(r"[^a-z0-9]+", "-", str(a.get("name") or path.name).lower()).strip("-")[:40] or path.name
                if name not in self.files.sources():
                    self.files.add(name, dept, f"this-pc {path}")          # same machine: live, read-only
                return f"✓ linked {path} to {dept} as a live read-only folder (no copies)"
            return f"✗ unknown action {kind!r}"
        except (KeyError, ValueError, TypeError, AttributeError, FileExistsError, OSError) as e:
            return f"✗ {kind}: {e}"

    def user_prompt(self, task: dict) -> str:
        parts = [today_line()]
        dept = self.c.depts.get(task["dept"])
        tools = self.tools_for(dept, dept.member(task["member"]), task["body"], task.get("kind")) if dept else []
        if tools:                                                          # tool users: map instead of exploring
            parts.append(workspace_map(self.c.workspace(dept.id)))
            for p, w in self.own_live_dirs(dept.id):
                parts.append(f"LIVE FOLDER ({'writable' if w else 'read-only'}): {p}\n"
                             f"{workspace_map(p, summaries=False, max_chars=6000, max_files=300)}")
            # other teams' folders, names only (no per-file summaries): a full list beats extra browsing turns
            maps = "\n\n".join(f"READABLE (read-only): {p}\n{workspace_map(p, summaries=False, max_chars=7000, max_files=400)}"
                               for p in self.readable_dirs(dept.id))
            if maps:
                parts.append(maps[:READABLE_MAP_CHARS])
        if tools and dept and {"Edit", "Write"} & set(tools) and (wt := self.changes.prompt_text(task)):
            parts.append(wt)
        if tools and dept and (keys := self.env.names_for(dept.id)):
            parts.append("KEYS (environment variables of your shell; use as $NAME, never print or save a value):\n"
                         + "\n".join(f"{k}{' - ' + n if n else ''}" for k, n in keys))
        if ctx := self.kb.context(task["body"]):
            parts.append(f"KB (shared, may be relevant):\n{ctx}")
        if cl := self.clients.context(task["body"]):
            parts.append(f"CLIENT RECORD:\n{cl}")
        if task.get("depends_on"):
            dep = self.s.task(task["depends_on"])
            if dep and dep.get("result"):
                parts.append(self._input_block(dep, can_read=bool({"Read"} & set(tools))))
        if task.get("project_id") and (board := self.projects.board(task["project_id"])):
            parts.append(f"BOARD (this project, all teams):\n{board}")
        if task.get("kind") == "deliverable":
            parts.append("DELIVERABLE: this is a finished piece of work for the owner; it may run to 1200 words.")
        parts.append(f"TASK:\n{task['body']}")
        return "\n\n".join(parts)

    def _input_block(self, dep: dict, can_read: bool) -> str:
        """What a dependent task gets from the step before it: a short summary and the file, not the transcript.
        Agents that can read open the file when they need it; text-only agents get the deliverable itself (capped)."""
        head = f"INPUT from {dep['dept']} (task #{dep['id']}):"
        summ = dep.get("summary") or dep.get("done_line") or ""
        path = dep.get("output_path")
        if can_read and path:
            return f"{head}\nSummary: {summ}\nFull deliverable: {path}"
        return f"{head}\n{(dep['result'] or '')[:TEXT_INPUT_CHARS]}"

    # ---------- low-level call ----------
    def call(self, prompt: str, *, model: str, system: str, dept: str, task_id: int | None, auto: bool,
             tools: list[str] | None = None, max_turns: int = 1, cwd: Path | None = None,
             add_dirs: list[str] | None = None, read_only: list[str] | None = None,
             timeout: int | None = None) -> RunResult:
        timeout = int(timeout or self.c.budget.get("task_timeout", 900))
        shared = (add_dirs if add_dirs is not None else [str(self.c.shared_dir)]) if tools else None
        if not self.exe:
            return RunResult(text="", ok=False, model=model, error="claude CLI not found")
        if self._effort_ok is None:
            self._effort_ok = supports(self.exe, "--effort")
        tier = next((k for k, v in self.c.models.items() if v == model), 0)
        effort = self.c.effort.get(tier, "") if self._effort_ok else ""
        on_start = (lambda p: self._procs.__setitem__(task_id, p)) if task_id else None
        res = run_claude(self.exe, prompt, model=model, system=system, tools=tools, max_turns=max_turns, cwd=cwd,
                         effort=effort, add_dirs=shared, read_only=read_only,
                         env=self.env.env_for(dept if dept in self.c.depts else None, bool(tools)),
                         secrets=secret_paths(self.home), sandbox=self.sandbox, on_start=on_start,
                         timeout=timeout)
        res.text = self.env.redact(res.text)          # a key that leaks into a reply is masked before it's saved
        res.error = self.env.redact(res.error)
        if res.error == "timeout":                    # it ran (and likely spent quota) until killed: count the call
            self.s.record_usage(task_id=task_id, dept=dept, model=model, input=0, output=0, cache_read=0,
                                cache_create=0, cost_usd=0.0, duration_ms=timeout * 1000,
                                auto=int(auto), ok=0)
        for u in res.usage:
            if not res.ok and not (u["input"] or u["output"]):
                continue   # auth/network failures used no quota
            self.s.record_usage(task_id=task_id, dept=dept, duration_ms=res.duration_ms, auto=int(auto),
                                ok=int(res.ok), **u)
        return res

    # ---------- submit ----------
    def submit(self, dept: str, body: str, *, member: str | None = None, tier: int | None = None,
               source: str = "dept", depends_on: int | None = None, priority: int = 5,
               backlog: bool = False, parent_id: int | None = None, depth: int = 0, owner: bool = False,
               project_id: int | None = None, kind: str | None = None, hold: bool = False) -> int:
        """Queue a task. `owner=True` only for text the owner typed: only they may force a model (`!opus`).
        Returns 0 when a suggested follow-up is dropped (duplicate or backlog full)."""
        if dept not in self.c.depts:
            raise ValueError(f"unknown department {dept!r}")
        forced = None
        if owner:
            forced, body = router.forced_tier(body)
        tier = forced or router.clamp_tier(tier)
        d = self.c.depts[dept]
        m = d.member(member)
        h = hashlib.sha1(f"{dept}|{m.id}|{_norm(body)}".encode()).hexdigest()
        if backlog:
            if dup := self.s.one("SELECT id FROM tasks WHERE hash=? AND status IN ('backlog','queued','waiting',"
                                 "'running','routine_blocked')", (h,)):
                return dup["id"] if owner else 0
            cap = int(self.c.budget.get("backlog_cap", 20))
            if not owner and self.s.one("SELECT COUNT(*) n FROM tasks WHERE dept=? AND status='backlog'",
                                        (dept,))["n"] >= cap:
                return 0
        status = "backlog" if backlog else ("waiting" if depends_on else "queued")
        if depends_on and (dep := self.s.task(depends_on)) and dep["status"] in ("done", "cached"):
            status = "queued"
        if hold:                                     # a plan waiting for the owner (approval or conference room)
            status = "held"
        tid = self.s.add_task(dept=dept, member=m.id, body=body, source=source, priority=priority,
                              status=status, tier=tier, tier_forced=int(bool(forced)), parent_id=parent_id,
                              depends_on=depends_on, depth=depth, hash=h, project_id=project_id, kind=kind)
        if not backlog and not hold:
            self.pump()
        return tid

    # ---------- CEO ----------
    def ceo(self, text: str) -> dict:
        """Owner command -> project. Router (0 tokens) -> planner (one stronger call) -> dispatcher (see pipeline.py)."""
        from .pipeline import handle
        return handle(self, text)

    def delegate(self, dept: str, text: str) -> dict:
        """Command a department through its head: 1 Haiku call splits work across that team only."""
        d = self.c.depts[dept]
        head = d.members[0]
        forced, clean = router.forced_tier(text)
        suffix = f" !t{forced}" if forced else ""
        if len(d.members) == 1:
            return {"tasks": [self.submit(dept, clean + suffix, member=head.id, priority=2, owner=True)],
                    "routing": "direct", "tokens": 0}
        ok, why = self.budget.check(auto=False)
        if not ok:
            return {"tasks": [], "error": why}
        parent = self.s.add_task(dept=dept, member=head.id, body=clean, title="Delegating: " + clean[:68],
                                 source="dept", status="routing", tier=1, model=self.c.models[1],
                                 worker=self.worker, lease_until=time.time() + LEASE_SECONDS)
        res, plan, ids = None, [], []
        try:
            team = "\n".join(f"{m.id}: {m.role}" for m in d.members)
            res = self.call(f"TEAM:\n{team}\n\nCOMMAND:\n{clean}", model=self.c.models[1],
                            system=self._read(self.c.prompts_dir / "lead.md") or LEAD_PROMPT, dept=dept,
                            task_id=parent, auto=False, cwd=self.c.dept_dir(dept))
            if not res.ok:
                return {"tasks": [], "error": f"The department head couldn't plan it: {res.error[:200]}"}
            plan = [p for p in (plan_item({**x, "dept": dept}) for x in self._parse_plan(res.text)[:4]) if p] \
                or [{"member": head.id, "task": clean, "tier": None, "after": None}]
            for p in plan:
                dep = p.get("after")
                ids.append(self.submit(dept, p["task"] + suffix, member=p.get("member"), tier=forced or p.get("tier"),
                                       source="dept", priority=2, parent_id=parent, owner=True,
                                       depends_on=ids[dep] if dep is not None and 0 <= dep < len(ids) else None))
            return {"tasks": ids, "routing": "head", "tokens": res.tokens, "plan": plan}
        except Exception as e:
            log.exception("delegation failed")
            return {"tasks": ids, "error": f"Delegation failed: {type(e).__name__}: {e}"[:300]}
        finally:
            self.s.update_task(parent, status="done" if res and res.ok else "failed", worker=None, lease_until=None,
                               tokens=res.tokens if res else 0, result=json.dumps(plan),
                               done_line=f"delegated {len(ids)} task(s) to team", error=(res.error if res else None))

    def _parse_reply(self, text: str) -> dict:
        """CEO reply: {"answer","actions","tasks"}; an older-style bare JSON array is read as tasks."""
        m = re.search(r"\{.*\}|\[.*\]", text, re.S)
        if not m:
            return {}
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return {"tasks": self._parse_plan(text)}
        if isinstance(data, list):
            return {"tasks": [d for d in data if isinstance(d, dict) and d.get("task")]}
        if isinstance(data, dict):
            tasks = [d for d in (data.get("tasks") or []) if isinstance(d, dict) and d.get("task")]
            return {"answer": data.get("answer", ""), "actions": data.get("actions") or [], "tasks": tasks}
        return {}

    def _parse_plan(self, text: str) -> list[dict]:
        m = re.search(r"\[.*\]", text, re.S)
        if not m:
            return []
        try:
            data = json.loads(m.group(0))
            return [d for d in data if isinstance(d, dict) and d.get("task")]
        except json.JSONDecodeError:
            return []

    def _guess_dept(self, text: str) -> str:
        t = text.lower()
        if did := blueprints.guess_dept(self.c, text):       # the team whose function this is, whatever it's called
            return did
        rules = [("engineering", r"code|bug|api|build|deploy|app|website|script|database|frontend|backend|test"),
                 ("marketing", r"market|brand|seo|content|post|blog|ad|campaign|copy|social"),
                 ("sales", r"sale|lead|prospect|outreach|customer|support|ticket|deal|email"),
                 ("finance", r"budget|cost|price|pricing|revenue|finance|invoice|plan|ops|hire"),
                 ("research", r"research|competitor|market size|analy|trend|survey")]
        for dept, pat in rules:
            if dept in self.c.depts and re.search(pat, t):
                return dept
        return "research" if "research" in self.c.depts else next(iter(self.c.depts))

    # ---------- execution ----------
    def pump(self) -> None:
        """Start queued tasks up to concurrency. Each task is claimed atomically, so two processes (server and a
        CLI command) or two threads never run the same task."""
        if self._stopping:
            return
        with self._lock:
            free = int(self.c.budget["concurrency"]) - len(self._running)
            if free <= 0:
                return
            ready = self.s.q("SELECT id FROM tasks WHERE status='queued' ORDER BY priority, id LIMIT ?", (free * 3,))
            for r in ready:
                if free <= 0:
                    break
                if r["id"] in self._running or not self.s.claim(r["id"], ("queued",), self.worker, LEASE_SECONDS):
                    continue
                self._running.add(r["id"])
                free -= 1
                self.pool.submit(self._run_guarded, r["id"])

    def _run_guarded(self, tid: int) -> None:
        try:
            self.run_task(tid)
        except Exception as e:   # never let a worker die silently
            log.exception("task #%s crashed", tid)
            self._finish(tid, "failed", error=self.env.redact(f"{type(e).__name__}: {e}")[:500])
        finally:
            with self._lock:
                self._running.discard(tid)
            self._procs.pop(tid, None)
            self.pump()

    def _blocked(self, tid: int, t: dict, why: str) -> dict:
        """Over budget: park the task where it will be retried, without a worker holding it."""
        if t["source"] in SCHEDULED:
            if t["source"] == "routine" and self.s.one(
                    "SELECT id FROM tasks WHERE hash=? AND status='routine_blocked' AND id!=?", (t["hash"], tid)):
                return self._finish(tid, "cancelled", error="skipped: the same routine is already waiting for budget")
            status = "routine_blocked"
        else:
            status = "queued_blocked"
        self.s.update_task(tid, status=status, error=why, worker=None, lease_until=None)
        return self.s.task(tid)

    def run_task(self, tid: int) -> dict:
        t = self.s.task(tid)
        if t["status"] == "cancelled":
            return self._finish(tid, "cancelled")
        auto = t["source"] not in MANUAL
        dept = self.c.depts.get(t["dept"])
        if not dept:
            return self._finish(tid, "failed", error=f"department {t['dept']!r} no longer exists")
        m = dept.member(t["member"])
        tools = self.tools_for(dept, m, t["body"], t.get("kind"))
        # 1) result cache: identical task done in last 24h -> 0 tokens. Never for scheduled work (it must really
        #    run each time) or for tasks that change files (the change has to happen again).
        if t["source"] not in NO_CACHE_SOURCES and not (WRITE_TOOLS & set(tools)):
            hit = self.s.one("SELECT * FROM tasks WHERE hash=? AND status='done' AND id!=? AND updated>? "
                             "ORDER BY id DESC LIMIT 1", (t["hash"], tid, time.time() - CACHE_SECONDS))
            if hit:
                return self._finish(tid, "cached", result=hit["result"], done_line=hit["done_line"],
                                    model=hit["model"], tier=hit["tier"], tokens=0, summary=hit.get("summary"),
                                    output_path=hit.get("output_path"))
        # 2) budget gate
        ok, why = self.budget.check(auto=auto, scheduled=t["source"] in SCHEDULED)
        if not ok:
            return self._blocked(tid, t, why)
        tier = t["tier"] if t["tier_forced"] and t["tier"] else router.pick_tier(t["body"], m.tier, m.max_tier,
                                                                                 hint=t["tier"])
        cwd = self.c.workspace(dept.id)              # the real department folder, never a temp dir
        live = self.own_live_dirs(dept.id)
        others = [str(p) for p in self.readable_dirs(dept.id)]   # other teams' folders: always readable, never editable
        branches = [w["worktree"] for w in self.changes.worktrees_for(t)] if WRITE_TOOLS & set(tools) else []
        extra = [str(self.c.shared_dir)] + [str(p) for p, _ in live] + others + branches
        read_only = [str(p) for p, w in live if not w] + others + [  # the same folders reached through their links
            str(self.files.link_path(x)) for x in self.files.sources().values()
            if (x.mode == "direct" and not x.writable or x.dept != dept.id) and self.files._is_link(self.files.link_path(x))]
        system = self.system_prompt(dept.id, m.id)
        prompt = self.user_prompt(t)
        total, attempts, res = 0, 0, None
        turns, bumped = self._turns(dept, tools), False
        tmo, timed = int(self.c.budget.get("task_timeout", 900)), False
        started = time.time()
        while True:
            if self._cancelled(tid):
                return self._finish(tid, "cancelled", tokens=total)
            model = self.c.models.get(tier) or self.c.models[max(self.c.models)]
            self.s.update_task(tid, tier=tier, model=model)          # status stays 'running' (cancel may change it)
            res = self.call(prompt, model=model, system=system, dept=dept.id, task_id=tid, auto=auto,
                            tools=tools, max_turns=turns, cwd=cwd, add_dirs=extra,
                            read_only=read_only, timeout=tmo)
            total += res.tokens
            if self._cancelled(tid):
                return self._finish(tid, "cancelled", tokens=total)
            esc = _line("ESCALATE", res.text) if res.ok else ""
            if res.ok and not esc:
                break
            if res.ok:                                   # the agent asked for a stronger model: the only escalation
                nxt = router.escalate(tier, m.max_tier)
                if nxt is None or t["tier_forced"]:
                    break
                tier = nxt
                continue
            if "max_turns" in res.error and not bumped and turns < MAX_TURNS:
                turns, bumped = min(turns * 2, MAX_TURNS), True       # it ran out of room, not out of skill
                continue
            if res.error == "timeout" and not timed and tools:        # big work, not broken work: once, with more time
                tmo, timed = min(tmo * 2, MAX_TIMEOUT), True
                prompt += TIMEOUT_NOTE
                continue
            attempts += 1
            # errors never move up a tier (that multiplied cost); limits, auth and timeouts stop at once
            if attempts < 2 and not any(s in res.error.lower() for s in STOP_ERRORS):
                continue
            break
        if not res.ok and is_limit(res.error):      # nothing is wrong with the work: it resumes after the reset
            self.budget.note_limit(res.error)
            self.s.update_task(tid, tokens=total)
            return self._blocked(tid, t, res.error[:300])
        if not res.ok:
            return self._finish(tid, "failed", error=self.env.redact(res.error)[:500], tokens=total)
        text = res.text
        done = _line("DONE", text) or t["title"]
        nxt_line = _line("NEXT", text)
        result = _strip_footer(text)
        written = self._written_files(cwd, started) if tools else []
        written_rel = [str(f.relative_to(cwd).as_posix()) for f in written]
        written_rel += self.changes.touched(t, started) if tools else []      # in the project's repo branch
        if written:
            result = _with_files(result, cwd, written)
        result = self.env.redact(result)             # files an agent wrote may contain a key it read
        out = self.files.save_output({**t, "model": res.model}, result)  # every deliverable lands in <dept>/outputs/
        self._finish(tid, "done", result=result, done_line=done[:200], next_line=nxt_line[:300], tokens=total,
                     attempts=t["attempts"] + 1, summary=self._summary(done, result),
                     output_path=str(out) if out else None, files=json.dumps(written_rel[:40]) if written_rel else None)
        self.after_done(tid, t, dept, m, text, result)
        return self.s.task(tid)

    def _turns(self, dept, tools: list[str]) -> int:
        """Steps one call may take. Reading needs few; building something needs room to edit and check it."""
        if not tools:
            return 1
        floor = int(self.c.budget.get("build_turns", 30)) if WRITE_TOOLS & set(tools) else 12
        return max(dept.max_turns, floor)

    @staticmethod
    def _summary(done: str, result: str) -> str:
        """3 short lines a dependent task gets instead of the whole deliverable."""
        lines = [done] + [ln.strip("-*# ").strip() for ln in result.splitlines()
                          if ln.strip() and not ln.lstrip().startswith(("```", "|"))][:6]
        out, seen = [], set()
        for ln in lines:
            if ln and ln not in seen and len(out) < 3:
                out.append(ln[:200])
                seen.add(ln)
        return "\n".join(out)

    def after_done(self, tid: int, t: dict, dept, m, text: str, result: str) -> None:
        """Side effects of a finished deliverable, all without a model call: KB, numbers, clients, the Outbox, and
        a question for the owner when the team could not finish without one. Nothing starts more work by itself."""
        trust = "untrusted" if {"WebFetch", "WebSearch", "Bash"} & set(self.tools_for(dept, m, t["body"], t.get("kind"))) else ""
        self.kb.ingest(text, dept.id, tid, trust=trust)
        self.metrics.ingest(text, f"task #{tid} ({dept.id})")
        self.clients.ingest(text, dept.id, tid, trust=trust)      # LEAD: / CLIENT: lines -> the pipeline
        if (ob := _line("OUTBOX", text)) and not outgoing(result if len(ob) < 20 else ob):
            log.info("task #%s: OUTBOX line ignored: the reply is a report, not one message to send", tid)
            ob = ""
        if ob:
            # `OUTBOX: yes` marks the deliverable itself as outgoing (no duplicate text); older replies put it inline
            body = result if ob.lower() in ("yes", "y", "true", "above", "see above") or len(ob) < 20 else ob
            if not result.strip():
                self.s.update_task(tid, result=ob)
            self.s.x("INSERT INTO outbox(created,task_id,dept,body,status,project_id) VALUES(?,?,?,?,?,?)",
                     (time.time(), tid, dept.id, self.env.redact(body), "pending", t.get("project_id")))
        qs = [q.strip() for q in QUESTION_RE.findall(text)][:2]
        # older agents wrote owner questions as NEXT lines ("once the owner picks the price, ...")
        nxt_line = _line("NEXT", text)
        if not qs and nxt_line and self.projects.OWNER_GATE.search(nxt_line):
            qs = [nxt_line]
        for q in qs:
            if t.get("project_id"):
                self.projects.suggest(t["project_id"], q, dept.id, m.id, tid)

    @staticmethod
    def _written_files(folder: Path, since: float) -> list[Path]:
        """Files an agent created or changed in its own folder during this task (not outputs/ or linked sources/)."""
        out = []
        for dirpath, dirnames, filenames in os.walk(folder):
            rel = Path(dirpath).relative_to(folder).parts
            if rel[:1] in (("outputs",), ("sources",)):
                dirnames[:] = []
                continue
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in JUNK_DIRS]
            for f in filenames:
                fp = Path(dirpath) / f
                try:
                    if fp.stat().st_mtime >= since - 1 and not f.startswith(".") and not f.endswith(JUNK_EXT):
                        out.append(fp)
                except OSError:
                    pass
        return sorted(out)[:10]

    def _release(self, tid: int, fan_only: bool = False) -> None:
        """Start what waited on this task. A plan's final step (fan-in) also waits for every other step of its
        plan, so a report or end-to-end check never runs before a parallel sibling has finished."""
        fan = self.s.get("fanin") or {}
        cands = [] if fan_only else [k["id"] for k in self.s.q(
            "SELECT id FROM tasks WHERE status='waiting' AND depends_on=?", (tid,))]
        cands += [int(k) for k, v in fan.items() if tid in v and int(k) not in cands]
        for k in cands:
            t = self.s.task(k)
            if not t or t["status"] != "waiting":
                continue
            dep = self.s.task(t["depends_on"]) if t.get("depends_on") else None
            if dep and dep["status"] not in ("done", "cached"):
                continue
            if any((s := self.s.task(i)) and s["status"] not in ("done", "cached", "failed", "cancelled", "archived")
                   for i in fan.get(str(k), [])):
                continue
            self.s.update_task(k, status="queued")
            if str(k) in fan:
                fan.pop(str(k))
                self.s.set("fanin", fan)

    def wait_for_all(self, final: int, others: list[int]) -> None:
        fan = self.s.get("fanin") or {}
        fan[str(final)] = [i for i in others if i and i != final]
        self.s.set("fanin", fan)
        t = self.s.task(final)
        if t and t["status"] == "queued" and fan[str(final)]:     # its own step was already done: still wait
            self.s.update_task(final, status="waiting")
            self._release(fan[str(final)][0], fan_only=True)

    # ---------- waiting for budget ----------
    def retry_blocked(self) -> None:
        """Work that hit a cap is retried when budget frees up: commands under the manual cap, routines under the
        scheduled (auto) cap."""
        if self.budget.check(auto=False)[0]:
            self.s.x("UPDATE tasks SET status='queued' WHERE status='queued_blocked'")
        if self.budget.check(auto=True, scheduled=True)[0]:
            self.s.x("UPDATE tasks SET status='queued' WHERE status='routine_blocked'")
        self.pump()

    def wait(self, ids: list[int], timeout: float = 1800) -> list[dict]:
        end = time.time() + timeout
        pending = set(ids)
        while pending and time.time() < end:
            for i in list(pending):
                t = self.s.task(i)
                if t["status"] in ("done", "failed", "cached", "backlog", "queued_blocked"):
                    pending.discard(i)
            time.sleep(0.5)
        return [self.s.task(i) for i in ids]
