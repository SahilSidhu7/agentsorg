import io
import tarfile

import pytest

from agentsorg import agents as agents_mod
from agentsorg.agents import Org
from agentsorg.config import init_home
from agentsorg.files import tar_bytes
from agentsorg.runtime import RunResult


@pytest.fixture
def org(tmp_path, monkeypatch):
    init_home(tmp_path)
    o = Org(tmp_path)
    o.exe = "fake"
    o.calls = []

    def fake_run(exe, prompt, *, model, system, tools=None, max_turns=1, cwd=None, timeout=900, effort="", add_dirs=None, read_only=None, env=None, **_):
        o.calls.append({"tools": tools, "cwd": cwd, "prompt": prompt, "add_dirs": add_dirs, "read_only": read_only})
        return RunResult(text="Here is the plan.\nDONE: wrote plan", ok=True, model=model,
                         usage=[{"model": model, "input": 1, "output": 1, "cache_read": 0, "cache_create": 0, "cost_usd": 0}])

    monkeypatch.setattr(agents_mod, "run_claude", fake_run)
    yield o
    o.close()           # no worker may leak into the next test's fake Claude


def untar(buf: io.BytesIO) -> dict[str, bytes]:
    buf.seek(0)
    with tarfile.open(fileobj=buf, mode="r:gz") as t:
        return {m.name: t.extractfile(m).read() for m in t if m.isfile()}


def test_layout(org):
    h = org.home
    assert (h / "shared").is_dir() and not (h / "workspace").exists()
    for d in org.c.depts:
        assert (h / d / "sources").is_dir() and (h / d / "outputs").is_dir()
        assert (h / d / "instructions.md").exists() and (h / d / "skills").is_dir()   # one folder per department


def test_two_way_sync(org):
    f = org.files
    f.add("app", "engineering", "PC1 C:\\app")
    root = f.path(f.get("app"))
    # first push from the PC
    rep = f.receive("app", tar_bytes({"src/a.py": b"A1", "README.md": b"R1", "node_modules/x.js": b"junk",
                                      "../../evil.txt": b"x"}))
    assert sorted(rep["added"]) == ["README.md", "src/a.py"]
    assert not (root / "node_modules").exists() and not (org.home / "evil.txt").exists()
    # nothing changed by agents yet -> pull sends nothing
    out = io.BytesIO(); assert f.send("app", out)["sent"] == []
    # agents edit a.py and add b.py
    (root / "src/a.py").write_bytes(b"A1-agent"); (root / "src/b.py").write_bytes(b"B-agent")
    # PC pushes again with README changed only (a.py unchanged since sync): agents' edit must survive
    rep = f.receive("app", tar_bytes({"src/a.py": b"A1", "README.md": b"R2"}))
    assert rep["updated"] == ["README.md"] and (root / "src/a.py").read_bytes() == b"A1-agent"
    assert "src/b.py" in rep["kept_agent_files"] and (root / "src/b.py").exists()
    # pull returns exactly the agents' changes
    out = io.BytesIO(); sent = f.send("app", out)
    assert sorted(sent["sent"]) == ["src/a.py", "src/b.py"]
    assert untar(out) == {"src/a.py": b"A1-agent", "src/b.py": b"B-agent"}
    out = io.BytesIO(); assert f.send("app", out)["sent"] == []          # already delivered
    # both sides change a.py -> conflict: agents' version kept, PC version saved beside it, held from pull
    (root / "src/a.py").write_bytes(b"A2-agent")
    rep = f.receive("app", tar_bytes({"src/a.py": b"A2-pc", "README.md": b"R2", "src/b.py": b"B-agent"}))
    assert rep["conflicts"] == ["src/a.py"]
    assert (root / "src/a.py").read_bytes() == b"A2-agent" and (root / "src/a.from-pc.py").read_bytes() == b"A2-pc"
    out = io.BytesIO(); sent = f.send("app", out)
    assert sent["sent"] == [] and sent["held_conflicts"] == ["src/a.py"]  # never overwrites the PC's edit
    # resolve: keep the merged agent version and delete the side copy -> it goes back to the PC
    (root / "src/a.from-pc.py").unlink()
    out = io.BytesIO(); assert f.send("app", out)["sent"] == ["src/a.py"]
    # file deleted on the PC (unchanged on server) is deleted on the server too
    rep = f.receive("app", tar_bytes({"src/a.py": b"A2-agent", "src/b.py": b"B-agent"}))
    assert rep["deleted"] == ["README.md"] and not (root / "README.md").exists()


def test_outputs_and_read_access(org):
    t = org.wait([org.submit("marketing", "landing page plan")], timeout=10)[0]
    outs = org.files.outputs("marketing")
    assert len(outs) == 1 and outs[0]["name"].endswith(f"_{t['id']}_landing-page-plan.md")
    assert "Here is the plan." in org.files.read_output("marketing", outs[0]["name"])
    with pytest.raises(KeyError):
        org.files.read_output("marketing", "../../company.yaml")
    assert org.calls[-1]["tools"] == []                                  # text dept, no linked folders: no tools
    org.files.add("brand", "marketing", "PC2 D:\\brand")
    org.files.receive("brand", tar_bytes({"guide.md": b"# Brand guide\nOur tone is warm", "logo.png": b"\x89PNG"}))
    org.wait([org.submit("marketing", "write the about page")], timeout=10)
    call = org.calls[-1]
    assert call["tools"] == ["Read", "Glob", "Grep"] and call["cwd"] == org.home / "marketing"
    assert "sources/brand/guide.md" in call["prompt"] and "1 png" in call["prompt"]   # map: docs listed, media counted
    assert "outputs/: 1 past deliverables" in call["prompt"]
    assert call["add_dirs"] == [str(org.home / "shared")]


def test_cli_send_receive_roundtrip(org, monkeypatch, capsysbinary):
    import sys
    from agentsorg import cli
    org.files.add("docs", "research")
    data = tar_bytes({"notes.md": b"hello"})
    monkeypatch.setattr(sys, "stdin", type("S", (), {"buffer": data})())
    cli.main(["--home", str(org.home), "files", "receive", "docs"])
    assert (org.files.path(org.files.get("docs")) / "notes.md").read_bytes() == b"hello"
    (org.files.path(org.files.get("docs")) / "notes.md").write_bytes(b"hello, edited")
    capsysbinary.readouterr()
    cli.main(["--home", str(org.home), "files", "send", "docs"])
    out = capsysbinary.readouterr().out
    assert untar(io.BytesIO(out)) == {"notes.md": b"hello, edited"}      # stdout is a clean tar


def test_same_machine_sync(org, tmp_path):
    proj = tmp_path / "myproject"
    (proj / "src").mkdir(parents=True)
    (proj / "src" / "app.py").write_text("v1")
    (proj / "node_modules").mkdir()
    (proj / "node_modules" / "junk.js").write_text("x")
    org.files.add("proj", "engineering", f"this-pc {proj}", mode="sync")
    rep = org.files.receive_dir("proj", proj)
    assert rep["added"] == ["src/app.py"]                                  # excludes honoured
    root = org.files.path(org.files.get("proj"))
    (root / "src" / "app.py").write_text("v2 by agents")
    (root / "NOTES.md").write_text("new")
    rep = org.files.send_dir("proj", proj)
    assert sorted(rep["sent"]) == ["NOTES.md", "src/app.py"]
    assert (proj / "src" / "app.py").read_text() == "v2 by agents" and (proj / "NOTES.md").read_text() == "new"
    c = org.files.commands("proj")
    assert "--from" in c["push_local"] and "--to" in c["pull_local"]


def test_cli_local_flags(org, tmp_path, capsys):
    from agentsorg import cli
    proj = tmp_path / "p"; proj.mkdir(); (proj / "a.txt").write_text("hi")
    cli.main(["--home", str(org.home), "files", "add", "p", "--dept", "research", "--origin", f"me {proj}", "--mode", "sync"])
    cli.main(["--home", str(org.home), "files", "receive", "p", "--from", str(proj)])
    cli.main(["--home", str(org.home), "files", "send", "p", "--to", str(proj)])
    out = capsys.readouterr().out
    assert "Same machine as the company" in out and "+1 added" in out and "wrote 0 file(s)" in out


def test_design_maps_described_folders(org, tmp_path):
    from agentsorg import design
    raw = {"name": "Shop", "about": "A shop",
           "departments": [{"id": "eng", "name": "Engineering", "tools": ["Read", "Edit"],
                            "members": [{"id": "lead", "name": "Ada", "tier": 2}]},
                           {"id": "content", "name": "Content", "members": [{"id": "w", "name": "Wren"}]}],
           "sources": [{"name": "App Code", "dept": "eng", "origin": "PC1 C:/work/app", "why": "backend"},
                       {"name": "brand", "dept": "content", "origin": "PC2 D:/brand"},
                       {"name": "ghost", "dept": "nope", "origin": "PC3 X:/y"},          # unknown dept: dropped
                       {"name": "brand", "dept": "eng", "origin": "PC1 dup"},             # duplicate: dropped
                       {"name": "empty", "dept": "eng", "origin": ""}]}                   # no path: dropped
    plan = design.validate(raw)
    assert [(s["name"], s["dept"]) for s in plan["sources"]] == [("app-code", "eng"), ("brand", "content")]
    assert "2 linked folder(s)" in design.summarize(plan)
    linked = design.apply(org, plan, "I run a shop.\nFOLDER this-pc C:/x\n  src/: 3 files")
    assert linked == ["app-code", "brand"]
    src = org.files.sources()
    assert src["app-code"].dept == "eng" and src["brand"].origin == "PC2 D:/brand"
    assert (org.home / "content" / "sources" / "brand").is_dir()
    assert "FOLDER" not in org.kb.search("shop")[0]["body"]                       # outline kept out of the brief
    assert design.apply(org, plan) == []                                          # re-apply: already linked


def test_folder_outline(tmp_path):
    from agentsorg.design import folder_outline
    root = tmp_path / "proj"
    (root / "src" / "api").mkdir(parents=True)
    (root / "src" / "api" / "routes.py").write_text("x")
    (root / "src" / "main.py").write_text("x")
    (root / "assets").mkdir(); (root / "assets" / "a.png").write_bytes(b"1"); (root / "assets" / "b.png").write_bytes(b"1")
    (root / "node_modules" / "pkg").mkdir(parents=True)
    (root / "README.md").write_text("# Proj\nOrder sync service")
    out = folder_outline(str(root), label="PC1")
    assert out.splitlines()[0] == f"FOLDER PC1 {root.resolve()}"
    assert "src/: 1 files (1 py)" in out and "api/: 1 files (1 py)" in out and "assets/: 2 files (2 png)" in out
    assert "node_modules" not in out and "Order sync service" in out


def test_cli_design_scan(org, tmp_path, monkeypatch, capsys):
    from agentsorg import cli, design
    seen = {}
    def fake_propose(o, desc):
        seen["desc"] = desc
        raise RuntimeError("stop here")
    monkeypatch.setattr(design, "propose", fake_propose)
    proj = tmp_path / "p"; (proj / "docs").mkdir(parents=True); (proj / "docs" / "a.md").write_text("x")
    cli.main(["--home", str(org.home), "design", "My shop", "--scan", f"PC2={proj}"])
    assert seen["desc"].startswith("My shop") and f"FOLDER PC2 {proj.resolve()}" in seen["desc"]
    assert "docs/: 1 files (1 md)" in seen["desc"]


def test_live_folders_linked_into_company(org, tmp_path):
    real = tmp_path / "elsewhere" / "harvey"
    (real / "src").mkdir(parents=True)
    (real / "src" / "app.py").write_text("print(1)")
    org.files.add("harvey", "sales", f"this-pc {real}")
    link = org.home / "sales" / "sources" / "harvey"
    if not org.files._is_link(link):
        pytest.skip("this OS user can't create links")
    assert (link / "src" / "app.py").read_text() == "print(1)"          # the whole project, reachable from one place
    tid = org.submit("sales", "read the harvey code and summarize it")
    org.wait([tid])
    ro = org.calls[-1]["read_only"]
    assert str(real.resolve()) in ro and str(link) in ro                # read-only both ways in
    (org.home / "sales" / "sources" / "realdir").mkdir()
    org.files.remove("harvey", delete_files=True)                       # never deletes the real project
    assert (real / "src" / "app.py").exists() and not org.files._is_link(link)
    assert (org.home / "sales" / "sources" / "realdir").is_dir()        # only links are ever cleaned up
