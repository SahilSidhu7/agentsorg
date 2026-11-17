"""Change requests: agents change the real product on a branch, the owner reviews the diff and merges.

When a department that can edit files works on a project, and one of its live folders is a git repo, the project
gets a worktree of that repo on branch `agentsorg/p<project id>` (under <company>/worktrees/). Agents edit there; the
real working tree stays read-only to them. When the project ends, its work is committed on the branch and the owner
sees "Changes ready" with the diff, Merge and Discard. Nothing is ever pushed, and nothing merges without a click.

Each linked folder can also have a test command (company.yaml sources.<name>.test). Agents are told how to run the
tests, the acceptance review runs them on the branch, and the owner can run them from the Files page.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path

log = logging.getLogger("agentsorg")
DIFF_CHARS = 200_000
TEST_TIMEOUT = 300
GIT_ID = ["-c", "user.name=agentsorg", "-c", "user.email=agentsorg@localhost"]
TRACK_EXCLUDE = ["node_modules/", ".venv/", "venv/", "__pycache__/", "*.pyc", "dist/", "build/", ".cache/", ".next/",
                 ".env", ".env.*", "!.env.example", "*.pem", "*.key", "*.p12", "id_rsa*", "id_ed25519*", "*.db",
                 "*.sqlite", "*.sqlite3", "*.db-wal", "*.db-shm", "*.log", "*.bak*", "*.tar", "*.tar.gz", "*.zip"]
ENV_HINTS = [
    (re.compile(r"No module named '?([\w.]+)'?"),
     "The tests need the Python package \"{0}\", which isn't installed where they run. Either give the project its "
     "own environment (in the project folder: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt "
     "pytest`, then use `.venv/bin/python -m pytest -q` as the test command), or run them in the project's container "
     "(`docker compose exec -T <service> pytest -q`)."),
    (re.compile(r"permission denied.*docker|docker.*permission denied|Got permission denied while trying to connect "
                r"to the Docker daemon", re.I),
     "Agents can't use Docker: this user isn't allowed to. On the server run `sudo usermod -aG docker $USER` and "
     "restart the agentsorg service, or use a test command that doesn't need Docker."),
    (re.compile(r"command not found|is not recognized as an internal or external command", re.I),
     "The test command uses a program that isn't installed where agents run. Check the test command on the Files page."),
]


def git(repo: Path | str, *args: str, timeout: int = 60) -> tuple[int, str]:
    try:
        p = subprocess.run(["git", "-C", str(repo), *GIT_ID, *args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except (OSError, subprocess.SubprocessError) as e:
        return 1, f"git failed: {e}"


def keep_line_endings(wt: Path | str, base: str) -> int:
    """Agents' editors write LF; a CRLF file rewritten as LF shows every line as changed. Put each changed file
    back to the line endings it had at `base`, so the diff the owner reviews shows only real changes."""
    rc, names = git(wt, "-c", "core.autocrlf=false", "diff", "--name-only", base)   # see endings-only changes too
    if rc:
        return 0
    fixed = 0
    for rel in [x.strip() for x in names.splitlines() if x.strip()]:
        fp = Path(wt) / rel
        try:
            old = subprocess.run(["git", "-C", str(wt), "show", f"{base}:{rel}"], capture_output=True,
                                 timeout=30).stdout
            new = fp.read_bytes()
        except (OSError, subprocess.SubprocessError):
            continue
        if not old or not new or b"\0" in old[:8000] or b"\0" in new[:8000]:
            continue
        lf = new.replace(b"\r\n", b"\n")
        crlf_before = old.count(b"\r\n") >= max(1, old.count(b"\n")) * 0.9
        want = lf.replace(b"\n", b"\r\n") if crlf_before else (lf if b"\r\n" not in old else new)
        if want != new:
            fp.write_bytes(want)
            fixed += 1
    return fixed


def is_repo(path: Path) -> bool:
    return (Path(path) / ".git").exists()


def env_hint(output: str) -> str:
    for rx, msg in ENV_HINTS:
        if m := rx.search(output or ""):
            return msg.format(*m.groups())
    return ""


def _py() -> str:
    import shutil
    return "python" if os.name == "nt" or shutil.which("python") else "python3"


def detect_test(path: Path) -> str:
    """A sensible default test command for a project folder ('' if none is obvious)."""
    p = Path(path)
    py = _py()
    try:
        pkg = json.loads((p / "package.json").read_text(encoding="utf-8")) if (p / "package.json").exists() else {}
    except (OSError, ValueError):
        pkg = {}
    if (pkg.get("scripts") or {}).get("test") and "no test specified" not in pkg["scripts"]["test"]:
        return "npm test --silent"
    if (p / "pytest.ini").exists() or (p / "conftest.py").exists() or (p / "tests").is_dir() and \
            any((p / "tests").glob("test_*.py")) or ((p / "pyproject.toml").exists() and "pytest" in
                                                    (p / "pyproject.toml").read_text(encoding="utf-8", errors="replace")):
        return f"{py} -m pytest -q"
    for sub in ("backend", "api", "server"):
        if (p / sub / "tests").is_dir():
            return f"cd {sub} && {py} -m pytest -q"
    return ""


SUMMARY_RE = re.compile(r"\b\d+ (passed|failed|error|errors|tests?)\b|\bok\b|\bFAIL", re.I)


class Changes:
    def __init__(self, org):
        self.org = org
        self.s = org.s
        self.root = org.home / "worktrees"

    # ---------- which repos ----------
    def repo_sources(self, dept_id: str) -> list:
        d = self.org.c.depts.get(dept_id)
        if not d or not ({"Edit", "Write"} & set(d.tools or [])):
            return []
        return [s for s in self.org.files.sources().values()
                if s.dept == dept_id and s.mode == "direct" and is_repo(self.org.files.path(s))]

    def untracked(self) -> list:
        """Live folders a file-editing team works on that aren't git repos: agents can't change them safely."""
        out = []
        for s in self.org.files.sources().values():
            d = self.org.c.depts.get(s.dept)
            if s.mode == "direct" and d and {"Edit", "Write"} & set(d.tools or []) and not is_repo(self.org.files.path(s)):
                out.append(s)
        return out

    def track(self, source: str) -> str:
        """The owner's click: make a live folder a git repo with one baseline commit, so agents can work on a branch.
        Nothing in the project changes: junk and secrets are excluded via .git/info/exclude, not a new .gitignore."""
        s = self.org.files.sources().get(source)
        if not s or s.mode != "direct":
            raise KeyError(f"no live folder {source!r}")
        path = self.org.files.path(s)
        if is_repo(path):
            return f"{path} is already a git repo"
        rc, out = git(path, "init", "-q", "-b", "main")
        if rc:
            rc, out = git(path, "init", "-q")
            if rc:
                raise ValueError(f"git init failed: {out[-300:]}")
        (path / ".git" / "info").mkdir(parents=True, exist_ok=True)
        (path / ".git" / "info" / "exclude").write_text("\n".join(TRACK_EXCLUDE) + "\n", encoding="utf-8")
        git(path, "add", "-A", timeout=300)
        rc, out = git(path, "commit", "-q", "--allow-empty", "-m", "agentsorg: baseline before agents' changes",
                      timeout=300)
        if rc:
            raise ValueError(f"the baseline commit failed: {out[-300:]}")
        self.org.log_decision("change", source, "change tracking on", str(path))
        msg = f"{path} now tracks changes; agents will work on branches you review and merge."
        if not self.test_command(source) and (t := detect_test(path)):
            self.set_test(source, t)
            msg += f" Test command set to `{t}` (change it on the Files page)."
        return msg

    # ---------- per-project branch ----------
    def worktrees_for(self, t: dict) -> list[dict]:
        """The project's branch of each repo this task's department works on (created on first use)."""
        pid = t.get("project_id")
        if not pid:
            return []
        out = []
        for src in self.repo_sources(t["dept"]):
            row = self.s.one("SELECT * FROM changes WHERE project_id=? AND source=? AND status='open'", (pid, src.name))
            if not row:
                row = self._create(pid, src)
            if row:
                out.append(row)
        return out

    def _create(self, pid: int, src) -> dict | None:
        repo = self.org.files.path(src).resolve()
        branch = f"agentsorg/p{pid}"
        wt = (self.root / f"{src.name}-p{pid}").resolve()
        rc, base = git(repo, "rev-parse", "HEAD")
        if rc:
            log.warning("no HEAD in %s: %s", repo, base)
            return None
        rc, cur = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
        self.root.mkdir(parents=True, exist_ok=True)
        if not wt.exists():
            rc, out = git(repo, "worktree", "add", "-B", branch, str(wt), base.strip())
            if rc:
                log.warning("worktree for %s failed: %s", repo, out)
                return None
        now = time.time()
        cid = self.s.x("INSERT INTO changes(created,updated,project_id,source,repo,branch,worktree,base,status,note) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?)", (now, now, pid, src.name, str(repo), branch, str(wt),
                                                       base.strip(), "open", cur.strip()))
        return self.s.one("SELECT * FROM changes WHERE id=?", (cid,))

    def touched(self, t: dict, since: float = 0) -> list[str]:
        """Files changed on the project's branches (for the board and the task's record)."""
        out = []
        for row in self.s.q("SELECT * FROM changes WHERE project_id=? AND status='open'", (t.get("project_id"),)) \
                if t.get("project_id") else []:
            rc, st = git(row["worktree"], "status", "--porcelain")
            if rc:
                continue
            for line in st.splitlines():
                parts = line.strip().split(maxsplit=1)      # "XY path" (the output's leading space may be gone)
                if len(parts) < 2:
                    continue
                f = parts[1].split(" -> ")[-1].strip().strip('"')
                if "__pycache__" in f or f.endswith((".pyc", ".pyo", ".log")):
                    continue
                p = Path(row["worktree"]) / f
                try:
                    if not since or not p.exists() or p.stat().st_mtime >= since - 1:
                        out.append(f"{row['source']}:{f}")
                except OSError:
                    pass
        return out[:40]

    def prompt_text(self, t: dict) -> str:
        lines = []
        for row in self.worktrees_for(t):
            src = self.org.files.sources().get(row["source"])
            test = self.test_command(row["source"])
            lines.append(f"YOUR WORKING COPY of {row['source']} (git branch {row['branch']}, for this project): "
                         f"{row['worktree']}\nMake every change to {row['source']} THERE, never in "
                         f"{row['repo']} (read-only). The owner reviews and merges the branch."
                         + (f"\nHOW TO RUN THE TESTS (from {row['worktree']}): {test}" if test else "")
                         + (f"\n{tl}" if (tl := self.test_fact(row["source"])) else ""))
        return "\n\n".join(lines)

    def test_fact(self, source: str) -> str:
        """The last real run of the owner's test command, so nobody argues from stale counts in notes."""
        last = self.org.s.get(f"test:{source}")
        if not last or last.get("ok") is None:
            return ""
        tail = [x.strip() for x in (last.get("output") or "").splitlines() if x.strip()]
        summary = next((x for x in reversed(tail) if SUMMARY_RE.search(x)), tail[-1] if tail else "")
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(last.get("at") or time.time()))
        where = last.get("where") or ""
        on = (f" on the project branch copy {Path(where).name}" if "worktrees" in where.replace("\\", "/")
              else " on the main checkout" if where else "")
        return (f"LAST TEST RUN ({when}{on}, {'passed' if last['ok'] else 'FAILED'}): {summary.strip('= ')[:160]}. "
                "This is measured; counts in notes or the knowledge base may be older.")

    # ---------- when the project ends ----------
    def finish(self, pid: int) -> list[dict]:
        out = []
        for row in self.s.q("SELECT * FROM changes WHERE project_id=? AND status='open'", (pid,)):
            wt = row["worktree"]
            if n := keep_line_endings(wt, row["base"]):
                log.info("change #%s: restored line endings in %s file(s)", row["id"], n)
            git(wt, "add", "-A")
            rc, _ = git(wt, "diff", "--cached", "--quiet")
            if rc:                                   # something is staged
                git(wt, "commit", "-q", "-m", f"agentsorg: project #{pid}")
            rc, names = git(row["repo"], "diff", "--name-only", f"{row['base']}..{row['branch']}")
            n = len([x for x in names.splitlines() if x.strip()]) if not rc else 0
            if n:
                self._set(row["id"], status="ready", files=n)
            else:
                self.discard(row["id"], why="no changes")
            out.append({**row, "files": n})
        return out

    def _set(self, cid: int, **f) -> None:
        f["updated"] = time.time()
        self.s.x(f"UPDATE changes SET {','.join(k + '=?' for k in f)} WHERE id=?", (*f.values(), cid))

    # ---------- the owner's review ----------
    def get(self, cid: int) -> dict:
        row = self.s.one("SELECT * FROM changes WHERE id=?", (cid,))
        if not row:
            raise KeyError("no such change")
        return row

    def diff(self, cid: int) -> dict:
        row = self.get(cid)
        rng = f"{row['base']}..{row['branch']}"
        _, stat = git(row["repo"], "diff", "--stat", rng)
        _, patch = git(row["repo"], "diff", rng, timeout=120)
        return {**row, "stat": stat, "diff": patch[:DIFF_CHARS], "truncated": len(patch) > DIFF_CHARS}

    def merge(self, cid: int) -> str:
        row = self.get(cid)
        if row["status"] != "ready":
            raise ValueError(f"this change is {row['status']}")
        repo = row["repo"]
        rc, dirty = git(repo, "status", "--porcelain", "--untracked-files=no")
        how = (f"To merge by hand:\n  cd {repo}\n  git merge --no-ff {row['branch']}")
        mine = {ln.split(maxsplit=1)[-1].split(" -> ")[-1].strip('"') for ln in dirty.splitlines() if ln.strip()}
        rc, theirs = git(repo, "diff", "--name-only", f"HEAD...{row['branch']}")
        overlap = sorted(mine & {x.strip() for x in theirs.splitlines() if x.strip()}) if not rc else sorted(mine)
        if overlap:          # unsaved work in a file the branch also changes: never risk it
            raise ValueError(f"{repo} has uncommitted changes in {', '.join(overlap[:5])}, which this branch also "
                             f"changes, so it wasn't merged. Commit or stash them first. {how}")
        # other uncommitted files (often data the running app writes) are left alone: git merges around them
        rc, cur = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
        if row["note"] and cur.strip() != row["note"]:
            raise ValueError(f"{repo} is on branch {cur.strip()}, not {row['note']} as when the work started. {how}")
        rc, out = git(repo, "merge", "--no-ff", "-m", f"Merge {row['branch']} (agentsorg project #{row['project_id']})",
                      row["branch"], timeout=120)
        if rc:
            git(repo, "merge", "--abort")
            raise ValueError(f"The merge had conflicts and was undone. {how}\n{out[-600:]}")
        self._remove_worktree(row)
        self._set(cid, status="merged")
        self.org.log_decision("change", f"{row['source']} {row['branch']}", "merged", f"{row['files']} file(s)",
                              ref=f"project:{row['project_id']}")
        return f"merged {row['branch']} into {cur.strip()} in {repo}"

    def discard(self, cid: int, why: str = "discarded by you") -> None:
        row = self.get(cid)
        self._remove_worktree(row)
        git(row["repo"], "branch", "-D", row["branch"])
        self._set(cid, status="discarded", note=why[:200])

    def _remove_worktree(self, row: dict) -> None:
        if Path(row["worktree"]).exists():
            git(row["repo"], "worktree", "remove", "--force", row["worktree"])
        git(row["repo"], "worktree", "prune")

    def ready(self) -> list[dict]:
        return self.s.q("SELECT * FROM changes WHERE status='ready' ORDER BY id")

    # ---------- tests ----------
    def test_command(self, source: str) -> str:
        return str(((self.org.c.raw.get("sources") or {}).get(source) or {}).get("test") or "")

    def set_test(self, source: str, command: str) -> None:
        srcs = self.org.c.raw.get("sources") or {}
        if source not in srcs:
            raise KeyError(f"no linked folder {source!r}")
        with self.org.c.lock:
            if command.strip():
                srcs[source]["test"] = command.strip()[:300]
            else:
                srcs[source].pop("test", None)
            self.org.c.save()

    def run_tests(self, source: str, cwd: str | Path | None = None) -> dict:
        """Run the owner's test command once (no model). The command is the owner's own, from company.yaml."""
        cmd = self.test_command(source)
        if not cmd:
            return {"ok": None, "output": "", "hint": "No test command set for this folder."}
        src = self.org.files.sources().get(source)
        where = Path(cwd) if cwd else self.org.files.path(src)
        shell = ["bash", "-lc", cmd] if os.name != "nt" else cmd      # Windows: cmd.exe parses the string itself
        from .runtime import clean_path
        env = {**os.environ, "PATH": clean_path(os.environ.get("PATH", ""))}
        try:
            p = subprocess.run(shell, cwd=str(where), capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=TEST_TIMEOUT, shell=os.name == "nt", env=env)
            out, ok = (p.stdout + p.stderr)[-6000:], p.returncode == 0
        except subprocess.TimeoutExpired:
            out, ok = f"stopped after {TEST_TIMEOUT}s", False
        except OSError as e:
            out, ok = str(e), False
        res = {"ok": ok, "output": out, "hint": "" if ok else env_hint(out), "at": time.time(), "where": str(where)}
        self.org.s.set(f"test:{source}", res)
        return res
