"""agentsorg mods: drop-in folders that change how agents work, without touching agentsorg itself.

<company>/mods/<name>/mod.yaml
    name, description, enabled, applies_to: ["*"] or [dept ids]
    system:   text appended to the system prompt (stable -> prompt-cached)
    context:  text added to every task prompt
    files:    [paths in the mod folder] appended to the system prompt
    kb:       [{title, body}] seeded into the shared knowledge base when enabled
    tiers:    {force: 1|2|3, max: 1|2|3, keywords: {"word": tier}}
<company>/mods/<name>/hooks.py (optional, your own Python, full permissions)
    before_task(task: dict, org) -> None | dict   # {"body": ...} rewrite, {"result": "..."} answer (0 tokens),
                                                  # {"skip": "reason"} refuse
    after_task(task: dict, text: str, org) -> None | str   # return new text to replace the result
"""
from __future__ import annotations

import importlib.util
import threading
import io
import re
import shutil
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

import yaml

from .config import PKG_DATA

KNOWN_KEYS = {"name", "description", "enabled", "applies_to", "system", "context", "files", "kb", "tiers", "builtin"}
BUILTIN = Path(__file__).parent / "data" / "builtin_mods"     # the CEO pipeline: router, planner, dispatcher, conference
# hashes of built-in files shipped by earlier versions: an untouched copy is replaced by the new one
# untouched copies of earlier packaged versions: these are upgraded in place; a file the owner edited is never touched.
# Add the old hash here whenever a packaged built-in file changes (test_builtin_files_have_their_old_versions_listed).
OLD_BUILTIN = {"conference/debate.md": {"078c43c9a0ea8b8f0d875e06f84ebaf54cd11557",
                                        "294b51d445bbdccb9f6854fc9d336dc2dbf8905c"}}      # 0.6.x
CURRENT_BUILTIN = {"conference/debate.md": "607dbbedd18c61f724e625afb6b8057256e36c79",
                   "conference/mod.yaml": "5fa5bb450c07e11dd8abf14c26a05e9594241c21",
                   "dispatcher/mod.yaml": "51e42a9afe0d7bdb855ade4c4969909d19a78d1c",
                   "planner/mod.yaml": "5b85781664f49630e6a41f2520faea3fa5eb35c1",
                   "router/mod.yaml": "0ea77abbe51f59a38f29f303afacceff30a5642e"}


def _hash(p: Path) -> str:
    import hashlib
    return hashlib.sha1(p.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
FILE_CHARS = 3000          # cap per attached file
MOD_SYSTEM_CHARS = 4000    # cap per mod's system text (files included)


@dataclass
class Mod:
    name: str
    path: Path
    description: str = ""
    enabled: bool = False
    applies_to: list[str] = field(default_factory=lambda: ["*"])
    system: str = ""
    context: str = ""
    files: list[str] = field(default_factory=list)
    kb: list[dict] = field(default_factory=list)
    tiers: dict = field(default_factory=dict)
    settings: dict = field(default_factory=dict)      # any other keys (routes:, triggers:, model: ...)
    builtin: bool = False
    has_code: bool = False
    error: str = ""
    _module: ModuleType | None = None

    def applies(self, dept: str) -> bool:
        return "*" in self.applies_to or dept in self.applies_to

    def system_text(self) -> str:
        parts = [self.system.strip()]
        for f in self.files:
            p = (self.path / f).resolve()
            if self.path.resolve() in p.parents and p.exists():
                parts.append(f"[{f}]\n{p.read_text(encoding='utf-8', errors='replace')[:FILE_CHARS]}")
        return "\n\n".join(x for x in parts if x)[:MOD_SYSTEM_CHARS]

    @property
    def tokens_per_call(self) -> int:
        """Approximate prompt tokens this mod adds to each call it applies to."""
        return (len(self.system_text()) + len(self.context)) // 4

    def summary(self) -> dict:
        return {"name": self.name, "description": self.description, "enabled": self.enabled,
                "applies_to": self.applies_to, "has_code": self.has_code, "tokens_per_call": self.tokens_per_call,
                "tiers": self.tiers, "kb": len(self.kb), "files": self.files, "error": self.error,
                "builtin": self.builtin, "settings": self.settings}


class Mods:
    def __init__(self, home: Path):
        self.dir = home / "mods"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._stamp = None
        self._mods: dict[str, Mod] = {}
        self._lock = threading.RLock()

    # ---------- loading ----------
    def _fingerprint(self) -> tuple:
        return tuple(sorted((str(p), p.stat().st_mtime_ns) for p in self.dir.glob("*/*") if p.is_file()))

    def all(self) -> dict[str, Mod]:
        # agent workers and the dashboard call this concurrently: build the new set aside, swap it in whole
        with self._lock:
            fp = self._fingerprint()
            if fp != self._stamp:
                fresh = {}
                for y in sorted(self.dir.glob("*/mod.yaml")):
                    m = self._load(y)
                    fresh[m.name] = m
                self._mods, self._stamp = fresh, fp
            return self._mods

    def _load(self, y: Path) -> Mod:
        folder = y.parent
        try:
            d = yaml.safe_load(y.read_text(encoding="utf-8")) or {}
            applies = d.get("applies_to", ["*"])
            m = Mod(name=folder.name, path=folder, description=str(d.get("description", "")),
                    enabled=bool(d.get("enabled", False)),
                    applies_to=[applies] if isinstance(applies, str) else list(applies),
                    system=str(d.get("system") or ""), context=str(d.get("context") or ""),
                    files=list(d.get("files") or []), kb=list(d.get("kb") or []), tiers=dict(d.get("tiers") or {}),
                    settings={k: v for k, v in d.items() if k not in KNOWN_KEYS}, builtin=bool(d.get("builtin")),
                    has_code=(folder / "hooks.py").exists())
        except Exception as e:   # a broken mod never takes the company down
            return Mod(name=folder.name, path=folder, error=f"mod.yaml: {e}")
        if m.enabled and m.has_code:
            try:
                spec = importlib.util.spec_from_file_location(f"agentsorg_mod_{m.name}", folder / "hooks.py")
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                m._module = mod
            except Exception as e:
                m.error = f"hooks.py: {type(e).__name__}: {e}"
        return m

    def active(self, dept: str) -> list[Mod]:
        return [m for m in self.all().values() if m.enabled and not m.error and m.applies(dept)]

    # ---------- what the engine asks ----------
    def system_for(self, dept: str) -> str:
        return "\n\n".join(f"(mod {m.name})\n{t}" for m in self.active(dept) if (t := m.system_text()))

    def context_for(self, dept: str) -> str:
        return "\n".join(m.context.strip() for m in self.active(dept) if m.context.strip())

    def tier(self, dept: str, body: str, tier: int) -> int:
        low = body.lower()
        for m in self.active(dept):
            t = m.tiers
            for word, kt in (t.get("keywords") or {}).items():
                if re.search(rf"\b{re.escape(str(word).lower())}\b", low):
                    tier = int(kt)
            if t.get("force"):
                tier = int(t["force"])
            if t.get("max"):
                tier = min(tier, int(t["max"]))
        return max(1, min(3, tier))

    # ---------- company-level hooks: route / plan / dispatch (the CEO pipeline) ----------
    def enabled(self) -> list[Mod]:
        return [m for m in self.all().values() if m.enabled and not m.error]

    def setting(self, mod: str, key: str, default=None):
        """A setting from an enabled mod's mod.yaml (e.g. router routes:, conference triggers:)."""
        m = self.all().get(mod)
        if not m or not m.enabled or m.error:
            return default
        return m.settings.get(key, default)

    def hook(self, name: str, *args):
        """First enabled mod whose hooks.py defines `name` and returns something wins: (result, mod name).
        A failing hook is recorded on the mod and skipped (the built-in behaviour runs instead)."""
        for m in self.enabled():
            fn = getattr(m._module, name, None) if m._module else None
            if not fn:
                continue
            try:
                r = fn(*args)
            except Exception as e:
                m.error = f"{name}: {type(e).__name__}: {e}"
                continue
            if r is not None:
                return r, m.name
        return None, ""

    def before_task(self, task: dict, org) -> dict:
        """Returns {} or one of {"body"}, {"result"}, {"skip"} (first mod to answer wins for result/skip)."""
        out: dict = {}
        for m in self.active(task["dept"]):
            fn = getattr(m._module, "before_task", None) if m._module else None
            if not fn:
                continue
            try:
                r = fn({**task, **out}, org) or {}
            except Exception as e:
                m.error = f"before_task: {type(e).__name__}: {e}"
                continue
            if "body" in r:
                out["body"] = str(r["body"])
            if "result" in r or "skip" in r:
                return {**out, **{k: r[k] for k in ("result", "skip") if k in r}, "by": m.name}
        return out

    def after_task(self, task: dict, text: str, org) -> str:
        for m in self.active(task["dept"]):
            fn = getattr(m._module, "after_task", None) if m._module else None
            if not fn:
                continue
            try:
                r = fn(task, text, org)
                if isinstance(r, str):
                    text = r
            except Exception as e:
                m.error = f"after_task: {type(e).__name__}: {e}"
        return text

    def fingerprint(self) -> tuple:
        return self._fingerprint()

    def install_builtins(self, state) -> list[str]:
        """Copy the pipeline mods into the company once (enabled). The owner may edit or disable them; a mod the
        owner deleted is not put back."""
        done = set(state.get("builtin_mods", []) or [])
        added = []
        for rel, olds in OLD_BUILTIN.items():
            dest, src = self.dir / rel, BUILTIN / rel
            try:
                if dest.exists() and _hash(dest) in olds and _hash(dest) != _hash(src):
                    shutil.copyfile(src, dest)
                    added.append(f"{rel} (updated)")
            except OSError:
                pass
        for src in sorted(BUILTIN.glob("*/mod.yaml")):
            name = src.parent.name
            if name in done:
                continue
            dest = self.dir / name
            if not dest.exists():
                shutil.copytree(src.parent, dest)
                added.append(name)
            done.add(name)
        state.set("builtin_mods", sorted(done))
        return added

    # ---------- management ----------
    def _yaml_path(self, name: str) -> Path:
        p = self.dir / name / "mod.yaml"
        if not p.exists():
            raise KeyError(f"no mod named {name!r}")
        return p

    def set_enabled(self, name: str, on: bool, org=None) -> Mod:
        p = self._yaml_path(name)
        d = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        d["enabled"] = on
        p.write_text(yaml.safe_dump(d, sort_keys=False, allow_unicode=True), encoding="utf-8")
        m = self.all()[name]
        if on and org is not None:
            for k in m.kb:
                if k.get("title") and k.get("body"):
                    org.kb.add(str(k["title"]), str(k["body"]), tags=f"mod:{name}")
        return m

    def read_yaml(self, name: str) -> str:
        return self._yaml_path(name).read_text(encoding="utf-8")

    def write_yaml(self, name: str, text: str) -> Mod:
        d = yaml.safe_load(text)               # raises on invalid YAML before anything is written
        if not isinstance(d, dict):
            raise ValueError("mod.yaml must be a mapping")
        folder = self.dir / _slug(name)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "mod.yaml").write_text(text, encoding="utf-8")
        return self.all()[folder.name]

    def new(self, name: str, *, code: bool = False, description: str = "", data: dict | None = None) -> Mod:
        slug = _slug(name)
        folder = self.dir / slug
        if folder.exists():
            raise FileExistsError(f"mod {slug!r} already exists")
        body = data or {"description": description or "What this mod does",
                        "enabled": False, "applies_to": ["*"],
                        "system": "# Text here is added to the system prompt of every agent this applies to.\n",
                        "context": "", "files": [], "kb": [], "tiers": {}}
        folder.mkdir(parents=True)
        (folder / "mod.yaml").write_text(yaml.safe_dump(body, sort_keys=False, allow_unicode=True), encoding="utf-8")
        if code:
            (folder / "hooks.py").write_text(HOOKS_TEMPLATE, encoding="utf-8")
        return self.all()[slug]

    def remove(self, name: str) -> None:
        folder = self._yaml_path(name).parent
        shutil.rmtree(folder)
        self._stamp = None

    def add(self, source: str) -> list[str]:
        """Install from a local folder, a .zip file, or an https URL to a .zip. Returns installed names (disabled)."""
        src = Path(source).expanduser()
        if src.is_dir():
            return [self._install_dir(src)]
        if src.is_file() and src.suffix == ".zip":
            data = src.read_bytes()
        elif source.startswith("https://"):
            import httpx
            r = httpx.get(source, timeout=60, follow_redirects=True)
            r.raise_for_status()
            data = r.content
        else:
            raise ValueError("source must be a folder, a .zip file, or an https:// URL to a .zip")
        names = []
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            yamls = [n for n in z.namelist() if n.endswith("mod.yaml") and n.count("/") <= 2]
            if not yamls:
                raise ValueError("zip has no mod.yaml")
            for y in yamls:
                prefix = y[: -len("mod.yaml")]
                name = _slug(Path(prefix).name or Path(source).stem)
                dest = self.dir / name
                if dest.exists():
                    raise FileExistsError(f"mod {name!r} already exists")
                for n in z.namelist():
                    if n.startswith(prefix) and not n.endswith("/"):
                        rel = Path(n[len(prefix):])
                        if ".." in rel.parts or rel.is_absolute():
                            continue              # no path traversal out of the mod folder
                        out = dest / rel
                        out.parent.mkdir(parents=True, exist_ok=True)
                        out.write_bytes(z.read(n))
                self._disable(dest)
                names.append(name)
        self._stamp = None
        return names

    def _install_dir(self, src: Path) -> str:
        if not (src / "mod.yaml").exists():
            raise ValueError(f"{src} has no mod.yaml")
        dest = self.dir / _slug(src.name)
        if dest.exists():
            raise FileExistsError(f"mod {dest.name!r} already exists")
        shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__"))
        self._disable(dest)
        self._stamp = None
        return dest.name

    def _disable(self, folder: Path) -> None:
        """Installed mods start disabled: you read them first, then enable."""
        p = folder / "mod.yaml"
        d = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        d["enabled"] = False
        p.write_text(yaml.safe_dump(d, sort_keys=False, allow_unicode=True), encoding="utf-8")


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40] or "mod"


CATALOG = PKG_DATA / "mods"


def catalog(installed: set[str] | None = None) -> list[dict]:
    """Mods shipped with agentsorg that a user may choose to install. Nothing is installed by default."""
    out = []
    for y in sorted(CATALOG.glob("*/mod.yaml")):
        d = yaml.safe_load(y.read_text(encoding="utf-8")) or {}
        m = Mod(name=y.parent.name, path=y.parent, system=str(d.get("system") or ""),
                context=str(d.get("context") or ""), files=list(d.get("files") or []))
        out.append({"name": m.name, "description": str(d.get("description", "")), "tokens_per_call": m.tokens_per_call,
                    "has_code": (y.parent / "hooks.py").exists(), "installed": m.name in (installed or set())})
    return out


def install_from_catalog(home: Path, name: str) -> Path:
    src = CATALOG / name
    if not (src / "mod.yaml").exists():
        raise KeyError(f"no catalog mod named {name!r}")
    dest = home / "mods" / name
    if dest.exists():
        raise FileExistsError(f"mod {name!r} is already installed")
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__"))
    return dest


HOOKS_TEMPLATE = '''"""Hooks for this mod. Runs inside agentsorg with your permissions."""


def before_task(task, org):
    """task: dict with dept, member, body, tier, source...
    Return None to do nothing, or one of:
      {"body": "rewritten task"}        change what the agent sees
      {"result": "answer"}              answer it yourself: no model call, 0 tokens
      {"skip": "reason"}                refuse the task
    """
    return None


def after_task(task, text, org):
    """Return a string to replace the agent's result, or None to keep it."""
    return None
'''


MOD_DESIGN_PROMPT = """You write agentsorg mods: small YAML files that change how a virtual company's Claude agents work.
Output ONLY the YAML, no prose, no code fences. Keys:
description: one line
enabled: false
applies_to: ["*"] or a list of department ids from DEPARTMENTS
system: |   instructions added to the agents' system prompt. Keep it under 120 words: every word costs tokens
            on every call.
context: ""  short per-task reminder only if truly needed, else ""
kb: []       up to 3 {title, body} facts worth sharing company-wide, only if the request contains facts
tiers: {}    optional: {force: 1|2|3} or {max: 1|2} or {keywords: {"word": 1|2|3}} (1 Haiku, 2 Sonnet, 3 Opus)
Prefer the fewest words that achieve the user's goal."""


def design_mod(org, description: str) -> str:
    """One Haiku call turns a plain description into a mod.yaml text."""
    depts = ", ".join(org.c.depts)
    res = org.call(f"DEPARTMENTS: {depts}\nWHAT THE MOD SHOULD DO:\n{description.strip()[:1500]}",
                   model=org.c.models[1], system=MOD_DESIGN_PROMPT, dept="_mods", task_id=None, auto=False)
    if not res.ok:
        raise RuntimeError(res.error or "mod design failed")
    text = re.sub(r"^```(?:yaml)?\s*|\s*```$", "", res.text.strip())
    d = yaml.safe_load(text)
    if not isinstance(d, dict):
        raise ValueError("the model did not return a YAML mapping")
    d["enabled"] = False
    return yaml.safe_dump(d, sort_keys=False, allow_unicode=True)


if __name__ == "__main__":   # pragma: no cover
    sys.exit("use `agentsorg mods`")
