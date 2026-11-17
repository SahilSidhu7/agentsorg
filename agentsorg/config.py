"""Company config, layout and the registry of companies on this machine.

One company = one folder:
  company.yaml
  <dept>/instructions.md · <dept>/skills/ · <dept>/sources/ · <dept>/outputs/
  shared/                      company-wide files
  prompts/base.md, ceo.md      company-wide prompts
  data/ (database, token, logs) · mods/

Several companies can live on one machine. Each has a name (its folder name), its own port and its own
background service. ~/.config/agentsorg/companies.json remembers them and which one is the default.
"""
from __future__ import annotations

import copy
import json
import os
import re
import secrets
import shutil
import socket
import threading
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import blueprints
from .fsutil import atomic_write, private_file

PKG_DATA = Path(__file__).parent / "data"
BUSINESS_FIELDS = {"customer": 300, "offer": 300, "pricing": 300, "stage": 20, "channels": 200,
                   "target_90d": 200, "constraints": 300}


def clean_business(b) -> dict:
    """The stored strategy: who pays, for what, at what price, where the company is. Unknown stays empty."""
    out = {}
    for k, n in BUSINESS_FIELDS.items():
        v = " ".join(str((b or {}).get(k) or "").split())[:n] if isinstance(b, dict) else ""
        if k == "stage":
            v = v.lower().replace(" ", "_")
            v = v if v in blueprints.STAGES else ""
        if v:
            out[k] = v
    return out


def clean_objective(g) -> dict | None:
    """A goal as an objective. Plain text stays plain text; a dict may add the number that measures it."""
    if isinstance(g, dict):
        text = " ".join(str(g.get("text") or g.get("goal") or "").split())[:200]
        if not text:
            return None
        o = {"text": text}
        metric = re.sub(r"[^a-z0-9_]+", "_", str(g.get("metric") or "").lower()).strip("_")[:40]
        if metric:
            o["metric"] = metric
        try:
            if g.get("target") is not None and str(g.get("target")).strip() != "":
                o["target"] = float(g["target"])
        except (TypeError, ValueError):
            pass
        if re.match(r"^\d{4}-\d{2}-\d{2}$", str(g.get("due") or "")):
            o["due"] = str(g["due"])
        if g.get("dept"):
            o["dept"] = str(g["dept"])[:24]
        return o
    text = " ".join(str(g or "").split())[:200]
    return {"text": text} if text else None


def objective_line(o: dict) -> str:
    extra = []
    if o.get("metric") and o.get("target") is not None:
        extra.append(f"{o['metric']} to {o['target']:g}")
    if o.get("due"):
        extra.append(f"by {o['due']}")
    return o["text"] + (f" ({', '.join(extra)})" if extra else "")
CONFIG_DIR = Path(os.environ.get("AGENTSORG_CONFIG_DIR") or Path.home() / ".config" / "agentsorg")
REGISTRY = CONFIG_DIR / "companies.json"
POINTER = CONFIG_DIR / "home"          # pre-0.3 single-company pointer, migrated into the registry
RESERVED = {"data", "mods", "shared", "prompts", "workspace", "skills", "ceo"}   # never department ids
FIRST_PORT = 5556


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40] or "company"


# ---------- registry ----------
def load_registry() -> dict:
    try:
        reg = json.loads(REGISTRY.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        reg = {"default": "", "companies": {}}
    reg.setdefault("companies", {})
    reg.setdefault("default", "")
    if not reg["companies"]:                      # migrate the old single pointer
        try:
            p = Path(POINTER.read_text(encoding="utf-8").strip())
            if (p / "company.yaml").exists():
                reg["companies"][slug(p.name)] = {"path": str(p)}
                reg["default"] = slug(p.name)
        except OSError:
            pass
    # forget companies whose folder is gone
    reg["companies"] = {n: v for n, v in reg["companies"].items() if (Path(v["path"]) / "company.yaml").exists()}
    if reg["default"] not in reg["companies"]:
        reg["default"] = next(iter(reg["companies"]), "")
    return reg


def save_registry(reg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write(REGISTRY, json.dumps(reg, indent=2))


def register(home: Path, make_default: bool = False) -> str:
    reg = load_registry()
    home = home.resolve()
    for n, v in reg["companies"].items():
        if Path(v["path"]).resolve() == home:
            name = n
            break
    else:
        base = name = slug(home.name)
        i = 2
        while name in reg["companies"]:
            name = f"{base}-{i}"
            i += 1
        reg["companies"][name] = {"path": str(home)}
    if make_default or not reg["default"]:
        reg["default"] = name
    save_registry(reg)
    return name


def company_name(home: Path) -> str:
    home = home.resolve()
    for n, v in load_registry()["companies"].items():
        if Path(v["path"]).resolve() == home:
            return n
    return slug(home.name)


def resolve_home(explicit: str | None = None, company: str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    if company:
        reg = load_registry()
        if company not in reg["companies"]:
            known = ", ".join(reg["companies"]) or "none yet"
            raise FileNotFoundError(f"No company named {company!r} on this machine (known: {known}).")
        return Path(reg["companies"][company]["path"])
    if env := os.environ.get("AGENTSORG_HOME"):
        return Path(env).expanduser().resolve()
    cwd = Path.cwd()
    for p in (cwd, *cwd.parents):
        if (p / "company.yaml").exists():
            return p
    reg = load_registry()
    if reg["default"]:
        return Path(reg["companies"][reg["default"]]["path"])
    return Path.home() / ".agentsorg"


def set_default_home(home: Path) -> None:        # kept for callers from 0.2
    register(home, make_default=True)


def used_ports(exclude: Path | None = None) -> set[int]:
    ports = set()
    for v in load_registry()["companies"].values():
        p = Path(v["path"])
        if exclude and p.resolve() == exclude.resolve():
            continue
        try:
            ports.add(int((yaml.safe_load((p / "company.yaml").read_text(encoding="utf-8")) or {}).get("port", FIRST_PORT)))
        except (OSError, ValueError, yaml.YAMLError):
            pass
    return ports


def free_port(exclude: Path | None = None) -> int:
    taken = used_ports(exclude)
    port = FIRST_PORT
    while True:
        if port not in taken:
            with socket.socket() as s:
                s.settimeout(0.3)                       # Windows can take ~2s to refuse a closed port
                if s.connect_ex(("127.0.0.1", port)) != 0:
                    return port
        port += 1


@dataclass
class Member:
    id: str
    name: str
    role: str
    tier: int = 2          # default model tier
    max_tier: int = 2      # opus only if member allows 3
    tools: list[str] = field(default_factory=list)


@dataclass
class Dept:
    id: str
    name: str
    mission: str
    members: list[Member]
    max_turns: int = 1
    tools: list[str] = field(default_factory=list)
    functions: list[str] = field(default_factory=list)     # what it does for the company (blueprints.FUNCTIONS)

    def member(self, mid: str | None) -> Member:
        """By id, else by name ("owen" finds Owen), else the head."""
        key = (mid or "").strip().lower()
        for m in self.members:
            if m.id == mid:
                return m
        for m in self.members:
            if key and m.name.lower() == key:
                return m
        return self.members[0]


class Company:
    def __init__(self, home: Path):
        self.home = home
        self.path = home / "company.yaml"
        if not self.path.exists():
            raise FileNotFoundError(f"No company.yaml in {home}. Run `agentsorg init` first.")
        self.lock = threading.RLock()        # API threads, CEO actions and routines all edit this file
        self.reload()

    def _read_yaml(self) -> dict:
        try:
            d = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as e:
            bak = self.path.with_name("company.yaml.bak")
            if bak.exists():                 # a broken edit must not take the company down: use the last good copy
                d = yaml.safe_load(bak.read_text(encoding="utf-8")) or {}
                self.load_error = f"company.yaml has an error ({str(e).splitlines()[0]}); using the last good copy"
            else:
                raise ValueError(f"company.yaml can't be read: {e}") from e
        if not isinstance(d, dict):
            raise ValueError("company.yaml must be a mapping")
        return d

    def maybe_reload(self) -> bool:
        """Pick up edits made by another process (CLI, a text editor). True if the file changed."""
        try:
            mt = self.path.stat().st_mtime_ns
        except OSError:
            return False
        if mt == self._mtime:
            return False
        with self.lock:
            self.reload()
        return True

    def reload(self) -> None:
        self.load_error = ""
        self.raw = self._read_yaml()
        self._snapshot = copy.deepcopy(self.raw)
        try:
            self._mtime = self.path.stat().st_mtime_ns
        except OSError:
            self._mtime = 0
        r = self.raw
        self.name = r.get("name", "agents.org")
        self.about = r.get("about", "")
        self.models: dict[int, str] = {int(k): v for k, v in (r.get("models") or {1: "haiku", 2: "sonnet", 3: "opus"}).items()}
        # reasoning effort per tier ("" = CLI default). Haiku's adaptive thinking is the big hidden cost.
        self.effort: dict[int, str] = {1: "low", **{int(k): str(v or "") for k, v in (r.get("effort") or {}).items()}}
        self.budget = {
            "auto_max_percent": 40, "manual_max_percent": 90,
            "auto_max_calls": 60, "manual_max_calls": 300,
            "heartbeat_minutes": 15, "quiet_hours": [],
            "concurrency": 2, "max_chain_depth": 2,
            **(r.get("budget") or {}),
        }
        self.report_time = str(r.get("report_time", "18:00"))
        # a goal is a line of text, or an objective: {text, metric, target, due, dept}
        self.objectives: list[dict] = [o for o in (clean_objective(g) for g in (r.get("goals") or [])) if o]
        self.goals: list[str] = [o["text"] for o in self.objectives]
        self.business: dict = clean_business(r.get("business"))
        self.port = int(r.get("port", FIRST_PORT))
        self.claude_bin = r.get("claude_bin") or ""
        self.depts: dict[str, Dept] = {}
        for did, d in (r.get("departments") or {}).items():
            members = [Member(id=mid, name=m.get("name", mid), role=m.get("role", ""),
                              tier=int(m.get("tier", 2)), max_tier=int(m.get("max_tier", m.get("tier", 2))),
                              tools=m.get("tools") or [])
                       for mid, m in (d.get("members") or {}).items()]
            fns = blueprints.clean_functions(d.get("functions")) or blueprints.infer(
                did, str(d.get("name", "")), str(d.get("mission", "")))
            self.depts[did] = Dept(id=did, name=d.get("name", did), mission=d.get("mission", ""),
                                   members=members, max_turns=int(d.get("max_turns", 1)),
                                   tools=d.get("tools") or [], functions=fns)

    def goals_text(self) -> str:
        return ("Company goals (work toward these; say when a task conflicts with them):\n"
                + "\n".join(f"- {objective_line(o)}" for o in self.objectives)) if self.objectives else ""

    def business_text(self) -> str:
        """The strategy every agent works from (stable, so it sits in the cached system prompt)."""
        b = self.business
        rows = [("Customer", b.get("customer")), ("Offer", b.get("offer")),
                ("Pricing", b.get("pricing") or ("unconfirmed: never quote a price; the owner decides it"
                                                 if b.get("offer") else "")),
                ("Stage", blueprints.STAGE_NAMES.get(b.get("stage"), "")), ("Channels", b.get("channels")),
                ("90-day target", b.get("target_90d")), ("Constraints", b.get("constraints"))]
        rows = [f"- {k}: {v}" for k, v in rows if v]
        return ("Business (the owner's strategy; work inside it):\n" + "\n".join(rows)) if rows else ""

    def save(self) -> None:
        """Atomic write (+ company.yaml.bak). If another process changed the file since we read it, keep its
        changes for every top-level section we didn't change ourselves."""
        with self.lock:
            out = self.raw
            try:
                changed_on_disk = self.path.stat().st_mtime_ns != self._mtime
            except OSError:
                changed_on_disk = False
            if changed_on_disk:
                disk = self._read_yaml()
                out = dict(disk)
                for k in set(self.raw) | set(self._snapshot):
                    if self.raw.get(k) != self._snapshot.get(k):        # ours changed it
                        if k in self.raw:
                            out[k] = self.raw[k]
                        else:
                            out.pop(k, None)
            atomic_write(self.path, yaml.safe_dump(out, sort_keys=False, allow_unicode=True), backup=True)
            self.reload()

    def upsert_member(self, dept: str, mid: str, data: dict) -> None:
        d = self.raw.setdefault("departments", {}).setdefault(dept, {"name": dept, "members": {}})
        d.setdefault("members", {})[mid] = {**d["members"].get(mid, {}), **data}
        self.save()

    def remove_member(self, dept: str, mid: str) -> None:
        self.raw["departments"][dept]["members"].pop(mid, None)
        self.save()

    # ---------- identity ----------
    @property
    def slug(self) -> str:
        return company_name(self.home)

    # ---------- paths ----------
    @property
    def db_path(self) -> Path: return self.home / "data" / "org.db"
    @property
    def prompts_dir(self) -> Path: return self.home / "prompts"          # base.md, ceo.md (company-wide)
    @property
    def shared_dir(self) -> Path: return self.home / "shared"

    def dept_dir(self, dept: str) -> Path:
        p = self.home / dept
        p.mkdir(parents=True, exist_ok=True)
        return p

    def instructions_path(self, dept: str) -> Path:
        return self.home / dept / "instructions.md"

    def dept_skills_dir(self, dept: str) -> Path:
        return self.home / dept / "skills"

    def workspace(self, dept: str) -> Path:                 # where a department's agents work
        return self.dept_dir(dept)

    def token(self) -> str:
        p = self.home / "data" / "token"
        if not p.exists():
            atomic_write(p, secrets.token_urlsafe(24), mode=0o600)
        else:
            private_file(p)
        return p.read_text().strip()

    def roster(self) -> str:
        """Compact roster for CEO routing prompt (kept tiny on purpose)."""
        lines = []
        for d in self.depts.values():
            ms = ", ".join(f"{m.id}({m.role})" for m in d.members)
            lines.append(f"{d.id}: {d.mission} | {ms}")
        return "\n".join(lines)


def migrate_layout(home: Path) -> list[str]:
    """Move a 0.2 layout (prompts/<dept>.md, skills/<dept>/, workspace/<dept>/) into <dept>/ folders.
    Moves only; never overwrites or deletes anything that has content. Returns what moved."""
    moved = []
    try:
        depts = list((yaml.safe_load((home / "company.yaml").read_text(encoding="utf-8")) or {}).get("departments") or {})
    except (OSError, yaml.YAMLError):
        return moved

    def move(src: Path, dst: Path):
        if src.exists() and not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            moved.append(f"{src.relative_to(home)} -> {dst.relative_to(home)}")

    for d in depts:
        move(home / "prompts" / f"{d}.md", home / d / "instructions.md")
        move(home / "skills" / d, home / d / "skills")
        old = home / "workspace" / d
        if old.is_dir():
            for child in list(old.iterdir()):          # sources/, outputs/, and any loose work files
                move(child, home / d / child.name)
    move(home / "workspace" / "shared", home / "shared")
    for leftover in [home / "workspace" / d for d in depts] + [home / "workspace", home / "skills"]:
        try:
            leftover.rmdir()                            # only removes empty folders
        except OSError:
            pass
    return moved


# packaged prompts that were shipped before and never edited by the user: safe to upgrade in place
OLD_DEFAULTS = {"prompts/base.md": {"ac5d3fbbd0b876443a4cdcf32eb64d4990767f04",
                                    "10098ee93de671980450f9a90f61f4dd5b7e05c6",
                                    "62b83206c8716667c059b3de5407b25867434ed0",      # 0.4.x
                                    "779829243713c19b8a53be5a805603c07873daae",      # 0.5-0.6
                                    "8f439846af003c30c25df9054d11c19c2cc82bf1"},     # 0.7.x
                "prompts/ceo.md": {"5c71a09b0e547a915acae349ddf57763378051c5",      # router-only, before 0.5
                                   "180b9596d7ee106899b2a3dae183401c49f15aa5",      # 0.5.x planner
                                   "cc065986da6eaba92a3302d6982039986f99d5cf",      # 0.6.0-0.6.1
                                   "9643958ae6f74d4f14be0a2a7cc8d2e3287b5f1d",      # 0.6.2-0.6.3
                                   "4148837dd00710be25a9c6ffff32385e5d2c864f",      # 0.6.4-0.6.7
                                   "b0abc0d3d2c2dbeaa6b12922bd95e60b2abdb3fd"}}     # 0.7.x


def upgrade_defaults(home: Path) -> list[str]:
    """Replace a company's copy of a packaged prompt with the new one, only if it is an untouched old copy."""
    import hashlib
    done = []
    for rel, old in OLD_DEFAULTS.items():
        dst, src = home / rel, PKG_DATA / rel
        try:
            if hashlib.sha1(dst.read_bytes().replace(b"\r\n", b"\n")).hexdigest() in old:
                shutil.copyfile(src, dst)
                done.append(rel)
        except OSError:
            pass
    return done


def init_home(home: Path, force: bool = False) -> list[str]:
    """Scaffold a company dir from packaged defaults. Never overwrites existing files unless force."""
    made = []
    home.mkdir(parents=True, exist_ok=True)
    fresh = not (home / "company.yaml").exists()
    pairs = [(PKG_DATA / "company.yaml", home / "company.yaml")]
    for src in (PKG_DATA / "prompts").glob("*.md"):
        if src.stem in ("base", "ceo"):
            pairs.append((src, home / "prompts" / src.name))
        else:                                                # department instructions live with the department
            pairs.append((src, home / src.stem / "instructions.md"))
    for src in (PKG_DATA / "skills").glob("*/*.md"):
        pairs.append((src, home / src.parent.name / "skills" / src.name))
    for src, dst in pairs:
        if dst.exists() and not force:
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        made.append(str(dst.relative_to(home)))
    if fresh:                                                # each company on this machine gets its own port
        c = home / "company.yaml"
        text = c.read_text(encoding="utf-8")                 # edit the one line: keep the file's comments
        port = free_port(exclude=home)
        text, n = re.subn(r"(?m)^port:\s*\d+", f"port: {port}", text)
        c.write_text(text if n else f"port: {port}\n{text}", encoding="utf-8")
    for d in ("data", "mods", "shared"):
        (home / d).mkdir(exist_ok=True)                      # mods/ empty on purpose: mods are opt-in
    return made
