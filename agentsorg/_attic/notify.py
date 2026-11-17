"""Messages to the owner (never to anyone else): the daily digest, the weekly review, urgent problems.

Off until the owner turns them on (Settings, or company.yaml `notify:`). Channels:
  email    OWNER_EMAIL + the Outbox's email channel (RESEND_API_KEY + MAIL_FROM, or SMTP_*)
  webhook  NOTIFY_WEBHOOK: a Slack or Discord incoming-webhook URL, or an ntfy.sh topic URL
These keys are owner-only by default: no agent can read them.
"""
from __future__ import annotations

import logging
import time

import httpx

from . import outbox_send

log = logging.getLogger("agentsorg")
URGENT_GAP = 6 * 3600          # the same urgent message at most every 6 hours


class Notify:
    def __init__(self, org):
        self.org = org

    def settings(self) -> dict:
        return {"email": False, "webhook": False, "daily_digest": True, "weekly_review": True, "urgent": True,
                **(self.org.c.raw.get("notify") or {})}

    def channels(self) -> dict:
        env = self.org.env.values()
        st = self.settings()
        return {"email": bool(st["email"] and env.get("OWNER_EMAIL") and outbox_send.channel(env)),
                "webhook": bool(st["webhook"] and env.get("NOTIFY_WEBHOOK", "").startswith("https://"))}

    def send(self, subject: str, body: str) -> list[str]:
        """Deliver on every configured channel. Returns what was used; never raises."""
        env, used = self.org.env.values(), []
        ch = self.channels()
        if ch["email"]:
            try:
                outbox_send.send(f"To: {env['OWNER_EMAIL']}\nSubject: [{self.org.c.name}] {subject}\n\n{body}", env)
                used.append("email")
            except Exception as e:
                log.warning("owner email failed: %s", self.org.env.redact(str(e)))
        if ch["webhook"]:
            url = env["NOTIFY_WEBHOOK"]
            text = f"*{self.org.c.name}: {subject}*\n{body}"[:3500]
            try:
                if "ntfy" in url:
                    r = httpx.post(url, content=text.encode(), headers={"Title": subject[:200]}, timeout=15)
                else:                                # Slack reads "text", Discord reads "content"
                    r = httpx.post(url, json={"text": text, "content": text[:1990]}, timeout=15)
                if r.status_code < 300:
                    used.append("webhook")
                else:
                    log.warning("owner webhook refused: HTTP %s", r.status_code)
            except httpx.HTTPError as e:
                log.warning("owner webhook failed: %s", type(e).__name__)
        if used:
            self.org.s.set("last_notify", {"at": time.time(), "subject": subject[:200], "via": used})
        return used

    def urgent(self, subject: str, body: str) -> list[str]:
        if not self.settings()["urgent"] or not any(self.channels().values()):
            return []
        key = "notify_urgent:" + subject[:80]
        last = self.org.s.get(key) or 0
        if time.time() - last < URGENT_GAP:
            return []
        self.org.s.set(key, time.time())
        return self.send(subject, body)

    def test(self) -> list[str]:
        return self.send("Test notification", "If you can read this, notifications work.")

    # ---------- periodic ----------
    def jobs(self) -> list:
        from .heartbeat import past
        from .maintenance import once
        out = []
        if not any(self.channels().values()):
            return out
        st, s = self.settings(), self.org.s
        today = time.strftime("%Y-%m-%d")
        if st["daily_digest"] and s.get("last_report_day") == today and past(self.org.c.report_time) \
                and once(self.org, "last_digest_day"):
            out.append(("daily digest", self.daily))
        return out

    def daily(self) -> None:
        r = self.org.s.one("SELECT body FROM reports WHERE day=? AND dept='_company'", (time.strftime("%Y-%m-%d"),))
        if r:
            self.send("Daily report", r["body"].replace("**", ""))
