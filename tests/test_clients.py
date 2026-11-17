"""0.7: client records (pipeline, interactions, revenue), and what comes in (mailbox, product webhook)."""
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from test_pipeline import org, until  # noqa: F401  (fixture)


from agentsorg.api import create_app


@pytest.fixture
def api(org):
    return TestClient(create_app(org)), {"X-Token": org.c.token()}


def test_footer_lines_write_the_pipeline(org):
    text = ("Found two.\nDONE: leads found\nLEAD: Dr. Ana Ruiz | ana@clinic.example | Ruiz Dental | Google Maps\n"
            "LEAD: no address here | Some Org\nLEAD: ana@clinic.example | Ana again")
    got = org.clients.ingest(text, "sales", 7)
    assert got == ["lead ana@clinic.example"] and len(org.clients.all()) == 1
    c = org.clients.by_email("ANA@clinic.example")
    assert (c["name"], c["org"], c["source"], c["stage"]) == ("Dr. Ana Ruiz", "Ruiz Dental", "Google Maps", "lead")
    assert c["next_date"] == date.today().isoformat()                      # a first email gets drafted
    org.clients.ingest("CLIENT: ana@clinic.example | qualified | send the proposal Friday", "sales", 8)
    c = org.clients.by_email("ana@clinic.example")
    assert c["stage"] == "qualified" and c["next_step"] == "send the proposal Friday"
    org.clients.ingest("CLIENT: ana@clinic.example | contacted | x", "sales", 9)
    assert org.clients.by_email("ana@clinic.example")["stage"] == "qualified"   # agents never move a contact back
    assert org.metrics.get("leads")["value"] == 1 and org.metrics.get("leads")["source"] == "records"
    assert "PIPELINE" in org.company_facts() and "qualified 1" in org.company_facts()


def test_unverified_records_never_overwrite_trusted_ones(org):
    org.clients.upsert("bo@x.example", name="Bo Real", stage="replied", force=True)
    org.clients.ingest("LEAD: Fake Name | bo@x.example | Evil Org\nCLIENT: bo@x.example | won | paid", "research", 3,
                       trust="untrusted")
    c = org.clients.by_email("bo@x.example")
    assert c["name"] == "Bo Real" and c["stage"] == "replied"
    org.clients.ingest("LEAD: Web Find | web@x.example | Org", "research", 4, trust="untrusted")
    assert "[unverified record]" in org.clients.card(org.clients.by_email("web@x.example"))


def test_sending_from_the_outbox_moves_the_lead_and_sets_a_follow_up(org, api, monkeypatch):
    c, h = api
    org.clients.upsert("ana@clinic.example", name="Ana", source="owner")
    oid = org.s.x("INSERT INTO outbox(created,task_id,dept,body,status) VALUES(1,1,'sales',?,'pending')",
                  ("To: ana@clinic.example\nSubject: Booking page for Ruiz Dental\n\nHello Ana",))
    monkeypatch.setattr("agentsorg.api.outbox_send.send", lambda body, env: "sent via test")
    assert c.post(f"/api/outbox/{oid}/send", headers=h).json()["ok"]
    ct = org.clients.by_email("ana@clinic.example")
    assert ct["stage"] == "contacted" and ct["next_date"] == (date.today() + timedelta(days=4)).isoformat()
    tl = org.clients.timeline(ct["id"])
    assert any(i["kind"] == "email_out" and "Booking page" in i["summary"] and i["outbox_id"] == oid for i in tl)
    oid2 = org.s.x("INSERT INTO outbox(created,task_id,dept,body,status) VALUES(1,1,'sales',?,'pending')",
                   ("To: new@person.example\nSubject: Hi\n\nHello",))
    c.post(f"/api/outbox/{oid2}/send", headers=h)
    assert org.clients.by_email("new@person.example")["stage"] == "contacted"    # a new address becomes a contact


def test_revenue_makes_a_customer_and_the_numbers(org, api):
    c, h = api
    cid = c.post("/api/clients", json={"email": "ana@clinic.example", "name": "Ana"}, headers=h).json()["id"]
    assert c.post(f"/api/clients/{cid}/revenue", json={"amount": "abc"}, headers=h).status_code == 400
    c.post(f"/api/clients/{cid}/revenue", json={"amount": 49, "currency": "eur", "kind": "recurring"}, headers=h)
    c.post(f"/api/clients/{cid}/revenue", json={"amount": 10, "kind": "refund"}, headers=h)
    d = c.get("/api/clients", headers=h).json()
    assert d["counts"]["won"] == 1 and d["numbers"]["revenue_month"] == 39 and d["numbers"]["mrr"] == 49
    assert org.metrics.get("customers")["value"] == 1 and org.metrics.get("revenue_month")["value"] == 39
    r = c.patch(f"/api/clients/{cid}", json={"stage": "churned", "note": "moved city"}, headers=h).json()
    assert r["stage"] == "churned" and any(i["summary"] == "moved city" for i in org.clients.timeline(cid))


def test_csv_import_brings_an_existing_book_of_clients(org, api):
    c, h = api
    csv = ("Name,Email,Company,Status,Value\nAna,ana@clinic.example,Ruiz Dental,Customer,490\n"
           "Bo,BO@x.example,Bo Physio,interested,\nNo Mail,,Org,new,\n")
    r = c.post("/api/clients/import", json={"text": csv}, headers=h).json()
    assert r == {"added": 2, "updated": 0, "skipped": 1}
    assert org.clients.by_email("ana@clinic.example")["stage"] == "won"
    assert org.clients.by_email("bo@x.example")["stage"] == "replied"
    assert c.post("/api/clients/import", json={"text": csv}, headers=h).json()["updated"] == 2
    assert org.clients.import_csv("x@y.example\nz@y.example")["added"] == 2    # a bare list of addresses works


