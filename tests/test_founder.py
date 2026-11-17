"""Founder flow: designer questions + company plan, Inbox, notifications, weekly review, editing the company."""
import time
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from test_pipeline import org  # noqa: F401  (fixture)

from agentsorg import design
from agentsorg.api import create_app


@pytest.fixture
def api(org):
    return TestClient(create_app(org)), {"X-Token": org.c.token()}


def test_designer_asks_only_what_it_doesnt_know():
    qs = design.questions("A booking app for clinics, $49/month subscription, launched with 3 paying clinics")
    assert [q["id"] for q in qs] == ["target", "channels"]
    assert len(design.questions("help me")) == 5
    assert "OWNER'S ANSWERS:\ntarget: 10 clinics by March" in design.with_answers("x", {"target": "10 clinics by March"})


def test_departments_created_edited_removed(org, api):
    c, h = api
    did = c.post("/api/depts", json={"name": "Customer Success", "mission": "keep clinics happy"}, headers=h).json()["id"]
    assert did == "customer_success" and org.c.depts[did].members[0].name == "Lead"
    c.patch(f"/api/depts/{did}", json={"mission": "renewals", "instructions": "Answer within a day."}, headers=h)
    assert org.c.depts[did].mission == "renewals"
    assert c.get(f"/api/depts/{did}/files", headers=h).json()["instructions"].startswith("Answer within a day.")
    assert "Answer within a day." in org.system_prompt(did, "lead")
    r = c.delete(f"/api/depts/{did}", headers=h).json()
    assert did not in org.c.depts and (org.home / r["archived_as"]).is_dir()
    assert c.post("/api/depts", json={"name": "data"}, headers=h).json()["id"] == "data_team"   # reserved name


def test_skip_design_builds_lean_office(org, api):
    c, h = api
    c.post("/api/design/skip", json={}, headers=h)
    assert set(org.c.depts) == {"product", "growth", "ops"} and org.s.get("designed")


def test_outbox_test_goes_only_to_the_owner(org, api, monkeypatch):
    from agentsorg import outbox_send
    c, h = api
    org.s.x("INSERT INTO outbox(created,task_id,dept,body,status) VALUES(?,?,?,?,?)",
            (time.time(), 1, "sales", "To: jo@clinic.co\nSubject: Hi\n\nHello Jo", "pending"))
    assert c.post("/api/outbox/1/test", headers=h).status_code == 400            # no OWNER_EMAIL yet
    org.env.set("OWNER_EMAIL", "me@studio.co")
    sent = []
    monkeypatch.setattr(outbox_send, "send", lambda body, env: sent.append(body) or "sent to me@studio.co")
    assert c.post("/api/outbox/1/test", headers=h).json()["message"]
    assert sent[0].startswith("To: me@studio.co\nSubject: [TEST] Hi") and "would go to: jo@clinic.co" in sent[0]
    assert org.s.one("SELECT status FROM outbox WHERE id=1")["status"] == "pending"   # the real email is untouched


