import time

from agentsorg import agents as agents_mod
from agentsorg.runtime import RunResult

from test_org import org  # noqa: F401  (fixture)


def test_abilities_api_and_facts(org):
    from fastapi.testclient import TestClient
    from agentsorg.api import create_app
    c = TestClient(create_app(org)); h = {"X-Token": org.c.token()}
    r = c.put("/api/dept/research/abilities", json={"abilities": ["web"]}, headers=h).json()
    assert r["abilities"] == ["web"] and org.c.depts["research"].tools == ["WebSearch", "WebFetch"]
    assert "searches and reads the web" in org.company_facts()
    org.env.set("STRIPE_KEY", "sk_test_abcdef123", depts=["research"])
    org.sandbox = False                      # no sandbox: shell needs the owner's explicit acceptance
    r = c.put("/api/dept/research/abilities", json={"abilities": ["web", "shell"]}, headers=h)
    assert r.status_code == 428 and "sandbox" in r.json()["detail"] and "Bash" not in org.c.depts["research"].tools
    c.put("/api/dept/research/abilities", json={"abilities": ["web", "shell"], "accept_unsandboxed": True}, headers=h)
    assert org.s.one("SELECT why FROM decisions WHERE kind='abilities' ORDER BY id DESC")["why"]
    assert "runs shell commands with keys STRIPE_KEY" in org.company_facts()
    st = {d["id"]: d for d in c.get("/api/state", headers=h).json()["depts"]}
    assert st["research"]["abilities"] == ["web", "shell"] and st["research"]["keys"] == ["STRIPE_KEY"]
    assert c.put("/api/dept/research/abilities", json={"abilities": ["root"]}, headers=h).status_code == 400
    c.put("/api/dept/research/abilities", json={"abilities": []}, headers=h)
    assert org.c.depts["research"].tools == []


def test_files_written_by_agent_show_in_result(org, monkeypatch):
    org.c.raw["departments"]["marketing"]["tools"] = ["Read", "Edit", "Write"]
    org.c.save()

    def fake(exe, prompt, *, cwd=None, **k):
        (cwd / "landing.md").write_text("# Join the waitlist\nHome-cooked tiffins.", encoding="utf-8")
        return RunResult(text="DONE: wrote landing page", ok=True, model="haiku", usage=[])
    monkeypatch.setattr(agents_mod, "run_claude", fake)
    t = org.wait([org.submit("marketing", "write the waitlist landing page")])[0]
    assert "### landing.md" in t["result"] and "Join the waitlist" in t["result"]
    outs = org.files.outputs("marketing")
    assert outs and "Join the waitlist" in org.files.read_output("marketing", outs[0]["name"])
