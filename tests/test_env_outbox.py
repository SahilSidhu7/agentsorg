import os
import time

from agentsorg import quota as quota_mod
from agentsorg.envvault import EnvVault
from agentsorg.runtime import RunResult

from test_org import org  # noqa: F401  (fixture)


def test_vault_scopes_masks_and_redacts(tmp_path):
    v = EnvVault(tmp_path)
    v.set("resend_api_key", "re_abcdef1234567890", depts=["sales"], note="send email")
    v.set("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-xyzxyzxyz")
    pub = v.public()
    assert "re_abcdef1234567890" not in str(pub) and pub["RESEND_API_KEY"]["masked"].startswith("re_a")
    assert pub["CLAUDE_CODE_OAUTH_TOKEN"]["claude"]
    assert v.env_for("sales", tools=True) == {"RESEND_API_KEY": "re_abcdef1234567890",
                                              "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-xyzxyzxyz"}
    assert v.env_for("marketing", tools=True) == {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-xyzxyzxyz"}
    assert "RESEND_API_KEY" not in v.env_for("sales", tools=False)        # text-only agents can't use keys anyway
    assert v.redact("key is re_abcdef1234567890 ok") == "key is [RESEND_API_KEY] ok"
    v.set("RESEND_API_KEY", note="transactional email")                  # edit without re-entering the value
    assert v.env_for("sales", True)["RESEND_API_KEY"] == "re_abcdef1234567890"
    for bad in ("PATH", "1X", "lower-case!"):
        try:
            v.set(bad, "x" * 8)
            raise AssertionError(bad)
        except ValueError:
            pass
    assert v.import_dotenv("# c\nexport A_KEY=\"one111\"\nB_KEY=two222\nbad line\n") == ["A_KEY", "B_KEY"]
    if os.name != "nt":
        assert (v.path.stat().st_mode & 0o777) == 0o600


def test_leaked_value_is_masked(org, monkeypatch):
    from agentsorg import agents as agents_mod
    org.env.set("API_TOKEN", "tok_leakme_123456")
    monkeypatch.setattr(agents_mod, "run_claude", lambda *a, **k: RunResult(
        text="here: tok_leakme_123456\nDONE: ok", ok=True, model="haiku", usage=[]))
    tid = org.submit("marketing", "anything")
    t = org.wait([tid])[0]
    assert "tok_leakme_123456" not in t["result"] and "[API_TOKEN]" in t["result"]


def test_outbox_stages_and_env_api(org):
    from fastapi.testclient import TestClient
    from agentsorg.api import create_app
    c = TestClient(create_app(org)); h = {"X-Token": org.c.token()}
    oid = org.s.x("INSERT INTO outbox(created,task_id,dept,body,status) VALUES(?,?,?,?,?)",
                  (time.time(), 1, "sales", "To: bob@x.co\nSubject: Hi\n\nHi Bob", "pending"))
    oid = org.s.one("SELECT id FROM outbox")["id"]
    assert c.get("/api/outbox", headers=h).json()["counts"] == {"pending": 1, "approved": 0, "done": 0}
    c.patch(f"/api/outbox/{oid}", json={"body": "To: bob@x.co\nSubject: Hi\n\nHi Bob, edited"}, headers=h)
    c.post(f"/api/outbox/{oid}/approved", headers=h)
    r = c.get("/api/outbox", headers=h).json()
    assert r["items"] == [] and r["counts"]["approved"] == 1                # approved leaves the review list
    assert c.get("/api/state", headers=h).json()["outbox_pending"] == 0
    assert c.get("/api/outbox?view=approved", headers=h).json()["items"][0]["body"].endswith("Hi Bob, edited")
    c.post(f"/api/outbox/{oid}/sent", headers=h)
    assert c.get("/api/outbox?view=done", headers=h).json()["items"][0]["status"] == "sent"
    # a non-email draft (a post) has nothing to send: approving files it under done
    org.s.x("INSERT INTO outbox(created,task_id,dept,body,status) VALUES(?,?,?,?,?)",
            (time.time(), 1, "marketing", "Launch post for LinkedIn", "pending"))
    pid = org.s.one("SELECT MAX(id) id FROM outbox")["id"]
    assert c.post(f"/api/outbox/{pid}/approved", headers=h).json()["status"] == "done"
    assert c.get("/api/outbox", headers=h).json()["counts"] == {"pending": 0, "approved": 0, "done": 2}
    assert c.put("/api/env/MY_KEY", json={"value": "abcdefgh1234", "depts": ["sales"]}, headers=h).status_code == 200
    assert "abcdefgh1234" not in c.get("/api/env", headers=h).text
    assert c.put("/api/env/PATH", json={"value": "x"}, headers=h).status_code == 400
    assert c.delete("/api/env/MY_KEY", headers=h).json()["ok"]


class FakeResp:
    def __init__(self, code, data=None, headers=None):
        self.status_code, self._d, self.headers = code, data or {}, headers or {}

    def json(self):
        return self._d


def test_quota_shared_cache_backoff_and_stale(tmp_path, monkeypatch):
    monkeypatch.setattr(quota_mod, "_oauth", lambda: ("tok", time.time() + 3600))
    hits = []
    resp = [FakeResp(200, {"five_hour": {"utilization": 22.0, "resets_at": "x"}})]
    monkeypatch.setattr(quota_mod.httpx, "get", lambda *a, **k: hits.append(1) or resp[0])
    q = quota_mod.Quota(tmp_path / "q.json")
    assert q.worst() == 22.0
    assert quota_mod.Quota(tmp_path / "q.json").worst() == 22.0 and len(hits) == 1   # other process: from the file
    resp[0] = FakeResp(429, headers={"retry-after": "600"})
    st = q.status(force=True)
    assert st["windows"]["five_hour"]["utilization"] == 22.0 and "rate-limited" in st["reason"]  # last good kept
    q.status(); q.status()
    assert len(hits) == 2                                                    # backing off, no hammering
    monkeypatch.setattr(quota_mod, "_oauth", lambda: ("tok", time.time() - 10))
    q2 = quota_mod.Quota(tmp_path / "other.json")
    assert q2.status()["windows"] is None and "expired" in q2.status()["reason"]


def test_outbox_send_parsing_and_channel(org, monkeypatch):
    from agentsorg import outbox_send as ob
    d = ob.parse_draft("**To:** Jo <jo@clinic.co.uk>\nSubject: Quick question\n\nHi Jo,\nbody")
    assert d["to"] == ["jo@clinic.co.uk"] and d["subject"] == "Quick question" and d["text"].startswith("Hi Jo")
    assert "isn't set up" in ob.check("To: a@b.co\nSubject: s\n\nx", {})[1]
    assert "To:" in ob.check("Hello there", {"SMTP_HOST": "h", "SMTP_USER": "u", "SMTP_PASS": "p"})[1]
    from fastapi.testclient import TestClient
    from agentsorg.api import create_app
    c = TestClient(create_app(org)); h = {"X-Token": org.c.token()}
    org.s.x("INSERT INTO outbox(created,task_id,dept,body,status) VALUES(?,?,?,?,?)",
            (time.time(), 1, "sales", "To: jo@clinic.co.uk\nSubject: Hi\n\nHello", "approved"))
    oid = org.s.one("SELECT id FROM outbox")["id"]
    it = c.get("/api/outbox?view=approved", headers=h).json()["items"][0]
    assert it["send"]["problem"].startswith("email sending isn't set up")
    assert c.post(f"/api/outbox/{oid}/send", headers=h).status_code == 400
    org.env.set("RESEND_API_KEY", "re_test_1234567890")
    org.env.set("MAIL_FROM", "Sasha <sasha@marchlight.co>")
    sent = []

    class R:
        status_code, text = 200, "{}"
    monkeypatch.setattr(ob.httpx, "post", lambda url, **k: sent.append(k["json"]) or R())
    r = c.post(f"/api/outbox/{oid}/send", headers=h).json()
    assert r["ok"] and sent[0]["to"] == ["jo@clinic.co.uk"] and sent[0]["subject"] == "Hi"
    assert org.s.one("SELECT status, note FROM outbox WHERE id=?", (oid,))["status"] == "sent"


def test_mail_keys_are_owner_only(tmp_path):
    v = EnvVault(tmp_path)
    v.set("SMTP_PASS", "app-password-123")
    v.set("OTHER", "other-value-123", depts=["none"])
    assert v.public()["SMTP_PASS"]["depts"] == ["none"]
    assert "SMTP_PASS" not in v.env_for("sales", True) and "OTHER" not in v.env_for(None, True)
    assert v.names_for("sales") == [] and v.values()["SMTP_PASS"] == "app-password-123"


