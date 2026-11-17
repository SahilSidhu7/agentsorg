import json
import time

from test_ceo_ops import org  # noqa: F401  (fixture)


def test_ceo_proposes_dept_and_owner_approves(org):
    org.ceo_reply = json.dumps({
        "answer": "Support needs its own team; approve it on the dashboard.",
        "actions": [{"action": "propose_dept", "id": "support", "name": "Customer Support", "mission": "answer customers",
                     "why": "tickets are piling up", "tools": ["Edit", "Write"],
                     "members": [{"id": "lead", "name": "Kai", "role": "support lead", "tier": 3}]}],
        "tasks": [{"dept": "support", "task": "draft 5 canned replies"}, {"dept": "marketing", "task": "tweet"}]})
    r = org.ceo("set up a support team and draft canned replies")
    assert "support" not in org.c.depts                         # nothing exists before the owner says yes
    assert any(a.startswith("? proposed new department Customer Support") for a in r["actions"])
    assert len(r["tasks"]) == 1                                 # marketing ran; support's task is held
    p = org.proposals.get("support")
    assert p["tasks"][0]["task"] == "draft 5 canned replies" and p["why"] == "tickets are piling up"
    assert "PROPOSED DEPARTMENTS" in org.company_facts()

    out = org.proposals.approve("support")
    d = org.c.depts["support"]
    assert out["dept"] == "support" and d.name == "Customer Support" and d.tools == []   # abilities stay the owner's call
    assert d.members[0].tier == 2                               # clamped: never Opus by default
    assert org.s.task(out["tasks"][0])["dept"] == "support"
    assert (org.home / "support").is_dir() and org.proposals.all() == []
    assert "marketing" in org.c.depts                           # existing departments untouched


def test_proposal_api_reject_and_duplicates(org):
    from fastapi.testclient import TestClient
    from agentsorg.api import create_app
    c = TestClient(create_app(org)); h = {"X-Token": org.c.token()}
    assert org.run_action({"action": "propose_dept", "id": "marketing", "members": [{"id": "a"}]}).startswith("✗")
    org.run_action({"action": "propose_dept", "id": "legal", "name": "Legal", "members": [{"id": "lee", "role": "counsel"}]})
    assert c.get("/api/attention", headers=h).json()["proposals"][0]["id"] == "legal"
    assert c.post("/api/proposals/legal/reject", headers=h).json()["ok"]
    assert "legal" not in org.c.depts and c.get("/api/proposals", headers=h).json()["proposals"] == []
    assert c.post("/api/proposals/legal/approve", headers=h).status_code == 404


def test_backlog_run_all(org):
    from fastapi.testclient import TestClient
    from agentsorg.api import create_app
    c = TestClient(create_app(org)); h = {"X-Token": org.c.token()}
    ids = [org.s.add_task(dept="marketing", member="lead", body=f"idea {i}", source="next", status="backlog")
           for i in range(2)]
    assert c.get("/api/state", headers=h).json()["backlog"] == 2
    c.post("/api/backlog/run", headers=h)
    end = time.time() + 10
    while time.time() < end and any(org.s.task(i)["status"] in ("queued", "running") for i in ids):
        time.sleep(0.1)
    assert all(org.s.task(i)["status"] == "done" for i in ids)
