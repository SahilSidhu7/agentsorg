"""0.7: the business profile, departments that cover every function, the org roadmap and its triggers."""
import json

import pytest
from fastapi.testclient import TestClient

from test_pipeline import org, until  # noqa: F401  (fixture)

from agentsorg import blueprints, design
from agentsorg.api import create_app


@pytest.fixture
def api(org):
    return TestClient(create_app(org)), {"X-Token": org.c.token()}


def test_design_covers_every_function_now_or_later():
    plan = design.validate({"name": "Co", "departments": [
        {"id": "product", "name": "Product", "members": [{"id": "lead", "name": "Ada"}]},
        {"id": "growth", "name": "Growth", "mission": "find customers", "functions": ["sales"],
         "members": [{"id": "lead", "name": "Max"}]}],
        "later": [{"function": "support", "when": {"metric": "customers", "gte": 5}, "why": "x"},
                  {"function": "bogus", "when": {"stage": "idea"}}]}, stage="idea")
    now = {f for d in plan["departments"] for f in d["functions"]}
    later = {x["function"]: x for x in plan["later"]}
    assert set(blueprints.FUNCTIONS) <= now | set(later)                     # nothing is left uncovered
    assert {"research", "marketing"} <= now                                  # the essential ones are never dropped
    growth = next(d for d in plan["departments"] if d["id"] == "growth")
    assert {"research", "marketing"} <= {m["id"] for m in growth["members"]}  # each has a named person
    assert later["support"]["when"] == {"metric": "customers", "gte": 5.0} and "bogus" not in later
    assert later["marketing"]["when"] == {"stage": "building"}               # folded now, its own team later
    assert later["finance"]["when"] == {"metric": "customers", "gte": 1}     # the blueprint's trigger


def test_goals_can_carry_a_number_and_a_date(org, api):
    c, h = api
    r = c.put("/api/goals", json={"goals": ["Be known in the clinic market",
                                            {"text": "10 paying clinics", "metric": "customers", "target": 10,
                                             "due": "2027-01-31", "dept": "sales"}]}, headers=h).json()
    assert r["goals"] == ["Be known in the clinic market", "10 paying clinics"]
    org.metrics.set("customers", 4)
    obs = c.get("/api/goals", headers=h).json()["objectives"]
    assert obs[1]["value"] == 4 and obs[1]["percent"] == 40 and "days_left" in obs[1]
    assert "customers: 4 of 10" in org.company_facts()
    assert "customers to 10, by 2027-01-31" in org.system_prompt("sales", "lead")
    c.put("/api/goals", json={"goals": ["10 paying clinics"]}, headers=h)    # the text list keeps the number
    assert org.c.objectives == [{"text": "10 paying clinics", "metric": "customers", "target": 10.0,
                                 "due": "2027-01-31", "dept": "sales"}]
    org.run_action({"action": "add_goal", "goal": "Launch in May"})
    assert org.c.objectives[0]["metric"] == "customers" and org.c.goals[-1] == "Launch in May"


def test_owner_answer_is_saved_and_never_asked_again(org, api):
    c, h = api
    pid = org.projects.create("x", status="running")
    org.projects.suggest(pid, "Owner decision needed: what is the price of the chatbot?", "sales", "lead", 1)
    sid = org.projects.suggestions(pid)[0]["id"]
    r = c.post(f"/api/projects/{pid}/suggestions/{sid}/answer", json={"text": "49 EUR a month", "run": True},
               headers=h).json()
    assert r["saved"] and org.s.task(r["task"])["dept"] == "sales" and "49 EUR a month" in org.s.task(r["task"])["body"]
    assert org.projects.suggestions(pid)[0]["state"] == "answered"
    facts = org.company_facts()
    assert "OWNER'S ANSWERS" in facts and "49 EUR a month" in facts
    assert c.post(f"/api/projects/{pid}/suggestions/{sid}/answer", json={"text": "x"}, headers=h).status_code == 404


def test_work_routes_by_function_not_by_department_name(org):
    design.apply(org, {**design.LEAN, "name": "Co"})
    assert org._guess_dept("fix the bug in the signup api") == "product"
    assert org._guess_dept("write a blog post about clinics") == "growth"
    assert org._guess_dept("what do competitors charge") == "growth"
    assert org._guess_dept("send the invoice") == "ops"
