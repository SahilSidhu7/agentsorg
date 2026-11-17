"""Bring an existing project in: read it once, write a compact context pack every agent gets by reference.

1. scan (deterministic, 0 tokens): file tree, every CLAUDE.md / AGENTS.md, README, manifests, deploy/CI config,
   .env.example key NAMES, recent git history. Secret files are skipped by name and never read.
2. one Sonnet call turns the scan into shared/context/project.md (the whole picture) plus a short
   shared/context/<dept>.md slice per department. Without a model (no CLI, over budget) a deterministic pack is
   written instead, so agents still start informed.
3. a fingerprint (mtimes + git HEAD) skips rebuilding when nothing changed; the daily job refreshes at most once a day.

Agents get their department slice in the (prompt-cached) system prompt and the path of the full pack; tool agents
read more only when they need it, instead of exploring the project on every task.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from pathlib import Path

from .design import OUTLINE_SKIP, folder_outline

CONTEXT_FILES = ("CLAUDE.md", "AGENTS.md", "CLAUDE.local.md")
MANIFESTS = ("package.json", "pyproject.toml", "requirements.txt", "requirements-dev.txt", "go.mod", "Cargo.toml",
             "Gemfile", "composer.json", "pom.xml", "build.gradle", "Dockerfile", "docker-compose.yml",
             "docker-compose.yaml", "compose.yaml", "Makefile", "Procfile", "fly.toml", "vercel.json",
             "netlify.toml", "wrangler.toml", "wrangler.jsonc", "render.yaml", "app.json", "manage.py")
ENV_EXAMPLES = (".env.example", ".env.sample", ".env.template", "example.env")
SECRET_NAME = re.compile(r"^(\.env(\..*)?|.*\.(pem|key|p12|pfx|keystore|jks)|id_(rsa|ed25519|ecdsa|dsa).*|"
                         r".*credentials.*|.*secrets?\..*|\.npmrc|\.pypirc|\.netrc)$", re.I)
CAPS = {"context": 4000, "readme": 3000, "manifest": 1500, "ci": 600}
MAX_SCAN = 24000                  # characters handed to the one model call
DEPT_SLICE = 2500                 # per-department slice that goes into every system prompt
PACK_PROMPT = """You write the context pack a team of AI agents reads before working on this project.
Be factual: only what the scan shows. No marketing, no guesses presented as facts (say "unclear" instead).
Write markdown with exactly these parts:
# Project
What it is, who it's for, current stage (2-4 sentences).
## Stack and structure
Languages, frameworks, services; a short map of the important folders/files and what they hold.
## How to run, test and deploy
Commands and where they come from (scripts, Makefile, CI). Say "not found" when missing.
## Conventions and rules
Everything the CLAUDE.md / AGENTS.md files require, plus coding/naming conventions you can see. Keep the rules.
## Open work and risks
TODOs, recent changes from the git log, obvious gaps.
Then, for EACH department listed under DEPARTMENTS, a section:
## DEPT <id>
5-10 bullets: only what that department needs from this project (files to look at, facts, rules, cautions).
Total under 1600 words."""


def _skip_dir(name: str) -> bool:
    return name in OUTLINE_SKIP or name.startswith(".") and name not in (".github", ".claude")


def _read(p: Path, cap: int) -> str:
    try:
        t = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return t[:cap] + ("\n…(truncated)" if len(t) > cap else "")


def _env_names(p: Path) -> str:
    names = []
    for line in _read(p, 6000).splitlines():
        m = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if m:
            names.append(m.group(1))
    return ", ".join(names[:60])


def _git(root: Path) -> tuple[str, str]:
    """(recent history, HEAD). Remote URLs lose any user:password part."""
    def run(*a):
        try:
            r = subprocess.run(["git", "-C", str(root), *a], capture_output=True, text=True, timeout=15,
                               encoding="utf-8", errors="replace")
            return r.stdout.strip() if r.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            return ""
    head = run("rev-parse", "HEAD")
    if not head:
        return "", ""
    log = run("log", "--oneline", "-30")
    remote = re.sub(r"//[^/@\s]+@", "//", run("remote", "get-url", "origin"))
    return (f"remote: {remote}\n" if remote else "") + log, head


def find_files(root: Path, max_depth: int = 4) -> dict[str, list[Path]]:
    found: dict[str, list[Path]] = {"context": [], "readme": [], "manifest": [], "env": [], "ci": []}

    def walk(d: Path, depth: int):
        try:
            entries = sorted(d.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            return
        for p in entries:
            if p.is_symlink():
                continue
            if p.is_dir():
                if depth < max_depth and not _skip_dir(p.name):
                    walk(p, depth + 1)
                elif p.name == ".claude" and (p / "CLAUDE.md").is_file():
                    found["context"].append(p / "CLAUDE.md")
                continue
            if SECRET_NAME.match(p.name) and p.name.lower() not in ENV_EXAMPLES:
                continue                                   # never read secrets, not even to summarise them
            if p.name in CONTEXT_FILES:
                found["context"].append(p)
            elif p.name.lower().startswith("readme") and depth <= 1:
                found["readme"].append(p)
            elif p.name in MANIFESTS and depth <= 2:
                found["manifest"].append(p)
            elif p.name.lower() in ENV_EXAMPLES:
                found["env"].append(p)
            elif ".github" in p.parts and p.suffix in (".yml", ".yaml"):
                found["ci"].append(p)
    walk(root, 0)
    found["context"] = found["context"][:12]
    found["manifest"] = found["manifest"][:10]
    found["ci"] = found["ci"][:5]
    return found


def scan_folder(root: Path) -> tuple[str, list[tuple[str, float]]]:
    """(scan text, [(file, mtime)] for the fingerprint)."""
    root = Path(root).resolve()
    parts = [folder_outline(str(root), label=root.name, depth=3, max_lines=70)]
    f = find_files(root)
    seen: list[tuple[str, float]] = []

    def add(kind: str, p: Path, body: str):
        rel = p.relative_to(root).as_posix()
        parts.append(f"--- {kind}: {rel}\n{body}")
        try:
            seen.append((str(p), p.stat().st_mtime))
        except OSError:
            pass
    for p in f["context"]:
        add("RULES FILE", p, _read(p, CAPS["context"]))
    for p in f["readme"]:
        add("README", p, _read(p, CAPS["readme"]))
    for p in f["manifest"]:
        add("MANIFEST", p, _read(p, CAPS["manifest"]))
    for p in f["env"]:
        add("ENV KEY NAMES (values never read)", p, _env_names(p))
    for p in f["ci"]:
        add("CI/DEPLOY", p, _read(p, CAPS["ci"]))
    log, head = _git(root)
    if log:
        parts.append(f"--- GIT (recent commits)\n{log}")
    seen.append(("git:" + str(root), float(int(head[:8], 16)) if head else 0.0))
    return "\n\n".join(parts), seen


class Intake:
    def __init__(self, org):
        self.org = org
        self.dir = org.c.shared_dir / "context"

    # ---------- what to read ----------
    def folders(self) -> list[Path]:
        """Every live folder linked on this machine (the project), plus synced copies that have files."""
        out = []
        for s in self.org.files.sources().values():
            p = self.org.files.path(s)
            if p.is_dir():
                out.append(p.resolve())
        return out

    def scan(self) -> tuple[str, str]:
        texts, seen = [], []
        for root in self.folders():
            t, s = scan_folder(root)
            texts.append(t)
            seen += s
        fp = hashlib.sha1(json.dumps(sorted(seen)).encode()).hexdigest()
        return "\n\n".join(texts)[:MAX_SCAN], fp

    # ---------- the pack ----------
    def pack_path(self, dept: str | None = None) -> Path:
        return self.dir / (f"{dept}.md" if dept else "project.md")

    def status(self) -> dict:
        meta = self.org.s.get("intake", {}) or {}
        return {"built": meta.get("built"), "fingerprint": meta.get("fp"), "llm": meta.get("llm", False),
                "folders": [str(p) for p in self.folders()], "exists": self.pack_path().exists(),
                "tokens": meta.get("tokens", 0)}

    def build(self, force: bool = False, scheduled: bool = False) -> dict:
        if not self.folders():
            return {"skipped": "no project folders linked yet"}
        scan, fp = self.scan()
        meta = self.org.s.get("intake", {}) or {}
        if not force and meta.get("fp") == fp and self.pack_path().exists():
            return {"skipped": "nothing changed since the last read", "built": meta.get("built")}
        depts = "\n".join(f"{d.id}: {d.name}. {d.mission}" for d in self.org.c.depts.values())
        text, llm, tokens = "", False, 0
        ok = self.org.exe and (self.org.budget.check(auto=True, scheduled=True) if scheduled
                               else self.org.budget.check(auto=False))[0]
        if ok:
            res = self.org.call(f"DEPARTMENTS:\n{depts}\n\nSCAN:\n{scan}", model=self.org.c.models[2],
                                system=PACK_PROMPT, dept="_intake", task_id=None, auto=scheduled)
            if res.ok and "# " in res.text:
                text, llm, tokens = self.org.env.redact(res.text.strip()), True, res.tokens
        if not text:                                   # no model available: a plain pack still beats exploring
            text = "# Project\n(automatic summary unavailable; raw scan below)\n\n" + scan[:12000]
        self._write(text)
        self.org.s.set("intake", {"fp": fp, "built": time.time(), "llm": llm, "tokens": tokens})
        self.org._sys_cache.clear()
        return {"built": time.time(), "llm": llm, "tokens": tokens, "chars": len(text)}

    def _write(self, text: str) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        main, _, rest = text.partition("\n## DEPT ")
        sections = ("## DEPT " + rest).split("\n## DEPT ") if rest else []
        self.pack_path().write_text(main.strip() + "\n", encoding="utf-8")
        for sec in sections:
            sec = sec.removeprefix("## DEPT ").strip()
            if not sec:
                continue
            head, _, body = sec.partition("\n")
            did = re.sub(r"[^a-z0-9_]", "", head.strip().split()[0].lower()) if head.strip() else ""
            if did in self.org.c.depts:
                self.pack_path(did).write_text(body.strip()[:DEPT_SLICE] + "\n", encoding="utf-8")

    # ---------- what agents get ----------
    def system_text(self, dept: str) -> str:
        """Stable text for the system prompt (cached): the department's slice + where the full pack is."""
        if not self.pack_path().exists():
            return ""
        slice_p = self.pack_path(dept)
        if slice_p.exists():
            body = slice_p.read_text(encoding="utf-8")[:DEPT_SLICE]
        else:
            body = self.pack_path().read_text(encoding="utf-8")[:DEPT_SLICE]
        return (f"PROJECT CONTEXT (from the project's own files, incl. its CLAUDE.md rules; follow those rules):\n"
                f"{body.strip()}\nFull project context: {self.pack_path()}")

    def files(self) -> list[Path]:
        return [p for p in [self.pack_path(), *(self.pack_path(d) for d in self.org.c.depts)] if p.exists()]

    def design_brief(self) -> str:
        p = self.pack_path()
        return p.read_text(encoding="utf-8")[:6000] if p.exists() else ""

    # ---------- daily refresh ----------
    def jobs(self) -> list:
        from .maintenance import once
        meta = self.org.s.get("intake", {}) or {}
        if not meta and self.folders() and not self.pack_path().exists() and once(self.org, "first_intake", "week"):
            return [("first project read", lambda: self.build(force=True))]   # companies older than intake
        if not meta or not self.folders():
            return []
        if time.time() - (meta.get("built") or 0) < 86400 or not once(self.org, "last_intake_check"):
            return []
        return [("context refresh", lambda: self.build(scheduled=True))]
