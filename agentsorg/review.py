"""Acceptance review: before the owner sees a result, one strong call checks the work against the command.

It lists each requirement in the owner's command as met / partial / missing with evidence (task results, files,
test output), and names the gaps as concrete fix tasks. The project gets at most one automatic fix round; after that
the result reports honestly what's still missing.
"""
from __future__ import annotations

import json
import logging
import re

log = logging.getLogger("agentsorg")
MAX_FIXES = 3
REVIEW_TURNS = 16      # enough to open the files that decide a status, not to re-audit everything
REVIEW_PROMPT = """You are the company's acceptance reviewer. Check the delivered work against what the OWNER ASKED.
The command is decided; judge only whether the work delivers it, never whether it should be done.
1. Break what the owner asked into its concrete requirements (what a customer or the owner must be able to do or have).
   If DECIDED IN THE MEETING is given, every safety condition and risk control of the chosen option is a requirement
   too: add each as its own row starting "Decided:" (for example "Decided: staging is isolated from live").
   VERIFY IN THE CODE: you can read the project's working copy (paths under CHANGES). Open the files that decide a
   status (deploy scripts, compose/config files, the code behind each claim) instead of trusting task summaries.
   A claim the files contradict is missing, and the evidence names the file and line.
2. For each, give status met | partial | missing and one line of evidence (under 300 characters: the file or test
   that decides it, and what is missing) from TASKS, FILES, TESTS or CHANGES. When a later task changed the
   picture (a fix round), judge from the latest work.
   A document describing something is not the thing itself: code that was asked for must exist and be tested;
   automation that was asked for must exist as something that runs. Failed tasks count as missing.
3. For every partial or missing requirement a team here can fix now, give one fix task (max 3): the department id,
   and a self-contained instruction that builds on the files already written.
Output ONLY JSON:
{"requirements":[{"need":"...","status":"met|partial|missing","evidence":"..."}],
 "gaps":[{"need":"...","dept":"<id>","task":"<self-contained instruction>"}]}"""


def clip(text: str, n: int) -> str:
    """Shorten at a sentence or word boundary, never mid-word."""
    text = " ".join(text.split())
    if len(text) <= n:
        return text
    cut = text[:n]
    end = max(cut.rfind(". "), cut.rfind("; "))
    return (cut[:end + 1] if end > n // 2 else cut.rsplit(" ", 1)[0]) + " …"


def material(org, pid: int, tests: list[dict]) -> str:
    p = org.s.one("SELECT command, plan FROM projects WHERE id=?", (pid,))
    rows = org.s.q("SELECT id, dept, status, kind, title, done_line, summary, error, files FROM tasks "
                   "WHERE project_id=? AND dept!='ceo' ORDER BY id", (pid,))
    tasks = "\n".join(f"- #{t['id']} {t['dept']} [{t['status']}{'/' + t['kind'] if t['kind'] else ''}] {t['title'][:100]}"
                      f" :: {(t['summary'] or t['done_line'] or '').replace(chr(10), ' · ')[:300]}"
                      + (f" :: ERROR {t['error'][:160]}" if t["error"] else "") for t in rows)
    files = sorted({f for t in rows for f in json.loads(t["files"] or "[]")})
    ch = org.s.q("SELECT source, branch, status, worktree FROM changes WHERE project_id=?", (pid,))
    return "\n\n".join([x for x in [
        f"OWNER ASKED:\n{p['command']}",
        f"TASKS:\n{tasks}",
        "FILES WRITTEN:\n" + ("\n".join(f"- {f}" for f in files[:80]) or "none"),
        "CHANGES:\n" + ("\n".join(f"- {c['source']} on {c['branch']} ({c['status']}), working copy: {c['worktree']}"
                                   for c in ch) or "none"),
        "TESTS:\n" + ("\n".join(f"- {t['source']}: {'passed' if t['ok'] else 'FAILED' if t['ok'] is False else 'not set'}"
                               f"{' :: ' + t['output'][-400:] if t.get('ok') is False else ''}" for t in tests) or "none set"),
        f"DEPARTMENTS:\n" + "\n".join(f"- {d.id}: {d.name} ({', '.join(d.tools) or 'text only'})"
                                      for d in org.c.depts.values()),
    ] if x])


def worktrees(org, pid: int) -> list[str]:
    return [r["worktree"] for r in org.s.q("SELECT worktree FROM changes WHERE project_id=? AND status IN "
                                           "('open','ready')", (pid,)) if r.get("worktree")]


def review(org, pid: int, tests: list[dict]) -> dict | None:
    if not org.exe or not org.budget.check(auto=False)[0]:
        return None
    from pathlib import Path
    from .agents import READ_TOOLS
    wts = [w for w in worktrees(org, pid) if Path(w).is_dir()]
    res = org.call(material(org, pid, tests), model=org.c.models[2], system=REVIEW_PROMPT, dept="_review",
                   task_id=None, auto=False, tools=READ_TOOLS if wts else None,
                   max_turns=REVIEW_TURNS if wts else 1, cwd=Path(wts[0]) if wts else None, add_dirs=wts or None)
    if not res.ok and "max_turns" in (res.error or "") and wts:     # out of reading room: judge from the material
        res = org.call(material(org, pid, tests), model=org.c.models[2], system=REVIEW_PROMPT, dept="_review",
                       task_id=None, auto=False)
    if not res.ok:
        log.warning("acceptance review failed: %s", res.error[:200])
        return None
    m = re.search(r"\{.*\}", res.text or "", re.S)
    try:
        data = json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        data = {}
    reqs = [{"need": str(r.get("need", ""))[:200], "status": str(r.get("status", "")).lower()[:10],
             "evidence": clip(str(r.get("evidence", "")), 600)}
            for r in (data.get("requirements") or []) if isinstance(r, dict) and r.get("need")][:20]
    gaps = [{"need": str(g.get("need", ""))[:200], "dept": str(g.get("dept", "")), "task": str(g.get("task", ""))[:3000]}
            for g in (data.get("gaps") or []) if isinstance(g, dict) and g.get("task")][:MAX_FIXES]
    return {"requirements": reqs, "gaps": gaps, "tokens": res.tokens}


def table(rv: dict | None) -> str:
    if not rv or not rv.get("requirements"):
        return "(no acceptance review: the budget or the reviewer wasn't available)"
    mark = {"met": "✓ met", "partial": "◐ partial", "missing": "✗ missing"}
    return "| Requirement | Status | Evidence |\n|---|---|---|\n" + "\n".join(
        f"| {r['need']} | {mark.get(r['status'], r['status'])} | {r['evidence']} |" for r in rv["requirements"])
