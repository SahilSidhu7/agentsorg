import pytest

from agentsorg import services as svc_mod
from agentsorg.agents import Org
from agentsorg.config import init_home


class FakeSystem:
    """Stands in for systemctl / journalctl / sudo on a Linux box."""
    def __init__(self):
        self.units = {"harvey-run.service": ("active", "running", "Harvey sales agent heartbeat",
                                             "/home/franky/harvey/.venv/bin/harvey run"),
                      "nightshift.timer": ("active", "waiting", "Nightshift scheduled runs", "")}
        self.sudo_ok = False
        self.calls = []

    def __call__(self, cmd, timeout=20):
        self.calls.append(cmd)
        if cmd[:2] == ["sudo", "-n"]:
            if not self.sudo_ok:
                return 1, "sudo: a password is required"
            return 0, ""
        if cmd[0] == "systemctl":
            args = [a for a in cmd[1:] if a != "--user"]
            if args[0] == "show":
                st = self.units.get(args[1])
                if not st:
                    return 0, "ActiveState=inactive\nUnitFileState=\nDescription="
                nxt = "Mon 2026-10-27 22:00:00 UTC" if args[1].endswith(".timer") else "n/a"
                return 0, (f"Description={st[2]}\nActiveState={st[0]}\nSubState={st[1]}\nActiveEnterTimestamp=today\n"
                           f"MemoryCurrent=52428800\nUnitFileState=enabled\nNextElapseUSecRealtime={nxt}\n"
                           f"ExecStart={{ path={st[3]} }}\nWorkingDirectory=/home/franky/harvey")
            if args[0] == "list-units":
                return 0, "\n".join(f"{u} loaded active running x" for u in self.units) + "\nsystemd-journald.service a b c"
            if args[0] in ("start", "stop", "restart"):
                return 0, ""
        if cmd[0] == "journalctl":
            return 0, "2026-10-27T09:00:00 franky harvey[1]: sent 3 emails"
        return 1, "unknown"


@pytest.fixture
def org(tmp_path, monkeypatch):
    fake = FakeSystem()
    monkeypatch.setattr(svc_mod, "_run", fake)
    monkeypatch.setattr(svc_mod, "supported", lambda: True)
    monkeypatch.setattr(svc_mod.shutil, "which", lambda name: f"/usr/bin/{name}")
    init_home(tmp_path / "co")
    o = Org(tmp_path / "co")
    o.fake = fake
    yield o
    o.close()


def test_discover_add_status_logs(org):
    found = org.services.discover(["/home/franky/harvey"], ["nightshift"])
    units = {f["unit"] for f in found}
    assert units == {"harvey-run.service", "nightshift.timer"}                      # journald skipped as system noise
    org.services.add("harvey-run", unit="harvey-run", scope="system", dept="sales", about="Cold email")
    org.services.add("nightshift", unit="nightshift.timer", scope="user")
    rows = {r["name"]: r for r in org.services.overview()}
    assert rows["harvey-run"]["state"] == "active" and "50 MB" in rows["harvey-run"]["detail"]
    assert rows["harvey-run"]["unit"] == "harvey-run.service"                       # .service added
    assert rows["nightshift"]["next"].startswith("Mon")
    assert "sent 3 emails" in org.services.logs(org.services.get("harvey-run"))
    facts = org.company_facts()
    assert "SERVICES" in facts and "harvey-run (Cold email): active" in facts         # the CEO can answer "is it up?"


def test_control_needs_permission_for_system_units(org):
    org.services.add("harvey-run", unit="harvey-run.service", scope="system")
    org.services.add("nightshift", unit="nightshift.timer", scope="user")
    s = org.services.get("harvey-run")
    assert not org.services.can_control(s)
    ok, msg = org.services.control(s, "restart")
    assert not ok and "agentsorg services permissions" in msg
    ok, msg = org.services.control(org.services.get("nightshift"), "restart")    # user units: no sudo needed
    assert ok and msg == "restarted nightshift"
    text, install = org.services.permissions()
    assert "NOPASSWD: /usr/bin/systemctl start harvey-run.service" in text and "nightshift" not in text
    assert install.index("visudo -cf") < install.index("install -m 0440")      # validate before installing
    org.fake.sudo_ok = True
    assert org.services.can_control(s) and org.services.control(s, "stop") == (True, "stopped harvey-run")
    assert ["sudo", "-n", "/usr/bin/systemctl", "stop", "harvey-run.service"] in org.fake.calls


def test_services_api(org):
    from fastapi.testclient import TestClient
    from agentsorg.api import create_app
    c = TestClient(create_app(org)); h = {"X-Token": org.c.token()}
    assert c.post("/api/services/discover", json={"paths": ["/home/franky/harvey"]}, headers=h).json()["found"]
    assert c.post("/api/services", json={"name": "harvey-run", "unit": "harvey-run.service"}, headers=h).status_code == 200
    r = c.get("/api/services", headers=h).json()
    assert r["services"][0]["state"] == "active" and r["services"][0]["can_control"] is False
    assert "NOPASSWD" in r["permissions"]["text"] and "sudoers-services" in r["permissions"]["install"]
    assert c.post("/api/services/harvey-run/restart", headers=h).status_code == 409   # explains the permission
    assert "sent 3 emails" in c.get("/api/services/harvey-run/logs", headers=h).json()["text"]
    assert c.patch("/api/services/harvey-run", json={"dept": "sales"}, headers=h).status_code == 200
    assert org.services.get("harvey-run").dept == "sales"
    assert c.delete("/api/services/harvey-run", headers=h).json()["ok"]


def test_discovery_boundaries_timers_and_self(org):
    org.fake.units.update({
        "agentsorg-marchlight-office.service": ("active", "running", "agents.org company", "/home/franky/marchlight-office"),
        "marchlight-outreach.service": ("inactive", "dead", "Send due outreach", "/home/franky/marchlight/sender.py"),
        "marchlight-outreach.timer": ("active", "waiting", "Outreach windows", ""),
        "other.service": ("active", "running", "Uses marchlight-office", "/home/franky/marchlight-office/x"),
    })
    units = {f["unit"] for f in org.services.discover(["/home/franky/marchlight"], ["outreach"])}
    assert "agentsorg-marchlight-office.service" not in units          # never the companies themselves
    assert "other.service" not in units                                 # /home/franky/marchlight ≠ ...marchlight-office
    assert "marchlight-outreach.timer" in units and "marchlight-outreach.service" not in units   # timer covers it


def test_docker_via_permission(org):
    org.services.add("booking-api", kind="docker", container="appointments-api-1", dept="engineering")
    text, _ = org.services.permissions()
    assert "/usr/bin/docker restart appointments-api-1" in text
    assert "/usr/bin/docker logs --tail 200 appointments-api-1" in text and "inspect -f" in text
    st = org.services.status(org.services.get("booking-api"))
    assert st["state"] == "unknown"                                      # no docker access yet: explained, not crashed
