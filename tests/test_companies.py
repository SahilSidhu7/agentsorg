import json
from pathlib import Path

import pytest

from agentsorg import config
from agentsorg.agents import Org
from agentsorg.config import init_home, load_registry, register, resolve_home


def test_two_companies_get_their_own_port_and_name(tmp_path, monkeypatch):
    a, b = tmp_path / "marchlight-office", tmp_path / "side project"
    init_home(a); register(a)
    init_home(b); register(b)
    ca, cb = config.Company(a), config.Company(b)
    assert ca.port != cb.port                                   # never collide on one machine
    reg = load_registry()
    assert set(reg["companies"]) == {"marchlight-office", "side-project"} and reg["default"] == "marchlight-office"
    assert ca.slug == "marchlight-office" and cb.slug == "side-project"
    assert resolve_home(company="side-project") == b
    monkeypatch.chdir(b / "marketing")                           # inside a company folder: that company wins
    assert resolve_home() == b
    monkeypatch.chdir(tmp_path)                                  # outside any company: the default
    assert resolve_home() == a
    with pytest.raises(FileNotFoundError):
        resolve_home(company="nope")


def test_old_single_pointer_is_migrated(tmp_path):
    home = tmp_path / "legacy"
    init_home(home)
    config.POINTER.parent.mkdir(parents=True, exist_ok=True)
    config.POINTER.write_text(str(home))
    reg = load_registry()
    assert reg["default"] == "legacy" and Path(reg["companies"]["legacy"]["path"]) == home


def test_old_layout_moves_into_department_folders(tmp_path):
    home = tmp_path / "old"
    init_home(home)
    # rebuild the 0.2 layout by hand
    (home / "prompts").mkdir(exist_ok=True)
    (home / "marketing" / "instructions.md").rename(home / "prompts" / "marketing.md")
    (home / "skills").mkdir()
    (home / "marketing" / "skills").rename(home / "skills" / "marketing")
    out = home / "workspace" / "marketing" / "outputs"; out.mkdir(parents=True)
    (out / "2026-10-27_1_post.md").write_text("old output")
    (home / "workspace" / "marketing" / "sources" / "brand").mkdir(parents=True)
    (home / "workspace" / "marketing" / "sources" / "brand" / "guide.md").write_text("g")
    (home / "workspace" / "shared").mkdir(); (home / "workspace" / "shared" / "x.txt").write_text("s")
    import shutil
    shutil.rmtree(home / "shared"); shutil.rmtree(home / "marketing")
    org = Org(home)
    assert org.migrated and (home / "marketing" / "instructions.md").exists()
    assert (home / "marketing" / "skills" / "copy.md").exists()
    assert (home / "marketing" / "outputs" / "2026-10-27_1_post.md").read_text() == "old output"
    assert (home / "marketing" / "sources" / "brand" / "guide.md").exists()
    assert (home / "shared" / "x.txt").read_text() == "s"
    assert not (home / "workspace").exists() and not (home / "skills").exists()
    assert not (home / "prompts" / "marketing.md").exists() and (home / "prompts" / "base.md").exists()
    assert "Marketing rules" in org.system_prompt("marketing", "copy")          # instructions still load
    assert Org(home).migrated == []                                             # idempotent


def test_agents_cannot_edit_their_instructions():
    from agentsorg.runtime import build_command
    cmd = build_command("claude", "sonnet", "s", ["Read", "Edit", "Write"], 8)
    i = cmd.index("--disallowedTools")
    assert {"Edit(instructions.md)", "Write(instructions.md)", "Edit(skills/**)", "Write(skills/**)"} <= set(cmd[i + 1:])
    assert "--disallowedTools" not in build_command("claude", "haiku", "s", ["Read", "Glob"], 4)   # read-only: nothing to deny


def test_sync_commands_name_the_company(tmp_path):
    home = tmp_path / "acme"
    init_home(home); register(home)
    org = Org(home)
    org.files.add("app", "engineering", "PC1 C:/app")
    c = org.files.commands("app")
    assert "--company acme files receive app" in c["push_bash"] and "--company acme files send app" in c["pull_powershell"]


def test_cli_companies_and_header(tmp_path, monkeypatch, capsys):
    from agentsorg import cli
    a, b = tmp_path / "one", tmp_path / "two"
    cli.main(["init", str(a)]); cli.main(["init", str(b)])
    monkeypatch.chdir(tmp_path)
    capsys.readouterr()
    cli.main(["companies"])
    out = capsys.readouterr().out
    assert "one" in out and "two" in out and "(default)" in out
    cli.main(["--company", "two", "goals"])
    err = capsys.readouterr().err
    assert err.startswith("■ two ·")                                            # every command says which company
    cli.main(["companies", "default", "two"])
    assert load_registry()["default"] == "two"
    with pytest.raises(SystemExit):
        monkeypatch.chdir(tmp_path); cli.main(["init"])                         # init needs a folder


def test_member_by_name(tmp_path):
    init_home(tmp_path / "c")
    d = config.Company(tmp_path / "c").depts["marketing"]
    assert d.member("copy").name == "Peggy" and d.member("peggy").id == "copy" and d.member("nobody").id == "lead"
