import json
from pathlib import Path

import pytest

from agentsorg import agents as agents_mod
from agentsorg.agents import Org
from agentsorg.config import init_home
from agentsorg.runtime import RunResult


def ok(model, text):
    return RunResult(text=text, ok=True, model=model,
                     usage=[{"model": model, "input": 1, "output": 1, "cache_read": 0, "cache_create": 0, "cost_usd": 0}])


@pytest.fixture
def org(tmp_path, monkeypatch):
    init_home(tmp_path / "co")
    o = Org(tmp_path / "co")
    o.exe = "fake"
    o.calls, o.ceo_reply = [], "[]"

    def fake_run(exe, prompt, *, model, system, tools=None, max_turns=1, cwd=None, timeout=900, effort="", add_dirs=None, read_only=None, env=None, **_):
        o.calls.append({"prompt": prompt, "system": system, "tools": tools, "cwd": cwd, "add_dirs": add_dirs,
                        "read_only": read_only})
        if "COMMAND:" in prompt:
            return ok(model, o.ceo_reply)
        return ok(model, "work\nDONE: did it")

    monkeypatch.setattr(agents_mod, "run_claude", fake_run)
    yield o
    o.close()           # no worker may leak into the next test's fake Claude


def project(tmp_path, name, files):
    p = tmp_path / name
    for rel, text in files.items():
        (p / rel).parent.mkdir(parents=True, exist_ok=True)
        (p / rel).write_text(text)
    return p


def test_ceo_sees_real_state_and_runs_in_company_folder(org, tmp_path):
    local = project(tmp_path, "marchlight", {"a.py": "x"})
    org.files.add("marchlight-local", "engineering", f"this-pc {local}")
    org.files.add("sideh", "engineering", "zoro C:/sideH-not-on-this-machine")
    assert "task(s) in progress" in org.ceo("status?")["answer"] and not org.calls   # answered for free
    org.ceo("what should we do about the linked folders?")
    call = org.calls[0]
    assert call["cwd"] == org.home                                          # never a /tmp path the CEO could leak
    assert "COMPANY OPERATIONS" in call["system"]
    p = call["prompt"]
    assert "marchlight-local -> engineering · LIVE folder" in p and "read-only" in p and "always current" in p
    assert "sideh -> engineering" in p and "on another computer" in p
    assert "engineering (Engineering): edits files in its folder" in p


def test_ceo_actions_link_live_folders_and_answer(org, tmp_path):
    local = project(tmp_path, "marchlight", {"src/app.py": "print(1)"})
    harvey = project(tmp_path, "harvey", {"leads.csv": "a,b", "templates/first.md": "hi"})
    org.files.add("marchlight-local", "engineering", f"this-pc {local}")         # same machine: live by default
    org.files.add("sideh", "engineering", "zoro C:/elsewhere")                   # other computer: synced
    assert org.files.get("marchlight-local").mode == "direct" and org.files.get("sideh").mode == "sync"
    org.ceo_reply = json.dumps({
        "answer": "Linked what's here; run sideh's push on zoro.",
        "actions": [{"action": "sync_local", "source": "marchlight-local"},
                    {"action": "link_local", "name": "harvey", "dept": "sales", "path": str(harvey)},
                    {"action": "sync_local", "source": "sideh"},
                    {"action": "link_local", "name": "everything", "dept": "sales", "path": str(Path.home())},
                    {"action": "link_local", "name": "self", "dept": "sales", "path": str(org.home)}],
        "tasks": [{"dept": "research", "task": "Summarize the lead files and templates"}]})
    r = org.ceo("bring in all the files")
    a = r["actions"]
    assert "live folder" in a[0] and a[1].startswith("✓ linked") and "no copies" in a[1]
    assert a[2].startswith("✗ sideh is on another computer")
    assert a[3].startswith("✗") and "too broad" in a[3] and a[4].startswith("✗")
    lk = org.home / "sales" / "sources" / "harvey"
    assert not lk.exists() or org.files._is_link(lk)                            # a link at most, nothing copied
    assert org.files.path(org.files.get("harvey")) == harvey.resolve()
    t = org.wait(r["tasks"], timeout=10)[0]
    call = [c for c in org.calls if c["tools"] and "Summarize the lead files" in c["prompt"]][-1]
    assert t["status"] == "done" and call["tools"] == ["Read", "Glob", "Grep"]
    assert str(harvey.resolve()) in call["add_dirs"] and "leads.csv" in call["prompt"]


def test_live_folders_are_read_only_unless_writable(org, tmp_path):
    ro = project(tmp_path, "harvey", {"cfg.yaml": "x"})
    rw = project(tmp_path, "site", {"index.html": "x"})
    org.files.add("harvey", "engineering", f"this-pc {ro}")
    org.files.add("site", "engineering", f"this-pc {rw}", writable=True)
    org.wait([org.submit("engineering", "fix the site files")], timeout=10)
    call = org.calls[-1]
    assert str(ro.resolve()) in call["add_dirs"] and str(rw.resolve()) in call["add_dirs"]
    assert call["read_only"][0] == str(ro.resolve()) and all("sources" in x for x in call["read_only"][1:])
    assert "LIVE FOLDER (read-only)" in call["prompt"] and "LIVE FOLDER (writable)" in call["prompt"]
    from agentsorg.runtime import build_command
    cmd = build_command("claude", "sonnet", "s", ["Read", "Edit", "Write"], 8, read_only=[str(ro.resolve())])
    posix = ro.resolve().as_posix().lstrip("/")
    assert f"Edit(//{posix}/**)" in cmd and f"Write(//{posix}/**)" in cmd
    with pytest.raises(ValueError, match="nothing to sync"):
        org.files.receive_dir("harvey", ro)


def test_text_task_without_file_words_stays_cheap(org, tmp_path):
    org.files.add("brand", "marketing", f"this-pc {project(tmp_path, 'b', {'g.md': 'x'})}", mode="sync")
    org.files.receive_dir("brand", tmp_path / "b")
    org.wait([org.submit("research", "Write a tagline for a booking product")], timeout=10)
    assert org.calls[-1]["tools"] == []                                      # no tool schemas, no map


def test_old_array_reply_still_works(org):
    org.ceo_reply = json.dumps([{"dept": "marketing", "task": "write a headline"}])
    r = org.ceo("something useful please")
    assert len(r["tasks"]) == 1 and r["answer"] == "" and r["actions"] == []


def test_answer_only_creates_no_tasks(org):
    org.ceo_reply = json.dumps({"answer": "Nobody here can check if the site is up.", "actions": [], "tasks": []})
    r = org.ceo("check the site is up")
    assert r["tasks"] == [] and "check if the site is up" in r["answer"]


def test_sync_caps_huge_folders(org, tmp_path, monkeypatch):
    from agentsorg import files
    monkeypatch.setattr(files, "MAX_SYNC_FILES", 2)
    big = project(tmp_path, "big", {"1.txt": "a", "2.txt": "b", "3.txt": "c"})
    org.files.add("big", "research", f"this-pc {big}", mode="sync")
    with pytest.raises(ValueError, match="too big"):
        org.files.receive_dir("big", big)


def test_sync_now_api(org, tmp_path):
    from fastapi.testclient import TestClient
    from agentsorg.api import create_app
    local = project(tmp_path, "here", {"a.md": "x"})
    org.files.add("here", "engineering", f"this-pc {local}", mode="sync")
    org.files.add("there", "engineering", "zoro C:/not-here")
    c = TestClient(create_app(org)); h = {"X-Token": org.c.token()}
    assert c.post("/api/files/here/sync", headers=h).json()["added"] == 1
    assert c.post("/api/files/there/sync", headers=h).status_code == 400
    srcs = {s["name"]: s for d in c.get("/api/files", headers=h).json()["depts"] for s in d["sources"]}
    assert srcs["here"]["local"] and not srcs["there"]["local"]
