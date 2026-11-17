"""0.7.1: agents know the org chart, fewer owner questions, Research decides when the owner lets it, the activity log."""
import json

from fastapi.testclient import TestClient

from test_pipeline import org, until  # noqa: F401  (fixture)

from agentsorg import activity, decisions, inbox
from agentsorg.api import create_app


def test_every_employee_knows_the_org_chart(org):
    sp = org.system_prompt("marketing", org.c.depts["marketing"].members[0].id)
    assert "you are an employee" in sp and "owner (a human founder)" in sp
    assert "research:" in sp and "sales:" in sp and "ASK: <id>" in sp
    assert "research decides prices" in sp                                  # few questions is the default
    org.c.raw["ask_owner"] = "all"
    org.c.save()
    org._sys_cache.clear()
    assert "decides prices" not in org.system_prompt("marketing", org.c.depts["marketing"].members[0].id)


def test_a_plain_follow_up_is_not_a_question_for_the_owner(org):
    pid = org.projects.create("p", status="review")
    org.projects.suggest(pid, "Add tests that expiry is idempotent when the timer runs twice.", "engineering", "x", 1)
    org.projects.suggest(pid, "Once the owner gives prices, set the config values.", "engineering", "x", 1)
    kinds = [s["kind"] for s in org.projects.suggestions(pid)]
    assert kinds == ["next", "question"]


def _decided(org):
    org.work = lambda p: ("DECISION: $49/month, USD, billed monthly\nCompetitors charge $45-60.\nDONE: decided"
                          if "DECISION NEEDED" in p else "the work\nDONE: did it")


def test_research_decides_and_the_question_is_never_asked_again(org):
    org.c.raw["ask_owner"] = "all"
    org.c.save()
    _decided(org)
    p1 = org.projects.create("emails", status="review")
    org.projects.suggest(p1, "Owner decides the chatbot price and currency, then update the copy.", "sales", "lead", 1)
    assert [i["kind"] for i in inbox.items(org) if i["kind"] == "decision"] == ["decision"]
    assert decisions.delegate(org) == []                                     # "ask me everything": nothing delegated
    org.c.raw["ask_owner"] = "few"
    org.c.save()
    tids = decisions.delegate(org)
    assert len(tids) == 1 and org.s.task(tids[0])["dept"] == "research"
    assert not [i for i in inbox.items(org) if i["kind"] == "decision"]     # handed over, not waiting on the owner
    until(lambda: org.s.get("decided_news"))
    s = org.projects.suggestions(p1)[0]
    assert s["state"] == "answered" and s["answer"].startswith("$49/month") and s["auto"] == "Research"
    news = [i for i in inbox.items(org) if i["kind"] == "decided"]
    assert len(news) == 1 and "$49/month" in news[0]["detail"]
    cont = org.s.one("SELECT * FROM tasks WHERE dept='sales' AND body LIKE 'Research decided%'")
    assert cont and cont["project_id"] == p1
    assert org.s.one("SELECT COUNT(*) n FROM kb WHERE tags LIKE '%company decision%'")["n"] == 1
    # the same question from another project: applied, not asked
    p2 = org.projects.create("landing", status="review")
    org.projects.suggest(p2, "Once the owner confirms the price and currency, fill in the pricing table.", "marketing",
                         "lead", 999)
    s2 = org.projects.suggestions(p2)[0]
    assert s2["state"] == "answered" and s2["auto"] == "Research"
    assert not [i for i in inbox.items(org) if i["kind"] == "decision"]
    # the owner can overrule it
    c, h = TestClient(create_app(org)), {"X-Token": org.c.token()}
    r = c.post(f"/api/decisions/decided/{news[0]['id']}/change", json={"text": "$59/month"}, headers=h).json()
    assert len(r["started"]) == 1 and decisions.already_decided(org, "the price?")["answer"] == "$59/month"


def test_owner_only_questions_stay_with_the_owner(org):
    pid = org.projects.create("site", status="review")
    org.projects.suggest(pid, "Once the owner gives the public domain, replace <DOMAIN>.", "marketing", "lead", 1)
    org.projects.suggest(pid, "Owner to approve the deploy to production.", "engineering", "x", 1)
    assert decisions.delegate(org) == []
    assert len([i for i in inbox.items(org) if i["kind"] == "decision"]) == 2


def test_a_team_that_had_the_answer_and_asks_again_reaches_the_owner(org):
    decisions.remember(org, "What is the price?", "$49/month", "owner")
    pid = org.projects.create("x", status="review")
    tid = org.submit("sales", "This was already decided (owner), so it was not asked again.\nQUESTION: price\n"
                              "DECISION: $49/month", project_id=pid)
    org.projects.suggest(pid, "Owner decides the price for the chatbot add-on.", "sales", "lead", tid)
    assert org.projects.suggestions(pid)[0]["state"] == "open"              # no loop: it goes to the owner


def test_activity_log_shows_what_was_asked_and_what_it_became(org):
    org.env.set("STRIPE_KEY", "sk_test_supersecret123456")
    c, h = TestClient(create_app(org)), {"X-Token": org.c.token()}
    c.post("/api/ceo", json={"text": "write the landing page"}, headers=h)
    c.put("/api/env/OTHER", json={"value": "hunter2-very-secret"}, headers=h)
    until(lambda: any(e["actor"] == "agent" for e in activity.read(org)))
    log = activity.read(org)
    owner = [e for e in log if e["actor"] == "owner"]
    assert owner[0]["event"] == "POST /api/ceo" and owner[0]["text"] == "write the landing page"
    ceo = next(e for e in log if e["actor"] == "ceo")
    assert ceo["asked"].startswith("write the landing page") and ceo["handed_out"]
    raw = activity.path(org).read_text()
    assert "hunter2" not in raw and "supersecret" not in raw
    r = c.get("/api/activity?actor=agent", headers=h).json()["entries"]
    assert r and all(e["actor"] == "agent" for e in r)
    assert "write the landing page" in activity.line(owner[0])
