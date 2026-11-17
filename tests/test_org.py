import json

import pytest

from agentsorg import agents as agents_mod
from agentsorg import router
from agentsorg.agents import Org
from agentsorg.config import init_home

from agentsorg.runtime import RunResult, build_command, parse_payload


class FakeQuota:
    def __init__(self, worst=None):
        self.w = worst

    def utilization(self, force=False):
        return None if self.w is None else {"five_hour": {"utilization": self.w, "resets_at": ""}}

    def worst(self):
        return self.w


@pytest.fixture
def org(tmp_path, monkeypatch):
    init_home(tmp_path)
    o = Org(tmp_path)
    o.exe = "fake-claude"
    o.budget.quota = FakeQuota(10)
    calls = []

    def fake_run(exe, prompt, *, model, system, tools=None, max_turns=1, cwd=None, timeout=900, effort="", add_dirs=None, read_only=None, env=None, **_):
        calls.append({"model": model, "prompt": prompt, "system": system, "tools": tools, "env": env})
        if "WHAT THE MOD SHOULD DO" in prompt:
            text = ("```yaml\ndescription: refunds\nenabled: true\napplies_to: [sales]\n"
                    "system: Mention the 30-day refund.\n```")
        elif "COMMAND:" in prompt:   # CEO router
            plan = [{"dept": "marketing", "member": "copy", "tier": 1, "task": "write headline"},
                    {"dept": "research", "member": "lead", "tier": 2, "task": "market note", "after": 0}]
            text = json.dumps(plan)
        elif "hard thing" in prompt and model == "haiku":
            text = "ESCALATE: too hard"
        else:
            text = f"result by {model}\nDONE: did it\nKB+: Pricing | We charge $19/mo\nNEXT: follow up"
        return RunResult(text=text, ok=True, model=model,
                         usage=[{"model": model, "input": 10, "output": 5, "cache_read": 0, "cache_create": 0,
                                 "cost_usd": 0.0}])

    monkeypatch.setattr(agents_mod, "run_claude", fake_run)
    o.fake_calls = calls
    yield o
    o.close()           # no worker may leak into the next test's fake Claude


def test_router_tiers():
    assert router.pick_tier("summarize this ticket", 2, 2) == 1
    assert router.pick_tier("build the payments service", 2, 2) == 2
    assert router.pick_tier("system design for multi-region", 2, 3) == 3
    assert router.pick_tier("system design for multi-region", 2, 2) == 2   # capped by member
    assert router.forced_tier("do x !opus") == (3, "do x")
    assert router.escalate(1, 2) == 2 and router.escalate(3, 3) is None


def test_command_is_isolated_and_toolless():
    cmd = build_command("claude", "haiku", "sys", [], 1)
    assert cmd[cmd.index("--tools") + 1] == "" and "--system-prompt" in cmd and "--strict-mcp-config" in cmd
    cmd = build_command("claude", "sonnet", "sys", ["Read", "Edit"], 8)
    assert "Read,Edit" in cmd and "acceptEdits" in cmd


def test_parse_payload():
    raw = json.dumps({"type": "result", "result": "hi", "is_error": False, "total_cost_usd": 0.01,
                      "modelUsage": {"claude-haiku": {"inputTokens": 3, "outputTokens": 2, "costUSD": 0.01}}})
    r = parse_payload(raw, "haiku")
    assert r.ok and r.text == "hi" and r.tokens == 5
    assert not parse_payload("garbage", "haiku").ok


def test_budget_gate(org):
    org.budget.quota = FakeQuota(39)
    org.s.set("auto", True)
    assert org.budget.check(auto=True)[0]
    org.budget.quota = FakeQuota(41)
    assert not org.budget.check(auto=True)[0]
    assert org.budget.check(auto=False)[0]           # manual allowed until 90
    org.budget.quota = FakeQuota(None)               # unreadable -> call-count fallback
    assert org.budget.check(auto=True)[0]
    org.s.set("paused", True)
    assert not org.budget.check(auto=False)[0]


def test_escalation(org):
    t = org.submit("research", "hard thing summary", member="analyst")
    res = org.wait([t], timeout=10)[0]
    assert res["status"] == "done" and res["model"] == "sonnet"


def test_head_delegation_and_office(org):
    from agentsorg.office import member_report, office_state
    r = org.delegate("marketing", "launch campaign")
    assert r["routing"] == "head" and len(r["tasks"]) == 2
    done = org.wait(r["tasks"], timeout=10)
    assert all(t["dept"] == "marketing" for t in done)            # head only delegates inside its dept
    assert {t["member"] for t in done} <= {m.id for m in org.c.depts["marketing"].members}
    st = office_state(org)
    mk = next(d for d in st["depts"] if d["id"] == "marketing")
    assert mk["members"][0]["head"] and sum(m["done"] for m in mk["members"]) >= 2
    assert any(m["last"] for m in mk["members"])                  # speech bubble data
    rep = member_report(org, "marketing", "copy")
    assert rep["report"][-1].startswith("used ") and rep["tasks"]


def test_design_validate_and_apply(org):
    from agentsorg import design
    raw = {"name": "Candle Co", "about": "Hand-poured candles sold on Shopify",
           "departments": [
               {"id": "Growth Team", "name": "Growth", "mission": "content + ads", "tools": ["Bash", "Read"],
                "rules": "Be punchy.", "skill": "Use seasonal hooks.",
                "members": [{"id": "lead", "name": "Mia", "role": "Head of growth", "tier": 3, "max_tier": 3},
                            {"id": "w", "name": "Jo", "role": "Writer", "tier": 1, "max_tier": 1},
                            {"id": "x", "name": "X"}, {"id": "y", "name": "Y"}]},
               {"id": "ops", "name": "Ops", "members": [{"id": "boss", "name": "Al", "max_tier": 3}]},
               {"id": "ceo", "name": "nope", "members": [{"id": "a"}]}]}
    plan = design.validate(raw)
    g = plan["departments"][0]
    assert g["id"] == "growth_team" and len(g["members"]) == 3          # max 3 per dept
    assert g["tools"] == ["Read"]                                        # Bash stripped
    assert g["members"][0]["tier"] == 2                                  # never opus by default
    assert sum(m["max_tier"] == 3 for d in plan["departments"] for m in d["members"]) == 1
    assert all(d["id"] != "ceo" for d in plan["departments"])
    design.apply(org, plan, "I sell candles")
    assert set(org.c.depts) == {"growth_team", "ops"} and org.c.about.startswith("Hand-poured")
    assert "Be punchy" in org.system_prompt("growth_team", "lead") and "Hand-poured" in org.system_prompt("ops", "boss")
    assert org.kb.search("candles")                                      # brief shared via KB
    assert not (org.home / "finance").exists()                           # untouched default dept folder removed
    (org.home / "legal").mkdir(); (org.home / "legal" / "notes.md").write_text("mine")
    design.prune_untouched_depts(org)
    assert (org.home / "legal" / "notes.md").exists()                    # anything of the user's stays
    t = org.submit("growth_team", "write a tweet", member="w")
    assert org.wait([t], timeout=10)[0]["status"] == "done"


def test_api_smoke(org):
    from fastapi.testclient import TestClient
    from agentsorg.api import create_app
    c = TestClient(create_app(org))
    assert c.get("/api/state").status_code == 401
    h = {"X-Token": org.c.token()}
    s = c.get("/api/state", headers=h).json()
    assert len(s["depts"]) == 5
    assert c.get("/").status_code == 200
    r = c.post("/api/dept/marketing", json={"text": "tagline for app"}, headers=h).json()
    org.wait(r["tasks"], timeout=10)
    assert c.get(f"/api/task/{r['tasks'][0]}", headers=h).json()["status"] == "done"
    assert c.post("/api/kb", json={"title": "ICP", "body": "solo founders"}, headers=h).status_code == 200
    assert c.get("/api/usage", headers=h).status_code == 200
    assert len(c.get("/api/office", headers=h).json()["depts"]) == 5
    assert c.get("/api/member/marketing/lead", headers=h).json()["head"] is True
    assert c.post("/api/dept/sales/delegate", json={"text": "follow up leads"}, headers=h).json()["routing"] == "head"
    for f in ("office.js", "office.css"):
        assert c.get(f"/static/{f}").status_code == 200


def test_workspace_map(tmp_path):
    from agentsorg.wsmap import workspace_map
    assert "empty" in workspace_map(tmp_path)
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text('"""Order sync service entrypoint."""\nimport os\n')
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.js").write_text("junk")
    (tmp_path / "logo.png").write_bytes(b"\x89PNG")
    m = workspace_map(tmp_path)
    assert "app/main.py" in m and "Order sync service entrypoint" in m
    assert "node_modules" not in m and "logo.png" not in m
    assert workspace_map(tmp_path) is m          # cached until files change


