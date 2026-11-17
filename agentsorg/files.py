"""Department folders + folders linked from other PCs, synced over plain `ssh` + `tar` (nothing to install on the PCs).

<company>/
  <dept>/sources/<name>/   copy of a folder that lives on PC 1 / PC 2, or a link to a folder on this machine
                           (so the whole project is reachable from the company folder, nothing copied)
  <dept>/outputs/          every finished task, saved as a file
  shared/                  company-wide files

PC -> company:  tar -czf - -C <folder> . | ssh <server> agentsorg files receive <name>
company -> PC:  ssh <server> agentsorg files send <name> | tar -xzf - -C <folder>

The server keeps the state of the last sync (a hash per file). A file changed on both sides is never
overwritten: the agents' version stays and the PC's version is saved beside it as `<name>.from-pc<ext>`.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import re
import tarfile
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

EXCLUDE = ["node_modules", ".git", ".venv", "venv", "__pycache__", "dist", "build", ".next", "target", ".DS_Store",
           "Thumbs.db", ".idea", ".vscode", "*.pyc"]
CONFLICT_TAG = ".from-pc"
MAX_SYNC_FILES = 20_000
MAX_SYNC_BYTES = 2 * 2**30
MAX_RECEIVE_BYTES = 512 * 2**20     # one push is held in memory while it is checked


@dataclass
class Source:
    name: str
    dept: str
    origin: str
    exclude: list[str]
    mode: str = "sync"          # sync = a copy in <dept>/sources/<name>; direct = the real folder on this machine
    writable: bool = False      # direct folders are read-only for agents unless you opt in


def too_broad(path: Path, company: Path) -> str:
    """Why a folder must not be linked for agents to read ('' if fine): a home, a drive root, a secrets folder,
    or the company itself."""
    p, home, co = Path(path).resolve(), Path.home().resolve(), Path(company).resolve()
    if p == home or len(p.parts) <= 2 or p in co.parents or p == co:
        return f"{p} is too broad or is the company itself; link a specific project folder"
    for inner in ("data", "mods", "prompts"):          # the company's own private parts
        if (co / inner) in (p, *p.parents):
            return f"{p} is part of the company's private data; agents may not read it"
    for secret in (".ssh", ".claude", ".aws", ".gnupg", ".config"):
        if (home / secret).resolve() in (p, *p.parents):
            return f"{p} holds credentials; agents may not read it"
    return ""


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40] or "source"


def _excluded(rel: str, patterns: list[str]) -> bool:
    parts = PurePosixPath(rel).parts
    for p in patterns:
        if any(PurePosixPath(x).match(p) for x in parts) or PurePosixPath(rel).match(p):
            return True
    return False


def _hash(p: Path) -> str:
    h = hashlib.sha1()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class Files:
    def __init__(self, company):
        self.c = company
        self.root = company.home
        self.state_dir = company.home / "data" / "sources"

    # ---------- layout ----------
    def dept_dir(self, dept: str) -> Path:
        d = self.c.dept_dir(dept)
        for sub in ("sources", "outputs"):
            (d / sub).mkdir(parents=True, exist_ok=True)
        return d

    def ensure_layout(self) -> None:
        self.c.shared_dir.mkdir(parents=True, exist_ok=True)
        for d in self.c.depts:
            self.dept_dir(d)
        self.sync_links()

    # ---------- one place: <dept>/sources/<name> links to live folders ----------
    def link_path(self, s: Source) -> Path:
        return self.c.home / s.dept / "sources" / s.name

    @staticmethod
    def _is_link(p: Path) -> bool:
        return p.is_symlink() or bool(getattr(p, "is_junction", lambda: False)())

    @staticmethod
    def _unlink(p: Path) -> None:
        try:
            p.unlink()
        except OSError:
            os.rmdir(p)                                   # a Windows junction

    @staticmethod
    def _make_link(link: Path, target: Path) -> bool:
        try:
            os.symlink(target, link, target_is_directory=True)
            return True
        except OSError:
            if os.name == "nt":                           # symlinks need admin/dev mode; junctions don't
                r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
                return r.returncode == 0
            return False

    def sync_links(self) -> dict:
        """Make <dept>/sources/<name> point at every live folder; drop links to folders no longer linked.
        Only ever creates or removes links, never a real file or folder."""
        made, dropped, failed = [], [], []
        want = {}
        for s in self.sources().values():
            if s.mode == "direct" and (target := self.local_path(s)):
                want[self.link_path(s)] = target
        for d in self.c.depts:
            src = self.c.home / d / "sources"
            if not src.is_dir():
                continue
            for p in src.iterdir():
                if self._is_link(p) and p not in want:
                    self._unlink(p)
                    dropped.append(str(p.relative_to(self.c.home)))
        for link, target in want.items():
            if self._is_link(link):
                try:
                    if Path(os.path.realpath(link)) == target:
                        continue
                except OSError:
                    pass
                self._unlink(link)
            elif link.exists():
                continue                                  # a real folder already sits there: leave it alone
            link.parent.mkdir(parents=True, exist_ok=True)
            (made if self._make_link(link, target) else failed).append(str(link.relative_to(self.c.home)))
        return {"made": made, "dropped": dropped, "failed": failed}

    # ---------- registry (company.yaml -> sources:) ----------
    def sources(self) -> dict[str, Source]:
        raw = self.c.raw.get("sources") or {}
        return {n: Source(n, v.get("dept", ""), v.get("origin", ""), list(v.get("exclude") or EXCLUDE),
                          v.get("mode", "sync"), bool(v.get("writable", False)))
                for n, v in raw.items()}

    def get(self, name: str) -> Source:
        s = self.sources().get(name)
        if not s:
            raise KeyError(f"no linked folder named {name!r} (see: agentsorg files list)")
        return s

    def add(self, name: str, dept: str, origin: str = "", exclude: list[str] | None = None,
            mode: str | None = None, writable: bool = False) -> Source:
        """Link a folder. On this machine it is used in place (direct, no copies); elsewhere it is synced."""
        if dept not in self.c.depts:
            raise KeyError(f"unknown department {dept!r}")
        name = _slug(name)
        if not name:
            raise ValueError("the folder link needs a name")
        if name in self.sources():
            raise FileExistsError(f"{name!r} is already linked")
        probe = Source(name, dept, origin, exclude or EXCLUDE)
        if (lp := self.local_path(probe)) and (why := too_broad(lp, self.c.home)):
            raise ValueError(why)
        mode = mode or ("direct" if self.local_path(probe) else "sync")
        if mode == "direct" and not self.local_path(probe):
            raise ValueError(f"{origin!r} is not a folder on this machine, so it can't be used directly")
        entry = {"dept": dept, "origin": origin, "exclude": exclude or EXCLUDE, "mode": mode}
        if writable:
            entry["writable"] = True
        self.c.raw.setdefault("sources", {})[name] = entry
        self.c.save()
        if mode == "sync":
            self.path(self.get(name)).mkdir(parents=True, exist_ok=True)
        else:
            self.sync_links()
        return self.get(name)

    def remove(self, name: str, delete_files: bool = False) -> None:
        s = self.get(name)
        if delete_files and s.mode == "sync":            # never delete a live folder: it is the real project
            import shutil
            shutil.rmtree(self.path(s), ignore_errors=True)
        (self.state_dir / f"{name}.json").unlink(missing_ok=True)
        self.c.raw["sources"].pop(name, None)
        self.c.save()
        self.sync_links()

    def path(self, s: Source) -> Path:
        if s.mode == "direct":
            return self.local_path(s) or (self.dept_dir(s.dept) / "sources" / s.name)
        return self.dept_dir(s.dept) / "sources" / s.name

    def _require_sync(self, s: Source) -> None:
        if s.mode == "direct":
            raise ValueError(f"{s.name} is a live folder on this machine ({self.path(s)}): agents use it directly, "
                             "there is nothing to sync")

    def depts_with_sources(self) -> set[str]:
        return {s.dept for s in self.sources().values()}

    # ---------- state ----------
    def _base(self, name: str) -> dict:
        p = self.state_dir / f"{name}.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"files": {}, "synced": 0}

    def _save_base(self, name: str, files: dict) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / f"{name}.json").write_text(json.dumps({"files": files, "synced": time.time()}), encoding="utf-8")

    def _current(self, s: Source) -> dict[str, str]:
        root = self.path(s)
        out = {}
        for p in root.rglob("*"):
            if p.is_file():
                rel = p.relative_to(root).as_posix()
                if not _excluded(rel, s.exclude) and CONFLICT_TAG not in p.name:
                    out[rel] = _hash(p)
        return out

    def status(self, name: str) -> dict:
        s = self.get(name)
        if s.mode == "direct":
            q = self.quick_status(name)
            return {"name": name, "dept": s.dept, "origin": s.origin, "files": q["files"], "synced": 0, "mode": "direct",
                    "writable": s.writable, "path": str(self.path(s)), "changed_by_agents": [], "deleted_by_agents": [],
                    "conflicts": []}
        base, cur = self._base(name)["files"], self._current(s)
        return {"name": name, "dept": s.dept, "origin": s.origin, "files": len(cur),
                "synced": self._base(name)["synced"],
                "changed_by_agents": sorted(k for k, h in cur.items() if base.get(k) != h),
                "deleted_by_agents": sorted(k for k in base if k not in cur),
                "conflicts": sorted(p.relative_to(self.path(s)).as_posix() for p in self.path(s).rglob(f"*{CONFLICT_TAG}*"))}

    # ---------- PC -> company ----------
    def receive(self, name: str, stream) -> dict:
        """Apply a tar(.gz) snapshot of the PC folder. Never overwrites a file the agents changed since last sync."""
        s = self.get(name)
        self._require_sync(s)
        root = self.path(s)
        base = self._base(name)["files"]
        cur = self._current(s)
        incoming: dict[str, bytes] = {}
        total = 0
        root_r = root.resolve()
        with tarfile.open(fileobj=stream, mode="r|*") as tar:
            for m in tar:
                if not m.isfile():
                    continue
                total += m.size
                if total > MAX_RECEIVE_BYTES or len(incoming) >= MAX_SYNC_FILES:
                    raise ValueError(f"this push is too big (over {MAX_RECEIVE_BYTES // 2**20} MB or "
                                     f"{MAX_SYNC_FILES:,} files); link a more specific folder")
                member = m.name.replace("\\", "/")
                while member.startswith("./"):            # tar -C dir . writes ./a/b
                    member = member[2:]
                rel = PurePosixPath(member)
                if rel.is_absolute() or ".." in rel.parts or not member or ":" in rel.parts[0] \
                        or _excluded(str(rel), s.exclude):
                    continue                                   # no path traversal, skip junk folders
                f = tar.extractfile(m)
                if f:
                    incoming[str(rel)] = f.read()
        report = {"updated": [], "added": [], "unchanged": 0, "conflicts": [], "deleted": [], "kept_agent_files": []}
        new_base = {}
        for rel, data in incoming.items():
            h = hashlib.sha1(data).hexdigest()
            dest = root / rel
            if root_r not in dest.resolve().parents:          # a link planted inside the copy can't redirect writes
                continue
            agent_changed = rel in cur and base.get(rel) != cur[rel]
            if rel in cur and cur[rel] == h:
                report["unchanged"] += 1
            elif agent_changed and base.get(rel) != h:
                side = dest.with_name(dest.stem + CONFLICT_TAG + dest.suffix)   # both changed: keep both
                side.write_bytes(data)
                report["conflicts"].append(rel)
                new_base[rel] = h          # the PC has its own version; `send` holds this file until resolved
                continue
            elif agent_changed:
                report["kept_agent_files"].append(rel)         # PC unchanged since sync; agents' edit wins
                new_base[rel] = base[rel]
                continue
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
                report["updated" if rel in cur else "added"].append(rel)
            new_base[rel] = h
        for rel in cur:                                         # deleted on the PC
            if rel not in incoming:
                if base.get(rel) == cur[rel]:
                    (root / rel).unlink(missing_ok=True)
                    report["deleted"].append(rel)
                else:
                    report["kept_agent_files"].append(rel)      # created or edited by agents: keep, send back
        self._save_base(name, new_base)
        return report

    # ---------- company -> PC ----------
    def send(self, name: str, out, everything: bool = False) -> dict:
        """Write a tar.gz of the files agents changed (or everything) to `out`."""
        s = self.get(name)
        self._require_sync(s)
        st = self.status(name)
        held = {c.replace(CONFLICT_TAG, "") for c in st["conflicts"]}   # unresolved: never overwrite the PC's edit
        rels = [r for r in (sorted(self._current(s)) if everything else st["changed_by_agents"]) if r not in held]
        root = self.path(s)
        with tarfile.open(fileobj=out, mode="w:gz") as tar:
            for rel in rels:
                tar.add(str(root / rel), arcname=rel)
        base = self._base(name)["files"]
        cur = self._current(s)
        for rel in rels:                                        # the PC now has these versions
            base[rel] = cur[rel]
        for rel in st["deleted_by_agents"]:
            base.pop(rel, None)
        self._save_base(name, base)
        return {"sent": rels, "held_conflicts": sorted(held), "deleted_by_agents": st["deleted_by_agents"]}

    def quick_status(self, name: str) -> dict:
        """Cheap status (no hashing) for prompts: file count and last sync from the saved state."""
        s = self.get(name)
        if s.mode == "direct":
            n = 0
            for dirpath, dirnames, filenames in os.walk(self.path(s)):
                dirnames[:] = [d for d in dirnames if not d.startswith(".") and not _excluded(d, s.exclude)]
                n += len(filenames)
                if n >= 5000:
                    break
            return {"files": min(n, 5000), "synced": "live (used in place)"}
        b = self._base(name)
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(b["synced"])) if b["synced"] else "never"
        return {"files": len(b["files"]), "synced": when}

    def local_path(self, s: Source) -> Path | None:
        """The folder on THIS machine a source comes from, if it is here (origin = "<label> <path>")."""
        raw = s.origin.split(" ", 1)[-1].strip() if " " in s.origin else s.origin.strip()
        if not raw:
            return None
        p = Path(raw).expanduser()
        return p.resolve() if p.is_dir() else None

    # ---------- same machine: no tar pipes, no ssh ----------
    def receive_dir(self, name: str, folder: str | Path) -> dict:
        """Push from a folder on this machine (tar built in a temp file, then the normal safe receive)."""
        import tempfile
        src = Path(folder).expanduser().resolve()
        if not src.is_dir():
            raise ValueError(f"{src} is not a folder")
        s = self.get(name)
        self._require_sync(s)
        count = size = 0
        for p in src.rglob("*"):
            if p.is_file() and not _excluded(p.relative_to(src).as_posix(), s.exclude):
                count += 1
                size += p.stat().st_size
                if count > MAX_SYNC_FILES or size > MAX_SYNC_BYTES:
                    raise ValueError(f"{src} is too big to copy in at once (over {MAX_SYNC_FILES:,} files or "
                                     f"{MAX_SYNC_BYTES // 2**30} GB); link a more specific folder")
        with tempfile.TemporaryFile() as tmp:
            with tarfile.open(fileobj=tmp, mode="w:gz") as tar:
                for p in src.rglob("*"):
                    rel = p.relative_to(src).as_posix()
                    if p.is_file() and not _excluded(rel, s.exclude):
                        tar.add(str(p), arcname=rel)
            tmp.seek(0)
            return self.receive(name, tmp)

    def send_dir(self, name: str, folder: str | Path, everything: bool = False) -> dict:
        """Pull the agents' changes into a folder on this machine."""
        dest = Path(folder).expanduser().resolve()
        dest.mkdir(parents=True, exist_ok=True)
        buf = io.BytesIO()
        rep = self.send(name, buf, everything=everything)
        buf.seek(0)
        with tarfile.open(fileobj=buf, mode="r:gz") as tar:
            for m in tar:
                out = (dest / m.name).resolve()
                if m.isfile() and dest in out.parents:
                    out.parent.mkdir(parents=True, exist_ok=True)
                    out.write_bytes(tar.extractfile(m).read())
        return rep

    # ---------- the one-liners a PC needs ----------
    def commands(self, name: str, server: str = "home", folder: str = "") -> dict:
        s = self.get(name)
        if s.mode == "direct":
            return {"note": f"Live folder: agents use {self.path(s)} directly. Nothing to sync."}
        exe = f"{remote_bin()} --company {self.c.slug}"      # explicit: one machine can run several companies
        folder = folder or s.origin.split(" ", 1)[-1] or "<your folder>"
        ex_ps = " ".join(f'--exclude="{e}"' for e in s.exclude)
        ex_sh = " ".join(f"--exclude='{e}'" for e in s.exclude)   # quoted so the shell doesn't expand *.pyc
        up, down = f"agentsorg-{name}.tgz", f"agentsorg-{name}-out.tgz"
        tmp_up, tmp_down = "$env:TEMP\\" + up, "$env:TEMP\\" + down
        # Windows' built-in ssh.exe can hang when its stdin is piped, so Windows moves a temp file with scp instead
        return {
            "push_powershell": (f'tar -czf "{tmp_up}" {ex_ps} -C "{folder}" . ; scp "{tmp_up}" {server}:{up} ; '
                                f'ssh {server} {exe} files receive {name} --file {up}'),
            "pull_powershell": (f'ssh {server} {exe} files send {name} --file {down} ; scp {server}:{down} "{tmp_down}" ; '
                                f'tar -xzf "{tmp_down}" -C "{folder}"'),
            "push_bash": f"tar -czf - {ex_sh} -C '{folder}' . | ssh {server} {exe} files receive {name}",
            "pull_bash": f"ssh {server} {exe} files send {name} | tar -xzf - -C '{folder}'",
            "push_local": f'{exe} files receive {name} --from "{folder}"',
            "pull_local": f'{exe} files send {name} --to "{folder}"',
            "note": "Push first, then pull. Push never overwrites the agents' edits; pull returns only what they changed.",
        }

    # ---------- outputs ----------
    def save_output(self, task: dict, result: str) -> Path | None:
        if not result.strip() or task["dept"] not in self.c.depts:
            return None
        day = time.strftime("%Y-%m-%d")
        slug = re.sub(r"[^a-z0-9]+", "-", (task.get("title") or "task").lower()).strip("-")[:50] or "task"
        p = self.dept_dir(task["dept"]) / "outputs" / f"{day}_{task['id']}_{slug}.md"
        header = (f"<!-- task #{task['id']} · {task['dept']}/{task['member']} · {task.get('model') or ''} -->\n"
                  f"# {task.get('title') or 'Task'}\n\n")
        p.write_text(header + result.strip() + "\n", encoding="utf-8")
        return p

    def outputs(self, dept: str, limit: int = 20) -> list[dict]:
        d = self.dept_dir(dept) / "outputs"
        files = sorted(d.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
        return [{"name": p.name, "size": p.stat().st_size, "mtime": p.stat().st_mtime} for p in files]

    def read_output(self, dept: str, name: str) -> str:
        d = (self.dept_dir(dept) / "outputs").resolve()
        p = (d / name).resolve()
        if d not in p.parents or not p.is_file():
            raise KeyError("no such output")
        return p.read_text(encoding="utf-8", errors="replace")


def tar_bytes(files: dict[str, bytes]) -> io.BytesIO:
    """Helper for tests: build a tar.gz in memory."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for rel, data in files.items():
            info = tarfile.TarInfo(rel)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    buf.seek(0)
    return buf


def remote_bin() -> str:
    """How a PC should call agentsorg over ssh. Non-interactive ssh sessions often lack ~/.local/bin on PATH
    (pipx / install.sh put it there), so use the full home-relative path when that's where it lives."""
    import os
    if os.environ.get("AGENTSORG_REMOTE_BIN"):
        return os.environ["AGENTSORG_REMOTE_BIN"]
    return "~/.local/bin/agentsorg" if (Path.home() / ".local" / "bin" / "agentsorg").exists() else "agentsorg"
