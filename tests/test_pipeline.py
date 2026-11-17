"""CEO pipeline: router (free) -> planner (one stronger call) -> gates -> dispatcher; projects, handoffs, conference."""
import json
import time

import pytest

from agentsorg import agents as agents_mod
from agentsorg.agents import Org
from agentsorg.config import init_home
from agentsorg.runtime import RunResult


class Q:
    def utilization(self, force=False):
        return {"five_hour": {"utilization": 10, "resets_at": ""}}

    def worst(self):
        return 10


def ok(model, text):
    return RunResult(text=text, ok=True, model=model,
                     usage=[{"model": model, "input": 10, "output": 5, "cache_read": 0, "cache_create": 0, "cost_usd": 0}])


PLAN2 = {"answer": "Two teams.", "tasks": [
    {"dept": "marketing", "member": "copy", "tier": 1, "task": "write the headline", "expects": "doc"},
    {"dept": "engineering", "member": "lead", "tier": 2, "task": "put the headline on the site", "after": 0}]}


@pytest.fixture
def org(tmp_path, monkeypatch):
    init_home(tmp_path)
    o = Org(tmp_path)
    o.exe = "fake"
    o.budget.quota = Q()
    o.calls = []
    o.plan = PLAN2
    o.seat = "POSITION: support\nWHY: fine"
    o.work = lambda prompt: "the work\nDONE: did it"

    def fake(exe, prompt, *, model, system, **k):
        o.calls.append({"model": model, "prompt": prompt, "system": system, **k})
        if "COMMAND:" in prompt:
            return ok(model, json.dumps(o.plan))
        if "Chair" in system:
            return ok(model, "## Recommendation\nGo.\nOPTIONS: [\"go as planned\", \"smaller test first\"]\nRECOMMEND: 0"
                      + getattr(o, "chair_tail", ""))
        if "conference room" in system:
            return ok(model, o.seat if isinstance(o.seat, str) else o.seat(system))
        if "project result for the company" in system:
            return ok(model, "COMBINED RESULT")
        return ok(model, o.work(prompt))

    monkeypatch.setattr(agents_mod, "run_claude", fake)
    yield o
    o.close()


def until(fn, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if v := fn():
            return v
        time.sleep(0.05)
    return fn()


def kinds(org):
    return [("plan" if "COMMAND:" in c["prompt"] else "chair" if "Chair" in c["system"] else
             "seat" if "conference room" in c["system"] else "work") for c in org.calls]


# ---------- router ----------
def test_router_routes_for_free(org):
    r = org.ceo("@sales draft a follow-up to Jo")
    assert r["routing"] == "direct" and not [k for k in kinds(org) if k == "plan"]
    r = org.ceo("write a tagline for the spring sale")                  # router mod route: tagline -> marketing
    assert r["routing"] == "direct" and org.s.task(r["tasks"][0])["dept"] == "marketing"
    assert "in progress" in org.ceo("status")["answer"]
    assert "plan" not in kinds(org)


def test_multi_department_work_gets_one_planner_call_on_sonnet(org):
    r = org.ceo("Draft a landing headline and put it on the site")
    assert r["routing"] == "ceo" and len(r["tasks"]) == 2 and r["project"]
    plans = [c for c in org.calls if "COMMAND:" in c["prompt"]]
    assert len(plans) == 1 and plans[0]["model"] == org.c.models[2]


def test_project_runs_combines_revises_accepts(org):
    r = org.ceo("Draft a landing headline and put it on the site")
    pid = r["project"]
    p = until(lambda: (x := org.projects.get(pid)) and x["status"] == "review" and x)
    assert p["result"] == "COMBINED RESULT" and len(p["tasks"]) == 2
    eng = org.s.task(r["tasks"][1])
    assert eng["depends_on"] == r["tasks"][0]
    board = org.projects.board(pid)
    assert "marketing" in board and "engineering" in board
    new = org.projects.revise(pid, "make it shorter")
    t = org.s.task(new)
    assert "REVISION NOTES FROM THE OWNER:\nmake it shorter" in t["body"] and "PREVIOUS VERSION" in t["body"]
    until(lambda: org.projects.get(pid)["status"] == "review" and org.s.task(new)["status"] == "done")
    org.projects.accept(pid)
    assert org.projects.get(pid)["final"] == 1
    assert org.s.one("SELECT choice FROM decisions WHERE kind='deliverable'")["choice"] == "accepted"


def test_handoff_by_reference_for_readers_full_text_for_writers(org):
    org.ceo("Draft a landing headline and put it on the site")
    until(lambda: len([k for k in kinds(org) if k == "work"]) >= 2)
    eng = [c for c in org.calls if "put the headline on the site" in c["prompt"]][0]["prompt"]
    assert "INPUT from marketing" in eng and "Full deliverable:" in eng and "Summary:" in eng


def test_editing_teams_read_other_teams_folders_read_only(org, tmp_path):
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text("<h1>hi</h1>")
    org.files.add("site", "engineering", f"this-pc {site}")
    org.c.raw["departments"]["marketing"]["tools"] = ["Read", "Edit", "Write", "Glob", "Grep"]
    org.c.save()
    org.wait([org.submit("marketing", "write the services copy")])
    call = [c for c in org.calls if "services copy" in c["prompt"]][-1]
    assert str(site.resolve()) in call["add_dirs"] and str(site.resolve()) in call["read_only"]
    assert "READABLE (read-only)" in call["prompt"] and "index.html" in call["prompt"]


# ---------- plan limits: commands wait, they are never lost ----------
def test_limit_holds_the_command_and_resumes_after_reset(org):
    from agentsorg.budget import limit_ends
    from datetime import datetime, timezone
    real_plan = org.plan
    limited = {"on": True}
    import agentsorg.agents as am
    fake = am.run_claude

    def gated(exe, prompt, *, model, system, **k):
        if limited["on"] and "COMMAND:" in prompt:
            org.calls.append({"model": model, "prompt": prompt, "system": system, **k})
            return RunResult(text="", ok=False, model=model, error="You've hit your session limit · resets 4pm (UTC)")
        return fake(exe, prompt, model=model, system=system, **k)
    am.run_claude = gated
    try:
        r = org.ceo("Draft a landing headline and put it on the site")
        pid = r["project"]
        assert org.projects.get(pid)["status"] == "waiting" and "starts by itself" in r["held"]
        assert not org.budget.check(auto=False)[0] and org.s.get("limit_until") > time.time()
        assert org.projects.retry_waiting() == 0                                 # still limited: no call spent
        n = len(org.calls)
        limited["on"] = False
        org.s.set("limit_until", time.time() - 1)
        assert org.projects.retry_waiting() == 1
        p = until(lambda: (x := org.projects.get(pid)) and x["status"] == "review" and x)
        assert p and len(p["tasks"]) >= 2 and len(org.calls) > n
    finally:
        am.run_claude = fake
    org.plan = real_plan
    noon = datetime(2026, 10, 31, 13, 50, tzinfo=timezone.utc).timestamp()
    assert datetime.fromtimestamp(limit_ends("resets 4pm (UTC)", noon), timezone.utc).hour == 16
    assert limit_ends("no time here", noon) == noon + 1800


@pytest.mark.parametrize("msg,hour", [
    ("resets 4pm (UTC)", 16), ("resets 4pm (GMT)", 16),
    ("resets at 9:30pm (UTC+5:30)", 16), ("resets 11am (GMT-05:00)", 16),
])
def test_limit_reset_zone_does_not_depend_on_local_time(msg, hour):
    from datetime import datetime, timezone
    from agentsorg.budget import limit_ends
    noon = datetime(2026, 10, 31, 13, 50, tzinfo=timezone.utc).timestamp()
    at = datetime.fromtimestamp(limit_ends(msg, noon) - 60, timezone.utc)
    assert (at.hour, at.minute) == (hour, 0)


def test_running_task_that_hits_a_limit_resumes(org):
    n = {"c": 0}
    import agentsorg.agents as am
    fake = am.run_claude

    def once(exe, prompt, *, model, system, **k):
        n["c"] += 1
        if n["c"] == 1:
            return RunResult(text="", ok=False, model=model, error="You've hit your session limit · resets 4pm (UTC)")
        return fake(exe, prompt, model=model, system=system, **k)
    am.run_claude = once
    try:
        t = org.submit("marketing", "write a line of copy")
        assert until(lambda: org.s.task(t)["status"] == "queued_blocked")
        org.s.set("limit_until", time.time() - 1)
        org.retry_blocked()
        assert until(lambda: org.s.task(t)["status"] == "done")
    finally:
        am.run_claude = fake


def test_expired_quota_window_counts_as_empty(tmp_path):
    from agentsorg.quota import Quota, _current
    win = {"five_hour": {"utilization": 100.0, "resets_at": "2020-01-01T00:00:00+00:00"},
           "seven_day": {"utilization": 84.0, "resets_at": "2999-01-01T00:00:00+00:00"}}
    cur = _current(win, time.time())
    assert cur["five_hour"]["utilization"] == 0.0 and cur["seven_day"]["utilization"] == 84.0


def test_failed_command_shows_in_inbox_and_retries(org):
    from agentsorg import inbox
    pid = org.projects.create("do a thing", status="failed")
    org.projects.set(pid, error="The planner couldn't run: boom")
    assert [i for i in inbox.items(org) if i["kind"] == "problem" and i.get("project") == pid]
    org.projects.retry(pid)
    assert until(lambda: org.projects.get(pid)["status"] in ("running", "review"))


def test_cancelled_plan_is_not_offered_for_retry(org):
    from fastapi.testclient import TestClient
    from agentsorg import inbox
    from agentsorg.api import create_app
    c = TestClient(create_app(org)); h = {"X-Token": org.c.token()}
    pid = org.projects.create("x", status="running")
    a = org.submit("marketing", "first step", project_id=pid, hold=True)
    b = org.submit("sales", "second step", project_id=pid, depends_on=a, hold=True)
    org.projects.release(pid)
    org.cancel(a)
    assert until(lambda: org.s.task(b)["status"] == "cancelled")
    assert not [i for i in inbox.items(org) if i["kind"] == "failed"]
    org.s.update_task(b, status="failed")
    assert c.post(f"/api/task/{b}/retry", json={"text": ""}, headers=h).status_code == 409   # needs step one first


def test_retry_reopens_the_project(org):
    from fastapi.testclient import TestClient
    from agentsorg.api import create_app
    c = TestClient(create_app(org)); h = {"X-Token": org.c.token()}
    pid = org.projects.create("x", status="review")
    t = org.s.add_task(dept="marketing", member="lead", body="fix it", source="ceo", status="failed", project_id=pid,
                       kind="deliverable")
    assert c.post(f"/api/task/{t}/retry", json={"text": ""}, headers=h).status_code == 200
    assert until(lambda: org.projects.get(pid)["status"] == "review" and org.s.task(t)["status"] == "done")
