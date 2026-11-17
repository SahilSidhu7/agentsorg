"""0.6: findings from the 2026-11-06 QA run (docs/qa-run-2026-11-06.md), each with a regression test."""
import json
import subprocess
import time
from pathlib import Path

import pytest

from agentsorg import agents as agents_mod
from agentsorg.agents import Org
from agentsorg.config import init_home
from agentsorg.runtime import RunResult


class Q:
    def utilization(self, force=False):
        return {"five_hour": {"utilization": 10, "resets_at": ""}}

    def worst(self):
        return 10


def ok(model, text):
    return RunResult(text=text, ok=True, model=model,
                     usage=[{"model": model, "input": 10, "output": 5, "cache_read": 0, "cache_create": 0, "cost_usd": 0}])


@pytest.fixture
def org(tmp_path, monkeypatch):
    init_home(tmp_path / "co")
    o = Org(tmp_path / "co")
    o.exe = "fake"
    o.budget.quota = Q()
    o.calls, o.plan, o.reviews = [], {"tasks": [{"dept": "marketing", "task": "write the copy"}]}, []
    o.work = lambda prompt, k: "the work\nDONE: did it"

    def fake(exe, prompt, *, model, system, **k):
        o.calls.append({"model": model, "prompt": prompt, "system": system, **k})
        if "COMMAND:" in prompt:
            return ok(model, json.dumps(o.plan))
        if "acceptance reviewer" in system:
            rv = o.reviews.pop(0) if o.reviews else {"requirements": [{"need": "it", "status": "met", "evidence": "x"}],
                                                     "gaps": []}
            return ok(model, json.dumps(rv))
        if "project result for the company" in system:
            return ok(model, "RESULT\n" + prompt[:4000])
        return ok(model, o.work(prompt, k))

    monkeypatch.setattr(agents_mod, "run_claude", fake)
    yield o
    o.close()


def until(fn, timeout=12):
    end = time.time() + timeout
    while time.time() < end:
        if v := fn():
            return v
        time.sleep(0.05)
    return fn()


def project_done(org, pid):
    return until(lambda: (x := org.projects.get(pid)) and x["status"] in ("review", "failed") and x)


# ---------- F2/F10: the owner's newest command wins ----------
def test_today_in_every_prompt(org):
    org.wait([org.submit("marketing", "write a line")])
    assert time.strftime("TODAY: %Y-%m-%d") in org.calls[-1]["prompt"]
    assert time.strftime("TODAY: %Y-%m-%d") in org.company_facts()


def test_same_team_builds_are_chained_and_share_files(org):
    org.plan = {"tasks": [{"dept": "engineering", "task": "write the architecture"},
                          {"dept": "engineering", "task": "build the subdomain tool", "after": 0},
                          {"dept": "engineering", "task": "build the signup flow", "after": 0}]}

    def work(prompt, k):
        if "subdomain tool" in prompt:
            (k["cwd"] / "subdomain.py").write_text("x = 1")
        return "built\nDONE: built it"
    org.work = work
    r = org.ceo("Write the architecture and build the tool and the flow")
    t = [org.s.task(i) for i in r["tasks"]]
    assert t[1]["depends_on"] == t[0]["id"] and t[2]["depends_on"] == t[1]["id"]
    project_done(org, r["project"])
    signup = [c for c in org.calls if "build the signup flow" in c["prompt"] and c.get("tools")][0]["prompt"]
    assert "Files written so far" in signup and "subdomain.py" in signup


def test_parallel_safe_tasks_stay_parallel(org):
    org.plan = {"tasks": [{"dept": "engineering", "task": "a", "parallel_safe": True},
                          {"dept": "engineering", "task": "b", "parallel_safe": True}]}
    r = org.ceo("Do a and b across modules")
    assert all(org.s.task(i)["depends_on"] is None for i in r["tasks"])


# ---------- F13 / F6 ----------
def test_question_tasks_are_lean_and_read_only_tasks_get_room(org):
    tid = org.submit("engineering", "A colleague asks: which port?", kind="question")
    org.wait([tid])
    q = [c for c in org.calls if "which port" in c["prompt"]][0]
    assert not q.get("tools") and "READABLE" not in q["prompt"]
    d = org.c.depts["research"]
    assert org._turns(d, ["Read", "Glob", "Grep"]) >= 12


# ---------- F12: project follow-ups wait for the owner ----------
def git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *a],
                          capture_output=True, text=True, check=True).stdout


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "product"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    (r / "app.py").write_text("print('v1')\n")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "init")
    return r


def test_project_changes_land_on_a_branch_and_merge_on_click(org, repo):
    from agentsorg import inbox
    (repo / "data.json").write_text("{}\n")                          # data the running app rewrites
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "data")
    org.files.add("product", "engineering", f"this-pc {repo}")
    org.plan = {"tasks": [{"dept": "engineering", "task": "add the signup page"}]}

    def work(prompt, k):
        wt = next(Path(d) for d in k["add_dirs"] if "worktrees" in d)
        (wt / "signup.py").write_text("def signup(): pass\n")
        (wt / "app.py").write_text("print('v2')\n")
        return "added signup\nDONE: signup page added"
    org.work = work
    r = org.ceo("@engineering add the signup page") if False else org.ceo("Add the signup page and its test")
    p = project_done(org, r["project"])
    task_call = [c for c in org.calls if "add the signup page" in c["prompt"] and c.get("tools")][0]
    assert "YOUR WORKING COPY of product" in task_call["prompt"] and str(repo.resolve()) in task_call["read_only"]
    assert not (repo / "signup.py").exists()                        # the real tree is untouched
    ch = p["changes"][0]
    assert ch["status"] == "ready" and ch["files"] == 2
    assert [i for i in inbox.items(org) if i["kind"] == "result" and "code changes (2 file(s))" in i["detail"]]
    d = org.changes.diff(ch["id"])
    assert "signup.py" in d["stat"] and "def signup" in d["diff"]
    (repo / "app.py").write_text("print('dirty')\n")                 # the owner's own unsaved work, same file
    with pytest.raises(ValueError, match="uncommitted changes in app.py"):
        org.changes.merge(ch["id"])
    git(repo, "checkout", "--", "app.py")
    (repo / "data.json").write_text('{"live": 1}\n')               # unrelated: merged around, kept as is
    assert "merged" in org.changes.merge(ch["id"])
    assert (repo / "data.json").read_text() == '{"live": 1}\n'
    assert (repo / "signup.py").exists() and "agentsorg project" in git(repo, "log", "-1", "--pretty=%s")


def test_discard_removes_branch(org, repo):
    org.files.add("product", "engineering", f"this-pc {repo}")
    org.plan = {"tasks": [{"dept": "engineering", "task": "try something"}]}

    def work(prompt, k):
        wt = next(Path(d) for d in k["add_dirs"] if "worktrees" in d)
        (wt / "x.py").write_text("x\n")
        return "ok\nDONE: ok"
    org.work = work
    r = org.ceo("Try something risky and report back")
    ch = project_done(org, r["project"])["changes"][0]
    org.changes.discard(ch["id"])
    assert "agentsorg/p" not in git(repo, "branch")


def test_test_command_check_and_hint(org, repo):
    org.files.add("product", "engineering", f"this-pc {repo}")
    org.changes.set_test("product", "python -c \"import sqlalchemy_not_here\"")
    r = org.changes.run_tests("product")
    assert r["ok"] is False and "isn't installed where they run" in r["hint"]
    from agentsorg.changes import detect_test
    (repo / "tests").mkdir()
    (repo / "tests" / "test_a.py").write_text("def test_a(): pass\n")
    assert detect_test(repo) == "python -m pytest -q"


# ---------- F14/F11: acceptance review, one fix round, an honest result ----------
def test_review_fixes_gaps_once_then_reports(org):
    org.plan = {"tasks": [{"dept": "marketing", "task": "write the copy"},
                          {"dept": "sales", "task": "write the delivery spec"}]}
    org.work = lambda p, k: (RunResult(text="", ok=False, model="x", error="something broke") if "delivery spec" in p
                             and "FIX" not in p else "work\nDONE: ok")
    calls = org.work

    def work(prompt, k):
        r = calls(prompt, k)
        if isinstance(r, RunResult):
            raise RuntimeError("broken")
        return r
    org.work = lambda p, k: "work\nDONE: ok" if "delivery spec" not in p or "FIX" in p else (_ for _ in ()).throw(RuntimeError())
    org.reviews = [{"requirements": [{"need": "delivery spec", "status": "missing", "evidence": "task failed"}],
                    "gaps": [{"need": "delivery spec", "dept": "sales", "task": "FIX: write the delivery spec"}]},
                   {"requirements": [{"need": "delivery spec", "status": "met", "evidence": "fix task #4"}], "gaps": [
                       {"need": "more", "dept": "sales", "task": "FIX again"}]}]
    r = org.ceo("Write the copy and the delivery spec")
    p = until(lambda: (x := org.projects.get(r["project"])) and x["status"] == "review" and x, timeout=20)
    assert p["fix_round"] == 1 and any("FIX: write the delivery spec" in t["title"] or "FIX" in t["title"]
                                       for t in p["tasks"])
    assert not any("FIX again" in t["title"] for t in p["tasks"])          # only one automatic round
    assert p["review"]["requirements"][0]["status"] == "met"
    assert "ACCEPTANCE" in p["result"] and "FAILED" in p["result"]          # the combiner saw failures and the table


def test_fix_tasks_for_one_team_run_one_after_another(org):
    org.plan = {"tasks": [{"dept": "engineering", "task": "build it"}]}
    org.reviews = [{"requirements": [{"need": "x", "status": "partial", "evidence": "half"}],
                    "gaps": [{"need": "a", "dept": "engineering", "task": "FIX one"},
                             {"need": "b", "dept": "engineering", "task": "FIX two"},
                             {"need": "c", "dept": "sales", "task": "FIX three"}]},
                   {"requirements": [{"need": "x", "status": "met", "evidence": "done"}], "gaps": []}]
    r = org.ceo("Build it")
    until(lambda: org.projects.get(r["project"])["status"] == "review", timeout=20)
    fx = {t["body"].split("\n")[0][:9]: t for t in org.s.q("SELECT * FROM tasks WHERE body LIKE 'FIX %'")}
    assert fx["FIX two"]["depends_on"] == fx["FIX one"]["id"] and fx["FIX three"]["depends_on"] is None


def test_review_evidence_is_never_cut_mid_word():
    from agentsorg.review import clip
    long = "deploy.sh line 45 runs the guard. " * 30
    out = clip(long, 600)
    assert len(out) <= 602 and out.endswith("…") and "guard. …" in out
    assert clip("short one", 600) == "short one"
    assert clip("word " * 200, 50).endswith("word …")


# ---------- F4/F1 ----------
def test_old_company_reads_its_project_once(org, tmp_path):
    p = tmp_path / "proj"
    p.mkdir()
    (p / "README.md").write_text("# hi")
    org.files.add("proj", "engineering", f"this-pc {p}")
    jobs = org.intake.jobs()
    assert [n for n, _ in jobs] == ["first project read"] and org.intake.jobs() == []


def test_untracked_folder_can_be_tracked_without_touching_files(org, tmp_path):
    from agentsorg import inbox
    p = tmp_path / "plain"
    p.mkdir()
    (p / "app.py").write_text("x\n")
    (p / ".env").write_text("SECRET=1\n")
    org.files.add("plain", "engineering", f"this-pc {p}")
    org.changes.track("plain")
    tracked = git(p, "ls-files")
    assert "app.py" in tracked and ".env" not in tracked and not (p / ".gitignore").exists()


def test_planner_is_told_product_code_goes_in_the_tracked_repo(org, repo):
    org.files.add("product", "engineering", f"this-pc {repo}")
    assert "CHANGE-TRACKED REPO: product code for it goes INTO this repo" in org.company_facts()
    org.wait([org.submit("research", "summary")])
    assert "Product code belongs in the product's repo" in (org.home / "prompts" / "ceo.md").read_text()


def test_file_lists_skip_build_junk(org, tmp_path):
    d = tmp_path / "w"
    (d / "pkg" / "__pycache__").mkdir(parents=True)
    (d / "pkg" / "a.py").write_text("x")
    (d / "pkg" / "__pycache__" / "a.cpython-312.pyc").write_text("x")
    names = [p.name for p in Org._written_files(d, 0)]
    assert names == ["a.py"]


def test_tracking_sets_a_test_command(org, tmp_path):
    p = tmp_path / "svc"
    (p / "tests").mkdir(parents=True)
    (p / "tests" / "test_x.py").write_text("def test_x(): pass\n")
    org.files.add("svc", "engineering", f"this-pc {p}")
    assert "Test command set to `python -m pytest -q`" in org.changes.track("svc")
    assert org.changes.test_command("svc") == "python -m pytest -q"


def test_env_hint_for_missing_test_runner():
    from agentsorg.changes import env_hint
    assert "own environment" in env_hint("/venv/bin/python: No module named pytest")
    assert "docker" in env_hint("permission denied while trying to connect to the docker API at unix:///var/run/docker.sock")


def test_agent_path_hides_our_own_venv(monkeypatch):
    import os, sys
    from agentsorg import runtime
    fake_bin = os.path.join("/opt/agentsorg-venv", "Scripts" if os.name == "nt" else "bin")
    monkeypatch.setattr(sys, "prefix", "/opt/agentsorg-venv")
    monkeypatch.setattr(sys, "base_prefix", "/usr")
    p = runtime.clean_path(os.pathsep.join([fake_bin, "/usr/bin"]))
    assert fake_bin not in p.split(os.pathsep) and "/usr/bin" in p


def test_branch_file_names_are_parsed_whole(org, repo):
    org.files.add("product", "engineering", f"this-pc {repo}")
    pid = org.projects.create("x", status="running")
    t = {"project_id": pid, "dept": "engineering"}
    wt = Path(org.changes.worktrees_for(t)[0]["worktree"])
    (wt / "app.py").write_text("changed\n")
    (wt / "backend").mkdir()
    (wt / "backend" / "new.py").write_text("x\n")
    got = org.changes.touched(t)
    assert "product:app.py" in got and any(g.startswith("product:backend") for g in got)


# ---------- run 4 (docs/qa-run-2026-11-06.md, R16-R22) ----------
def test_last_test_run_is_a_fact(org, repo):
    org.files.add("product", "engineering", f"this-pc {repo}")
    org.s.set("test:product", {"ok": False, "at": time.time(), "output": "....F\n=== 1 failed, 39 passed in 2s ==="})
    facts = org.company_facts()
    assert "LAST TEST RUN" in facts and "1 failed, 39 passed" in facts and "FAILED" in facts
    pid = org.projects.create("x", status="running")
    assert "LAST TEST RUN" in org.changes.prompt_text({"project_id": pid, "dept": "engineering"})


def test_owner_question_carries_what_was_asked(org):
    pid = org.projects.create("x", status="running")
    ans = org.submit("sales", "approval?", project_id=pid, kind="question")
    org.wait([ans])
    org.s.update_task(ans, result="Two ways: A) customer approves a private draft first; B) publish at once.\nDONE: ok")
    org.s.x("INSERT INTO messages(created,project_id,from_task,from_dept,to_dept,question,answer_task,status) "
            "VALUES(?,?,?,?,?,?,?,?)", (time.time(), pid, 1, "engineering", "sales",
                                        "Should a site draft need customer approval?", ans, "asked"))
    cont = org.submit("engineering", "continue", project_id=pid, depends_on=ans)
    org.projects.suggest(pid, "Build the approval flow once the founder picks A.", "engineering", "lead", cont)
    q = [s for s in org.projects.suggestions(pid) if s["kind"] == "question"][0]["text"]
    assert "Should a site draft need customer approval?" in q and "A) customer approves" in q and "DONE" not in q


def test_final_step_waits_for_parallel_siblings(org):
    import threading
    gate = threading.Event()
    org.c.budget["concurrency"] = 4
    org.plan = {"tasks": [{"dept": "engineering", "task": "build the base"},
                          {"dept": "engineering", "task": "build slow part", "after": 0, "parallel_safe": True},
                          {"dept": "engineering", "task": "write the final report", "after": 0}]}

    def work(prompt, k):
        if "slow part" in prompt and k.get("tools"):
            gate.wait(10)
        return "ok\nDONE: ok"
    org.work = work
    r = org.ceo("Build the base, the slow and fast parts, then report")
    ids = r["tasks"]
    until(lambda: org.s.task(ids[0])["status"] == "done")
    time.sleep(0.3)
    assert org.s.task(ids[2])["status"] == "waiting"            # its own step is done, a sibling isn't
    gate.set()
    until(lambda: org.s.task(ids[2])["status"] == "done")
    assert org.s.task(ids[2])["status"] == "done"


def test_line_endings_are_kept(org, repo):
    from agentsorg.changes import keep_line_endings
    git(repo, "config", "core.autocrlf", "false")                  # store bytes as written (as on Linux)
    (repo / "win.py").write_bytes(b"a = 1\r\nb = 2\r\n")
    (repo / "unix.py").write_bytes(b"print('v1')\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "crlf")
    base = git(repo, "rev-parse", "HEAD").strip()
    (repo / "win.py").write_bytes(b"a = 1\nb = 3\n")            # an editor rewrote it as LF
    (repo / "unix.py").write_bytes(b"print('v2')\r\n")           # and an LF file as CRLF
    assert keep_line_endings(repo, base) == 2
    assert (repo / "win.py").read_bytes() == b"a = 1\r\nb = 3\r\n"
    assert (repo / "unix.py").read_bytes() == b"print('v2')\n"
    assert git(repo, "-c", "core.autocrlf=false", "diff", "--numstat", "--", "win.py").split()[:2] == ["1", "1"]


def test_timeout_retries_once_with_more_time(org, monkeypatch):
    seen = []
    real = agents_mod.run_claude

    def flaky(exe, prompt, **k):
        if k.get("tools") and "big migration" in prompt:
            seen.append((k["timeout"], prompt))
            if len(seen) == 1:
                return RunResult(text="", ok=False, model=k["model"], error="timeout")
        return real(exe, prompt, **k)
    monkeypatch.setattr(agents_mod, "run_claude", flaky)
    org.wait([tid := org.submit("engineering", "do the big migration")])
    assert org.s.task(tid)["status"] == "done"
    assert seen[1][0] == 2 * seen[0][0] and "stopped by the time limit" in seen[1][1]
