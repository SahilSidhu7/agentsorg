"""0.8 Lite: the owner sees at most a few things, nothing runs that they didn't ask for, nothing piles up."""
import json
import time

import pytest

from agentsorg import agents as agents_mod
from agentsorg import inbox
from agentsorg.agents import Org
from agentsorg.config import init_home
from agentsorg.heartbeat import Heartbeat
from agentsorg.runtime import RunResult

from test_org import FakeQuota


@pytest.fixture
def org(tmp_path, monkeypatch):
    init_home(tmp_path)
    o = Org(tmp_path)
    o.exe = "fake-claude"
    o.budget.quota = FakeQuota(10)
    o.reply = "the work\nDONE: did it\nNEXT: write a blog post about it\nASK: research: what do rivals charge?"
    o.calls = []

    def fake_run(exe, prompt, *, model, system, **_):
        o.calls.append(prompt)
        text = (json.dumps({"answer": "", "tasks": [{"dept": "marketing", "task": f"step {i}"} for i in range(6)]})
                if "COMMAND:" in prompt else o.reply)
        return RunResult(text=text, ok=True, model=model, usage=[{"model": model, "input": 1, "output": 1,
                                                                  "cache_read": 0, "cache_create": 0, "cost_usd": 0}])
    monkeypatch.setattr(agents_mod, "run_claude", fake_run)
    yield o
    o.close()


def settle(org, ids, timeout=10):
    org.wait(ids, timeout)
    end = time.time() + timeout
    while time.time() < end and org.s.one("SELECT COUNT(*) n FROM tasks WHERE status IN ('queued','running','waiting')")["n"]:
        time.sleep(0.05)


def test_a_plan_is_at_most_three_steps_and_runs_without_approval(org):
    r = org.ceo("Plan a launch and write everything for it")
    assert len(r["tasks"]) == 3 and "held" not in r
    assert org.projects.get(r["project"])["status"] in ("running", "combining", "review")


def test_next_and_ask_lines_start_no_work(org):
    tid = org.submit("marketing", "write a note", owner=True)
    settle(org, [tid])
    n = org.s.one("SELECT COUNT(*) n FROM tasks WHERE dept!='ceo'")["n"]
    Heartbeat(org).run_once()
    settle(org, [])
    assert org.s.one("SELECT COUNT(*) n FROM tasks WHERE dept!='ceo'")["n"] == n == 1
    assert not org.s.q("SELECT id FROM tasks WHERE status='backlog'")


def test_question_line_reaches_the_owner_once_and_the_answer_is_reused(org):
    org.reply = "draft with a placeholder price\nDONE: draft\nQUESTION: What price do we charge per month?"
    pid = org.projects.create("write the pricing page", status="running")
    tid = org.submit("marketing", "write the pricing page", project_id=pid, owner=True)
    settle(org, [tid])
    qs = [i for i in inbox.all_items(org) if i["kind"] == "question"]
    assert len(qs) == 1 and "price" in qs[0]["title"].lower()
    from agentsorg import decisions
    decisions.answer(org, qs[0]["id"], "29 EUR a month")
    settle(org, [])
    assert not [i for i in inbox.all_items(org) if i["kind"] == "question"]
    pid2 = org.projects.create("write the sales email", status="running")
    org.projects.suggest(pid2, "What price do we charge per month?", "marketing", "lead", tid)
    assert org.projects.suggestions(pid2)[0]["state"] == "answered"          # applied, not asked again
    assert not [i for i in inbox.all_items(org) if i["kind"] == "question"]


def test_needs_you_has_four_kinds_and_five_items_at_most(org):
    for i in range(8):
        org.projects.create(f"result {i}", status="review")
    org.s.x("INSERT INTO outbox(created,dept,body,status) VALUES(?,?,?,?)", (time.time(), "sales", "To: a@b.c\n\nhi", "pending"))
    items = inbox.items(org)
    assert len(items) == inbox.MAX_ITEMS
    assert {i["kind"] for i in inbox.all_items(org)} <= {"result", "send", "question", "problem"}
    assert len(inbox.all_items(org)) == 9


def test_unopened_results_close_by_themselves(org):
    pid = org.projects.create("old result", status="review")
    org.s.x("UPDATE projects SET updated=? WHERE id=?", (time.time() - 8 * 86400, pid))
    fresh = org.projects.create("new result", status="review")
    assert org.projects.expire() == 1
    assert org.projects.get(pid)["status"] == "done" and org.projects.get(fresh)["status"] == "review"


def test_accept_and_discard(org):
    a = org.projects.create("a", status="review")
    b = org.projects.create("b", status="review")
    assert org.projects.accept(a) == [] and org.projects.get(a)["status"] == "done"
    org.projects.discard(b)
    assert org.projects.get(b)["status"] == "cancelled"
    assert not [i for i in inbox.all_items(org) if i["kind"] == "result"]


def test_every_dashboard_page_loads(org):
    from fastapi.testclient import TestClient
    from agentsorg.api import create_app
    c = TestClient(create_app(org))
    h = {"X-Token": org.c.token()}
    did = next(iter(org.c.depts))
    for path in ["/api/state", "/api/inbox", "/api/projects?limit=12", "/api/goals", "/api/routines", "/api/business",
                 "/api/env", "/api/company/settings", "/api/context", "/api/usage", "/api/activity?hours=72", "/api/kb",
                 "/api/files", "/api/services", "/api/clients", "/api/outbox?view=pending", "/api/office",
                 f"/api/tasks?dept={did}&limit=40", "/api/deliverables"]:
        assert c.get(path, headers=h).status_code == 200, path


def test_bare_command_shows_the_company_without_crashing(tmp_path, capsys):
    from agentsorg import cli
    init_home(tmp_path)
    cli.main(["--home", str(tmp_path)])                 # stdin isn't a terminal here, so it prints and returns
    assert "dashboard: http://127.0.0.1:" in capsys.readouterr().out


def test_ask_answers_from_what_the_company_knows_and_starts_nothing(org):
    from agentsorg import ask
    org.clients.upsert("rito@trippink.in", name="Rito (founder)", org="Trippink Tattoo Studio", stage="replied")
    org.kb.add("Owner: price per month", "49 EUR a month", tags="owner decision")
    org.reply = "To: rito@trippink.in\nSubject: A quick call\n\nHi Rito, our price is 49 EUR a month."
    before = org.s.one("SELECT COUNT(*) n FROM tasks")["n"]
    r = ask.ask(org, "What should I reply to Trippink?", [{"q": "hi", "a": "hello"}])
    sent = org.calls[-1]
    assert "rito@trippink.in" in sent and "49 EUR a month" in sent and "EARLIER IN THIS CHAT" in sent
    assert r["draft"] and r["answer"].startswith("To: rito@trippink.in")
    assert org.s.one("SELECT COUNT(*) n FROM tasks")["n"] == before          # no work started
    oid = ask.to_outbox(org, r["answer"])
    assert org.s.one("SELECT status FROM outbox WHERE id=?", (oid,))["status"] == "pending"   # waits for the owner
    with pytest.raises(ValueError):
        ask.ask(org, "   ")


def test_ask_over_the_api(org):
    from fastapi.testclient import TestClient
    from agentsorg.api import create_app
    c = TestClient(create_app(org))
    h = {"X-Token": org.c.token()}
    org.reply = "Not decided yet: set it on the Company page."
    r = c.post("/api/ask", json={"question": "What do we charge?"}, headers=h).json()
    assert r["answer"].startswith("Not decided") and r["draft"] is False
    assert c.post("/api/ask", json={"question": ""}, headers=h).status_code == 400
    assert c.post("/api/ask/outbox", json={"text": "To: a@b.co\n\nhi"}, headers=h).json()["outbox"]
