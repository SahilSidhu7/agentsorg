"""0.7: the weekly plan, the brakes on what the company starts by itself, daily follow-ups and the owner's brief."""
import json
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from test_pipeline import kinds, org, until  # noqa: F401  (fixture)

from agentsorg import design, rhythm
from agentsorg.api import create_app
from agentsorg.reports import run_reports

WEEK = {"note": "No leads yet, so find them first.", "priorities": [
    {"title": "Find 20 clinic leads", "objective": "10 paying clinics", "command": "@sales find 20 clinic leads"},
    {"title": "Landing page copy", "objective": "10 paying clinics", "command": "@marketing write the landing page"}]}


@pytest.fixture
def co(org):
    design.set_business(org, {"customer": "clinics", "stage": "launched"})
    org.c.raw["goals"] = [{"text": "10 paying clinics", "metric": "customers", "target": 10}]
    org.c.save()
    plain = org.work
    org.work = lambda p: json.dumps(WEEK) if "ROSTER:" in p and "COMMAND:" not in p else plain(p)
    org.plan = {"answer": "ok", "tasks": [{"dept": "sales", "member": "lead", "tier": 1, "task": "do the priority"}]}
    return org


def week_calls(org):
    return [c for c in org.calls if "Chief of Staff, planning the coming week" in c["system"]]


def test_weekly_plan_starts_by_itself_under_the_auto_cap(co):
    plan = rhythm.run_week(co)
    assert [i["title"] for i in plan["items"]] == ["Find 20 clinic leads", "Landing page copy"]
    assert all(i["project"] for i in plan["items"]) and len(week_calls(co)) == 1
    ctx = week_calls(co)[0]["prompt"]
    assert "customers: ? of 10" in ctx and "BUSINESS:" in ctx                # it plans from the objectives
    tasks = co.s.q("SELECT * FROM tasks WHERE dept!='ceo' AND project_id IN (?,?)",
                   tuple(i["project"] for i in plan["items"]))
    assert tasks and {t["source"] for t in tasks} == {"weekly"}              # not billed as the owner's command
    until(lambda: all(co.s.task(t["id"])["status"] == "done" for t in tasks))
    assert not co.s.get("auto", False)                                       # ran without auto mode switched on
    assert rhythm.run_week(co)["items"][0]["project"] == plan["items"][0]["project"] and len(week_calls(co)) == 1


def test_a_plan_made_at_the_weekend_is_for_the_coming_week():
    from datetime import datetime
    assert rhythm.week_id(datetime(2026, 11, 15, 12)) == rhythm.week_id(datetime(2026, 11, 16, 9)) == "2026-W47"
    assert rhythm.week_id(datetime(2026, 11, 13, 12)) == "2026-W46"


def test_the_week_sees_what_was_built_and_how_old_each_number_is(co):
    import time as _t
    pid = co.projects.create("Build delivery and refunds", status="review")
    co.projects.set(pid, review=json.dumps({"requirements": [
        {"need": "Refunds revoke entitlements", "status": "met", "evidence": "x"},
        {"need": "Chatbot delivered automatically", "status": "partial", "evidence": "y"}]}))
    co.s.x("INSERT INTO changes(created,updated,project_id,source,branch,status) VALUES(1,1,?,'app','agentsorg/p9','ready')",
           (pid,))
    co.metrics.set("failed_tests", 4, source="task #5 (engineering)")
    co.s.x("UPDATE metrics SET updated=? WHERE name='failed_tests'", (_t.time() - 9 * 86400,))
    ctx = rhythm.context(co)
    assert "ALREADY BUILT" in ctx and "Refunds revoke entitlements [met]" in ctx
    assert "agentsorg/p9, waiting for the owner to merge" in ctx
    assert "failed_tests: 4" in ctx and "9 days old: may be out of date" in ctx


def test_approve_mode_waits_for_one_click(co):
    co.c.raw["autonomy"] = "approve"
    co.c.save()
    plan = rhythm.run_week(co)
    assert not any(i["project"] for i in plan["items"])
    c, h = TestClient(create_app(co)), {"X-Token": co.c.token()}
    it = next(i for i in c.get("/api/inbox", headers=h).json()["items"] if i["kind"] == "week")
    assert "2 priorities" in it["title"]
    c.post("/api/week/drop", json={"i": 1}, headers=h)
    r = c.post("/api/week/start", headers=h).json()
    assert len(r["started"]) == 1
    cur = rhythm.current(co)
    assert cur["items"][0]["project"] and cur["items"][1]["state"] == "dropped"
    src = co.s.one("SELECT source FROM tasks WHERE project_id=? AND dept!='ceo'", (cur["items"][0]["project"],))
    assert src["source"] == "ceo"                                            # started by the owner


def test_brakes_stop_the_company_starting_more(co):
    for i in range(4):
        co.projects.create(f"old {i}", status="review")
    plan = rhythm.run_week(co)
    assert not plan["items"] and "waiting for your review" in plan["skipped"] and not week_calls(co)
    co.s.x("UPDATE projects SET status='done'")
    co.s.set(rhythm.KEY, {})
    co.s.set("paused", True)
    assert "paused" in rhythm.run_week(co)["skipped"]
    co.s.set("paused", False)
    co.s.set(rhythm.KEY, {})
    co.c.raw["autonomy"] = "manual"
    co.c.save()
    plan = rhythm.run_week(co)
    assert plan["items"] and not any(i["project"] for i in plan["items"])    # manual: a suggestion only
    assert rhythm.brake(co) == "autonomy is set to manual"


def test_token_ceiling_holds_the_rest_of_the_week(co):
    co.c.raw.setdefault("budget", {})["weekly_plan_tokens"] = 1000
    co.c.save()
    plan = rhythm.make(co)
    rhythm.start(co, 0)
    pid = rhythm.current(co)["items"][0]["project"]
    co.s.x("UPDATE tasks SET tokens=5000 WHERE project_id=?", (pid,))
    out = rhythm.start(co, 1)
    assert "token allowance" in out["held"] and not rhythm.current(co)["items"][1]["project"]
    assert plan["items"]


def test_due_contacts_get_a_drafted_follow_up_not_a_sent_one(co):
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    co.clients.upsert("ana@clinic.example", name="Ana", stage="contacted", next_date=yesterday, force=True)
    co.clients.log(co.clients.by_email("ana@clinic.example")["id"], "email_out", "Booking page for Ruiz Dental")
    co.clients.upsert("later@x.example", stage="contacted", force=True,
                      next_date=(date.today() + timedelta(days=3)).isoformat())
    ids = rhythm.follow_ups(co)
    assert len(ids) == 1
    t = co.s.task(ids[0])
    assert t["source"] == "duty" and "follow-up email to Ana" in t["body"] and "Booking page for Ruiz Dental" in t["body"]
    assert co.clients.by_email("ana@clinic.example")["next_date"] > date.today().isoformat()
    assert rhythm.follow_ups(co) == []                                        # not again today
    for _ in range(4):
        co.clients.log(co.clients.by_email("ana@clinic.example")["id"], "email_out", "again")
    co.clients.upsert("ana@clinic.example", next_date=yesterday, force=True)
    assert rhythm.follow_ups(co) == [] and co.clients.by_email("ana@clinic.example")["next_date"] == ""


def test_first_customer_asks_the_owner_to_confirm_the_stage(co):
    c, h = TestClient(create_app(co)), {"X-Token": co.c.token()}
    assert rhythm.stage_check(co) == ""
    co.clients.add_revenue(49, email="ana@clinic.example")
    assert rhythm.stage_check(co) == "first_customers" and rhythm.stage_check(co) == ""
    it = next(i for i in c.get("/api/inbox", headers=h).json()["items"] if i["kind"] == "stage")
    assert "first paying customers" in it["title"]
    c.post("/api/business/stage", json={"stage": ""}, headers=h)             # not yet
    assert not [i for i in c.get("/api/inbox", headers=h).json()["items"] if i["kind"] == "stage"]
    assert rhythm.stage_check(co) == "" and co.c.business["stage"] == "launched"
    c.post("/api/business/stage", json={"stage": "first_customers"}, headers=h)
    assert co.c.business["stage"] == "first_customers"


def test_the_owner_sees_each_decision_once_and_answers_it_once(co):
    from agentsorg import decisions, inbox
    p1, p2 = co.projects.create("landing page", status="review"), co.projects.create("emails", status="review")
    co.projects.suggest(p1, "Once the owner gives the domain, swap in full URLs.", "marketing", "lead", 1)
    co.projects.suggest(p2, "Once the owner gives the public domain, replace <DOMAIN> in the drafts.", "sales", "lead", 2)
    co.projects.suggest(p2, "Owner decides the chatbot price and currency, then update the copy.", "sales", "lead", 2)
    gs = decisions.grouped(co)
    assert [g["topic"] for g in gs] == ["domain", "pricing"] and gs[0]["projects"] == [p1, p2]
    items = [i for i in inbox.items(co) if i["kind"] == "decision"]
    assert len(items) == 2 and items[0]["title"] == "Decide: The public domain and sign-up link"
    assert "2 step(s) in 2 project(s)" in items[0]["detail"]
    r = decisions.answer(co, "domain", "marchlight.pro, sign-up at marchlight.pro/start")
    assert r["answered"] == 2 and {s["project"] for s in r["started"]} == {p1, p2}
    assert [g["topic"] for g in decisions.grouped(co)] == ["pricing"]
    assert co.s.one("SELECT COUNT(*) n FROM kb WHERE tags LIKE '%owner%'")["n"] == 1      # remembered once


def test_a_report_about_drafts_never_lands_in_the_outbox(co):
    from agentsorg.agents import outgoing
    assert outgoing("To: ana@clinic.example\nSubject: Hi\n\nHello Ana")
    assert outgoing("Booking that fills itself. Clinics in Leeds: try it free for a month.")
    assert not outgoing("I updated `sales/outbox/drafts/x.md` with Marketing's answer.\n\n**What changed**\n- hold")
    assert not outgoing("Created the drafts.\n\nFiles written: outbox/drafts/physio.md")


def test_the_brief_shows_clients_objectives_and_the_week(co):
    co.c.raw["autonomy"] = "approve"
    co.c.save()
    rhythm.run_week(co)
    co.clients.upsert("ana@clinic.example", name="Ana", stage="contacted", force=True)
    from agentsorg import inbound
    inbound.handle_message(co, "ana@clinic.example", "Re: hi", "interested")
    body = run_reports(co)["_company"]
    assert "**Clients today:**" in body and "Ana replied" in body
    assert "**Objectives:** 10 paying clinics [customers: 0 of 10]" in body
    assert "**This week's plan:** Find 20 clinic leads [proposed]" in body
