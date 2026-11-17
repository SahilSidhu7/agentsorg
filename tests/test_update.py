import pytest

from agentsorg import __version__, cli, update

from test_org import org  # noqa: F401  (fixture)


class Resp:
    def __init__(self, body=None, exc=None):
        self.body, self.exc = body, exc

    def raise_for_status(self):
        if self.exc:
            raise self.exc

    def json(self):
        return self.body


@pytest.fixture
def site(monkeypatch):
    """Fake the site's latest.json; record anything that would start the installer."""
    state = {"latest": __version__, "fail": False, "ran": [], "detached": []}

    def get(url, **kw):
        assert url.endswith("/latest.json")
        if state["fail"]:
            raise OSError("offline")
        return Resp({"version": state["latest"]})

    monkeypatch.setattr("httpx.get", get)
    monkeypatch.setattr(update, "run_here", lambda: state["ran"].append(1) or 0)
    monkeypatch.setattr(update, "start_detached", lambda log: state["detached"].append(log) or "started")
    return state


def bump(v):
    a = list(update.parse(v)); a[-1] += 1
    return ".".join(map(str, a))


def test_parse_compares_numerically():
    assert update.parse("0.8.10") > update.parse("0.8.9") > update.parse("v0.8.4")
    assert update.parse("garbage") == ()


def test_check(site):
    assert update.check() == {"current": __version__, "latest": __version__, "newer": False}
    site["latest"] = bump(__version__)
    assert update.check()["newer"] is True
    site["fail"] = True
    r = update.check()
    assert r["newer"] is False and "couldn't reach" in r["error"]
    site["fail"], site["latest"] = False, "not-a-version"
    assert "unreadable" in update.check()["error"]


def test_cli_update(site, capsys):
    cli.main(["update"])
    assert "is the latest version" in capsys.readouterr().out and not site["ran"]
    site["latest"] = bump(__version__)
    cli.main(["update", "--check"])
    assert f"{site['latest']} is out" in capsys.readouterr().out and not site["ran"]
    cli.main(["update"])
    assert site["ran"] == [1]
    site["fail"] = True
    with pytest.raises(SystemExit) as e:
        cli.main(["update"])
    assert e.value.code == 1


def test_installer_command_matches_the_site():
    cmd = " ".join(update.installer_command())
    assert update.SITE in cmd and ("install.ps1" in cmd if update.WINDOWS else "install.sh | sh" in cmd)


def test_dashboard_update(org, site):
    from fastapi.testclient import TestClient
    from agentsorg.api import create_app
    c = TestClient(create_app(org)); h = {"X-Token": org.c.token()}
    assert c.get("/api/state", headers=h).json()["version"] == __version__
    r = c.post("/api/update", headers=h).json()
    assert r["started"] is False and not site["detached"]            # already latest: nothing runs
    site["latest"] = bump(__version__)
    assert c.get("/api/update", headers=h).json()["newer"] is True and not site["detached"]
    r = c.post("/api/update", headers=h).json()
    assert r["started"] is True and site["detached"] == [org.home / "data" / "update.log"]
    assert c.post("/api/update").status_code in (401, 403)            # needs the dashboard token
