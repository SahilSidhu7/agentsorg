"""Deterministic workspace map for tool-using departments (0 tokens to build).

Agents with file tools otherwise spend their first turns on Glob/LS/Read just to orient,
and every extra turn re-sends the whole conversation. A small map up front removes that.
"""
from __future__ import annotations

import os
from pathlib import Path

SKIP_DIRS = {"node_modules", ".git", ".venv", "venv", "__pycache__", "dist", "build", ".next", "target",
             ".pytest_cache", ".mypy_cache", ".ruff_cache", ".idea", ".vscode", "coverage", ".cache"}
SKIP_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".svg", ".woff", ".woff2", ".ttf", ".zip", ".gz",
            ".whl", ".lock", ".pyc", ".so", ".dll", ".exe", ".pdf", ".db", ".sqlite"}
MAX_FILES = 120
MAX_CHARS = 2400
SUMMARY_CHARS = 70

_cache: dict[str, tuple[str, str]] = {}   # path -> (fingerprint, map)


def _summary(p: Path) -> str:
    """First meaningful line: a docstring, comment, heading or def. Cheap, no model call."""
    try:
        with p.open(encoding="utf-8", errors="replace") as f:
            for _, line in zip(range(25), f):
                s = line.strip().strip('"\'#/*-! ').strip()
                if len(s) >= 12 and not s.startswith(("import ", "from ", "use ", "package ", "<!doctype", "<?xml")):
                    return s[:SUMMARY_CHARS]
    except OSError:
        pass
    return ""


COUNT_ONLY_DIRS = {"outputs"}   # past deliverables: a count is enough, agents open one if needed


def _files(root: Path, notes: list[str], max_files: int = MAX_FILES) -> list[Path]:
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root).as_posix()
        prefix = "" if rel_dir == "." else rel_dir + "/"
        for d in [d for d in dirnames if d in COUNT_ONLY_DIRS]:
            n = sum(len(fs) for _, _, fs in os.walk(Path(dirpath) / d))
            if n:
                notes.append(f"{prefix}{d}/: {n} past deliverables (not listed)")
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and d not in COUNT_ONLY_DIRS
                             and not d.startswith("."))
        media: dict[str, int] = {}
        for f in sorted(filenames):
            p = Path(dirpath) / f
            if f.startswith("."):
                continue
            ext = p.suffix.lower()
            if ext in SKIP_EXT:
                media[ext.lstrip(".")] = media.get(ext.lstrip("."), 0) + 1
                continue
            if len(out) < max_files:
                out.append(p)
        if media:
            kinds = ", ".join(f"{n} {k}" for k, n in sorted(media.items(), key=lambda x: -x[1]))
            notes.append(f"{prefix or './'}: media/binary files: {kinds}")
    return out


def workspace_map(root: Path, summaries: bool = True, max_chars: int = MAX_CHARS, max_files: int = MAX_FILES) -> str:
    notes: list[str] = []
    files = _files(root, notes, max_files)
    if not files and not notes:
        return "WORKSPACE: empty. Create files as needed."
    fp = "|".join(f"{p}:{p.stat().st_mtime_ns}:{p.stat().st_size}" for p in files) + "|".join(notes)
    key = f"{root}|{summaries}|{max_chars}|{max_files}"
    hit = _cache.get(key)
    if hit and hit[0] == fp:
        return hit[1]
    head = "path size: first line" if summaries else "path size"
    lines = [f"WORKSPACE MAP ({len(files)} files; {head}). Use it instead of listing/globbing; Read only files you need."]
    for p in files:
        rel = p.relative_to(root).as_posix()
        size = p.stat().st_size
        s = _summary(p) if summaries and size < 400_000 else ""
        lines.append(f"{rel} {size // 1024}k{': ' + s if s else ''}" if size >= 1024 else f"{rel} {size}b{': ' + s if s else ''}")
    lines += notes
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars].rsplit("\n", 1)[0] + "\n…(more files; Glob only if you need them)"
    _cache[key] = (fp, text)
    return text
