"""Production safety: task lifecycle, spending, secrets, file integrity (0.5.0 audit findings)."""
import json
import threading
import time

import pytest
import yaml

from agentsorg import agents as agents_mod
from agentsorg.agents import Org, plan_item
from agentsorg.config import Company, init_home
from agentsorg.envvault import EnvVault, VaultCorrupt
from agentsorg.runtime import RunResult, build_command, deny_path, parse_payload, runtime_env
from agentsorg.services import Services, valid_target


class Q:
    def __init__(self, w=10):
        self.w = w

    def utilization(self, force=False):
        return {"five_hour": {"utilization": self.w, "resets_at": ""}}

    def worst(self):
        return self.w


def ok(model, text="work\nDONE: did it"):
    return RunResult(text=text, ok=True, model=model,
                     usage=[{"model": model, "input": 10, "output": 5, "cache_read": 0, "cache_create": 0, "cost_usd": 0}])


@pytest.fixture
def org(tmp_path, monkeypatch):
    init_home(tmp_path)
    o = Org(tmp_path)
    o.exe = "fake"
    o.budget.quota = Q()
    o.calls, o.reply = [], None

    def fake(exe, prompt, *, model, system, **k):
        o.calls.append({"model": model, "prompt": prompt, **k})
        if o.reply:
            return o.reply(model, prompt, k)
        return ok(model)

    monkeypatch.setattr(agents_mod, "run_claude", fake)
    yield o
    o.close()


def wait_status(org, tid, statuses, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        t = org.s.task(tid)
        if t["status"] in statuses:
            return t
        time.sleep(0.05)
    return org.s.task(tid)


# ---------- lifecycle ----------
def test_claim_is_atomic_across_threads(org):
    tid = org.s.add_task(dept="marketing", member="lead", body="x", source="dept", status="queued")
    wins = []
    barrier = threading.Barrier(8)

    def grab(i):
        barrier.wait()
        if org.s.claim(tid, ("queued",), f"w{i}", 60):
            wins.append(i)
    threads = [threading.Thread(target=grab, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(wins) == 1


def test_cli_org_never_requeues_running_work(org, tmp_path):
    tid = org.s.add_task(dept="marketing", member="lead", body="x", source="dept", status="running",
                         worker="server:1", lease_until=time.time() + 300)
    other = Org(tmp_path)                                         # e.g. `agentsorg usage` while the server runs
    assert org.s.task(tid)["status"] == "running"
    other.recover()                                               # even a recover respects the live lease
    assert org.s.task(tid)["status"] == "running"
    other.close()


def test_cancel_kills_and_wins(org):
    started = threading.Event()
    release = threading.Event()

    class P:
        killed = False

        def poll(self):
            return None if not self.killed else 1

    proc = P()

    def slow(model, prompt, k):
        k["on_start"](proc)
        started.set()
        release.wait(5)
        return RunResult(text="", ok=False, model=model, error="stopped")
    org.reply = slow
    killed = []
    import agentsorg.agents as am
    am_kill = am.kill_tree
    am.kill_tree = lambda p: (killed.append(p), setattr(p, "killed", True))
    try:
        dep_parent = org.submit("marketing", "long job")
        child = org.submit("sales", "uses it", depends_on=dep_parent)
        assert started.wait(5)
        assert org.cancel(dep_parent)
        release.set()
        t = wait_status(org, dep_parent, ("cancelled",))
        assert t["status"] == "cancelled" and killed
        assert org.s.task(child)["status"] == "cancelled" and "cancelled" in org.s.task(child)["error"]
    finally:
        am.kill_tree = am_kill


def test_crash_after_success_fails_dependents(org, monkeypatch):
    monkeypatch.setattr(org.files, "save_output", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    a = org.submit("marketing", "first")
    b = org.submit("sales", "second", depends_on=a)
    assert wait_status(org, a, ("failed",))["status"] == "failed"
    assert wait_status(org, b, ("failed",))["status"] == "failed"


# ---------- spending ----------
@pytest.mark.parametrize("err,status", [("rate limit reached", "queued_blocked"), ("timeout", "failed"),
                                        ("Claude usage limit reached", "queued_blocked"), ("auth failed", "failed")])
def test_errors_never_escalate(org, err, status):
    org.reply = lambda m, p, k: RunResult(text="", ok=False, model=m, error=err)
    t = org.wait([org.submit("research", "anything at all", member="analyst")])[0]
    assert t["status"] == status and len(org.calls) == 1
    assert not any(c["model"] == "opus" for c in org.calls)


def test_generic_error_retries_once_same_tier(org):
    org.reply = lambda m, p, k: RunResult(text="", ok=False, model=m, error="unparseable output")
    t = org.wait([org.submit("marketing", "x y z")])[0]
    assert t["status"] == "failed" and len(org.calls) == 2 and len({c["model"] for c in org.calls}) == 1


@pytest.mark.parametrize("reply", [
    '{"tasks":[{"dept":["x"],"task":"a"},{"dept":"marketing","member":5,"tier":"2","task":"b"}],'
    '"actions":[{"action":"propose_dept","id":"x","members":{"a":1}}]}',
    "not json at all",
    '{"tasks":[{"dept":"nope","task":"c"}]}',
])
def test_malformed_ceo_reply_never_leaves_parent_running(org, reply):
    org.reply = lambda m, p, k: ok(m, reply) if "COMMAND:" in p else ok(m)
    r = org.ceo("do the thing")
    parent = org.s.one("SELECT * FROM tasks WHERE dept='ceo' ORDER BY id DESC")
    assert parent["status"] in ("done", "failed")
    assert not org.s.q("SELECT id FROM tasks WHERE status IN ('running','routing') AND dept='ceo'")
    assert "error" in r or isinstance(r["tasks"], list)


def test_failed_planner_creates_no_work(org):
    org.reply = lambda m, p, k: RunResult(text="", ok=False, model=m, error="usage limit reached")
    r = org.ceo("plan the launch across teams")
    assert r["tasks"] == [] and "starts by itself" in r["held"]               # a limit: saved, not lost
    assert org.projects.get(r["project"])["status"] == "waiting"
    assert not org.s.q("SELECT id FROM tasks WHERE dept!='ceo'")
    org.reply = lambda m, p, k: RunResult(text="", ok=False, model=m, error="something broke")
    org.s.set("limit_until", 0)
    r = org.ceo("plan the launch across more teams")
    assert r["tasks"] == [] and "couldn't run" in r["error"] and org.projects.get(r["project"])["status"] == "failed"


def test_plan_item_coercion():
    assert plan_item({"dept": "a", "task": "x", "tier": "sonnet", "after": "1", "member": 3}) == \
        {"dept": "a", "member": None, "tier": 2, "task": "x", "after": 1}
    assert plan_item({"dept": "a"}) is None and plan_item("x") is None


# ---------- secrets and isolation ----------
def test_written_files_are_redacted(org, tmp_path):
    org.env.set("STRIPE_KEY", "sk_live_supersecret123", depts=["engineering"])

    def writes(model, prompt, k):
        (k["cwd"] / "notes.md").write_text("key is sk_live_supersecret123")
        return ok(model, "short\nDONE: wrote notes")
    org.reply = writes
    t = org.wait([org.submit("engineering", "write notes")])[0]
    assert "sk_live_supersecret123" not in t["result"] and "[STRIPE_KEY]" in t["result"]


def test_every_agent_call_denies_secret_paths(org):
    org.wait([org.submit("engineering", "fix the site files")])
    secrets = org.calls[-1]["secrets"]
    assert any(s.endswith("data") for s in secrets) and any(".claude" in s for s in secrets)
    cmd = build_command("claude", "haiku", "sys", ["Read"], 4, secrets=secrets)
    deny = cmd[cmd.index("--disallowedTools") + 1:]
    assert any(r.startswith("Read(//") and "data" in r for r in deny)


def test_windows_deny_paths_cover_both_forms():
    rules = deny_path(r"C:\Projects\site")
    assert "Edit(//c/Projects/site/**)" in rules and "Edit(//C:/Projects/site/**)" in rules


def test_shell_runs_in_sandbox_when_available():
    cmd = build_command("claude", "sonnet", "s", ["Bash"], 4, sandbox=True)
    assert json.loads(cmd[cmd.index("--settings") + 1])["sandbox"]["enabled"] is True
    assert "--settings" not in build_command("claude", "sonnet", "s", ["Read"], 4, sandbox=True)


def test_agent_env_is_an_allowlist(monkeypatch):
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "nope")
    monkeypatch.setenv("CLAUDECODE", "1")
    env = runtime_env("/tmp", {"MY_KEY": "v"})
    assert "AWS_SECRET_ACCESS_KEY" not in env and "CLAUDECODE" not in env and env["MY_KEY"] == "v" and "PATH" in env


def test_result_found_before_trailing_warning():
    raw = '{"type":"result","result":"hi","usage":{}}\nWarning: something\n'
    assert parse_payload(raw, "haiku").ok


def test_service_names_cannot_inject_sudoers(tmp_path):
    init_home(tmp_path)
    svc = Services(Company(tmp_path))
    for bad in ["x.service, ALL", "a b", "-x", "*", "x;rm", "../etc"]:
        assert not valid_target(bad)
        with pytest.raises(ValueError):
            svc.add("bad", unit=bad)
    svc.add("api", unit="booking-api")
    text, _ = svc.permissions()
    assert "booking-api.service" in text


def test_member_ids_are_checked(org):
    from fastapi.testclient import TestClient
    from agentsorg.api import create_app
    c = TestClient(create_app(org)); h = {"X-Token": org.c.token()}
    assert c.post("/api/dept/../members", json={"id": "x", "name": "x", "role": "r"}, headers=h).status_code in (404, 405)
    assert c.post("/api/dept/nope/members", json={"id": "x", "name": "x", "role": "r"}, headers=h).status_code == 404
    r = c.post("/api/dept/marketing/members", json={"id": "<img src=x>", "name": "Zed", "role": "r"}, headers=h).json()
    assert r["id"] == "img_src_x"
    assert c.delete("/api/dept/nope/members/a", headers=h).status_code == 404


def test_folder_links_refuse_secrets_and_company(org):
    import pathlib
    with pytest.raises(ValueError):
        org.files.add("home", "marketing", f"this-pc {pathlib.Path.home()}")
    with pytest.raises(ValueError):
        org.files.add("data", "marketing", f"this-pc {org.home / 'data'}")


def test_outbox_cannot_send_twice(org, monkeypatch):
    from fastapi.testclient import TestClient
    from agentsorg import outbox_send
    from agentsorg.api import create_app
    c = TestClient(create_app(org)); h = {"X-Token": org.c.token()}
    org.s.x("INSERT INTO outbox(created,task_id,dept,body,status) VALUES(?,?,?,?,?)",
            (time.time(), 1, "sales", "To: a@b.co\nSubject: Hi\n\nHello", "approved"))
    sent = []
    gate = threading.Event()

    def slow_send(body, env):
        sent.append(1)
        gate.wait(3)
        return "sent"
    monkeypatch.setattr(outbox_send, "send", slow_send)
    results = []
    th = [threading.Thread(target=lambda: results.append(c.post("/api/outbox/1/send", headers=h).status_code))
          for _ in range(2)]
    [t.start() for t in th]
    time.sleep(0.5)
    gate.set()
    [t.join() for t in th]
    assert len(sent) == 1 and sorted(results) == [200, 409]


def test_outbox_network_error_puts_it_back(org, monkeypatch):
    from fastapi.testclient import TestClient
    from agentsorg import outbox_send
    from agentsorg.api import create_app
    c = TestClient(create_app(org)); h = {"X-Token": org.c.token()}
    org.s.x("INSERT INTO outbox(created,task_id,dept,body,status) VALUES(?,?,?,?,?)",
            (time.time(), 1, "sales", "To: a@b.co\nSubject: Hi\n\nHello", "approved"))
    monkeypatch.setattr(outbox_send, "send", lambda b, e: (_ for _ in ()).throw(ConnectionError("down")))
    assert c.post("/api/outbox/1/send", headers=h).status_code == 502
    row = org.s.one("SELECT status, note FROM outbox WHERE id=1")
    assert row["status"] == "approved" and "not sent" in row["note"]


def test_every_api_route_needs_the_token(org):
    from fastapi.testclient import TestClient
    from agentsorg.api import create_app
    app = create_app(org)
    c = TestClient(app)
    checked = 0
    for r in app.routes:
        path = getattr(r, "path", "")
        if not path.startswith("/api/"):
            continue
        url = path.replace("{", "").replace("}", "")
        for m in getattr(r, "methods", []) - {"HEAD", "OPTIONS"}:
            assert c.request(m, url, headers={"X-Token": "wrong"}).status_code == 401, (m, path)
            checked += 1
    assert checked > 50
    assert c.get("/api/state?token=%C3%A9").status_code == 401           # non-ASCII token: 401, not a crash


# ---------- file integrity ----------
def test_company_yaml_merges_concurrent_edits(tmp_path):
    init_home(tmp_path)
    a, b = Company(tmp_path), Company(tmp_path)
    a.raw["goals"] = ["10 clinics"]
    a.save()
    time.sleep(0.02)
    b.raw["report_time"] = "09:00"                     # b never saw a's change
    b.save()
    final = yaml.safe_load((tmp_path / "company.yaml").read_text())
    assert final["goals"] == ["10 clinics"] and final["report_time"] == "09:00"
    assert (tmp_path / "company.yaml.bak").exists()


def test_broken_yaml_falls_back_to_backup(tmp_path):
    init_home(tmp_path)
    c = Company(tmp_path)
    c.raw["goals"] = ["keep me"]
    c.save()
    c.save()                                           # .bak now holds a good copy
    (tmp_path / "company.yaml").write_text("goals: [unclosed\n  - : :")
    c2 = Company(tmp_path)
    assert c2.goals == ["keep me"] and c2.load_error


def test_corrupt_vault_is_never_overwritten(tmp_path):
    v = EnvVault(tmp_path)
    v.set("A_KEY", "value-123456")
    (tmp_path / "data" / "env.json").write_text("{not json")
    with pytest.raises(VaultCorrupt):
        v.set("B_KEY", "other-123456")
    assert (tmp_path / "data" / "env.json").read_text() == "{not json"


def test_heartbeat_errors_are_saved_and_loop_continues(org, monkeypatch):
    from agentsorg.heartbeat import Heartbeat, parse_hhmm
    hb = Heartbeat(org)
    monkeypatch.setattr(org.routines, "due", lambda: 1 / 0)
    hb.run_once()
    err = org.s.get("heartbeat_error")
    assert err["step"] == "routines" and "ZeroDivisionError" in err["error"]
    assert parse_hhmm("9:00") == (9, 0) and parse_hhmm("bad") == (18, 0)


def test_daily_backup_runs_once(org):
    from agentsorg import maintenance
    jobs = maintenance.daily_jobs(org)
    assert [n for n, _ in jobs] == ["backup"] and maintenance.daily_jobs(org) == []
    jobs[0][1]()
    assert list((org.home / "data" / "backups").glob("org-*.db"))


def test_migrations_are_idempotent(org):
    assert org.s.migrate() == org.s.migrate() >= 1


def test_running_out_of_steps_is_named_and_not_retried(org):
    raw = '{"type":"result","subtype":"error_max_turns","is_error":true,"usage":{}}'
    r = parse_payload(raw, "sonnet")
    assert not r.ok and "max_turns" in r.error
    assert not parse_payload('{"type":"result","subtype":"error_max_turns","is_error":false,"usage":{}}', "sonnet").ok
    org.reply = lambda m, p, k: RunResult(text="", ok=False, model=m, error=r.error)
    t = org.wait([org.submit("engineering", "build the whole thing")])[0]
    assert t["status"] == "failed" and len(org.calls) == 2 and org.calls[0]["max_turns"] >= 30   # edit tools: room to build
    assert org.calls[1]["max_turns"] == 60                    # one retry with double the room, then it stops
