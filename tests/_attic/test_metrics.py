from agentsorg import agents as agents_mod
from agentsorg.reports import run_reports
from agentsorg.runtime import RunResult

from test_org import org  # noqa: F401  (fixture)


def test_metrics_from_owner_and_agents(org, monkeypatch):
    org.metrics.set("Waitlist signups", 3, target=50)
    monkeypatch.setattr(agents_mod, "run_claude", lambda *a, **k: RunResult(
        text="Counted the signups.\nDONE: counted\nMETRIC: waitlist_signups = 12\nMETRIC: reply rate = 4.5%",
        ok=True, model="haiku", usage=[]))
    t = org.wait([org.submit("marketing", "count the waitlist signups in the csv")])[0]
    assert "METRIC" not in t["result"]                                   # footer stripped from the deliverable
    m = {r["name"]: r for r in org.metrics.all()}
    assert m["waitlist_signups"]["value"] == 12 and m["waitlist_signups"]["target"] == 50
    assert m["reply_rate"]["value"] == 4.5 and m["reply_rate"]["unit"] == "%"
    assert "waitlist_signups: 12 / 50" in org.company_facts()
    assert "**Numbers:**" in run_reports(org)["_company"]


def test_metrics_api(org):
    from fastapi.testclient import TestClient
    from agentsorg.api import create_app
    c = TestClient(create_app(org)); h = {"X-Token": org.c.token()}
    assert c.put("/api/metrics/mrr", json={"value": 120, "target": 1000, "unit": "£"}, headers=h).json()["value"] == 120
    assert c.put("/api/metrics/mrr", json={"value": 150}, headers=h).json()["target"] == 1000   # keeps the target
    assert c.get("/api/metrics", headers=h).json()["metrics"][0]["name"] == "mrr"
    assert c.put("/api/metrics/!!", json={"value": 1}, headers=h).status_code == 400
    assert c.delete("/api/metrics/mrr", headers=h).json()["ok"]
