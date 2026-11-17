"""Budget gate. Autonomous work: below auto_max_percent (default 40%) of the subscription window.
Manual (CEO / dept commands): below manual_max_percent. Fallback: daily call counts."""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .quota import Quota
from .state import State

LIMIT_RE = re.compile(r"session limit|usage limit|limit reached|rate.?limit|hit your .*limit|\b429\b", re.I)
RESET_RE = re.compile(r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?(?:\s*\(([^)]+)\))?", re.I)
LIMIT_WAIT = 1800          # no reset time in the message: try again in 30 minutes


def is_limit(error: str | None) -> bool:
    """Claude refused because the plan's limit is used up (nothing is wrong with the work itself)."""
    return bool(LIMIT_RE.search(error or ""))


def limit_ends(error: str, now: float | None = None) -> float:
    """When the limit lifts, from messages like `resets 4pm (UTC)`."""
    now = now or time.time()
    m = RESET_RE.search(error or "")
    if not m:
        return now + LIMIT_WAIT
    try:
        tz = ZoneInfo(m.group(4)) if m.group(4) else None
    except Exception:
        tz = None
    cur = datetime.fromtimestamp(now, tz)
    h, ampm = int(m.group(1)), (m.group(3) or "").lower()
    if ampm:
        h = h % 12 + (12 if ampm == "pm" else 0)
    if h > 23:
        return now + LIMIT_WAIT
    at = cur.replace(hour=h, minute=int(m.group(2) or 0), second=0, microsecond=0)
    if at <= cur:
        at += timedelta(days=1)
    return at.timestamp() + 60              # a minute of margin past the reset


class Budget:
    def __init__(self, company, state: State, quota: Quota | None = None):
        self.c = company
        self.s = state
        self.quota = quota or Quota()

    def auto_percent(self) -> float:
        return float(self.s.get("auto_max_percent", self.c.budget["auto_max_percent"]))

    def in_quiet_hours(self, now: datetime | None = None) -> bool:
        now = now or datetime.now()
        for span in self.c.budget.get("quiet_hours") or []:   # e.g. ["23:00-07:00"]
            a, b = span.split("-")
            cur = now.strftime("%H:%M")
            if (a <= cur < b) if a < b else (cur >= a or cur < b):
                return True
        return False

    def check(self, auto: bool, scheduled: bool = False) -> tuple[bool, str]:
        """scheduled = a routine the owner set up: runs without auto mode, but under the auto cap."""
        if self.s.get("paused", False):
            return False, "paused (kill switch)"
        until = self.s.get("limit_until") or 0
        if until > time.time():
            return False, f"Claude plan limit reached; back at {datetime.fromtimestamp(until):%H:%M}"
        if auto:
            if not self.s.get("auto", False) and not scheduled:
                return False, "auto mode off"
            if self.in_quiet_hours():
                return False, "quiet hours"
        limit = self.auto_percent() if auto else float(self.c.budget["manual_max_percent"])
        worst = self.quota.worst()
        if worst is not None:
            if worst >= limit:
                return False, f"quota {worst:.0f}% >= {limit:.0f}% {'auto' if auto else 'manual'} cap"
            return True, f"quota {worst:.0f}% < {limit:.0f}%"
        cap = int(self.c.budget["auto_max_calls" if auto else "manual_max_calls"])
        used = self.s.calls_today(auto=True) if auto else self.s.calls_today()
        if used >= cap:
            return False, f"quota unreadable; {used}/{cap} daily calls used"
        return True, f"quota unreadable; {used}/{cap} daily calls"

    def note_limit(self, error: str) -> float:
        """Claude said the limit is used up: no call runs until it lifts."""
        until = limit_ends(error)
        self.s.set("limit_until", until)
        return until

    def snapshot(self) -> dict:
        st = self.quota.status() if hasattr(self.quota, "status") else {"windows": self.quota.utilization()}
        return {
            "windows": st["windows"], "worst": self.quota.worst(),
            "quota_age": st.get("age"), "quota_stale": st.get("stale", False), "quota_reason": st.get("reason", ""),
            "auto_max_percent": self.auto_percent(),
            "heartbeat_minutes": self.c.budget["heartbeat_minutes"],
            "manual_max_percent": self.c.budget["manual_max_percent"],
            "calls_today": self.s.calls_today(), "auto_calls_today": self.s.calls_today(auto=True),
            "auto": bool(self.s.get("auto", False)), "paused": bool(self.s.get("paused", False)),
            "auto_ok": self.check(True), "manual_ok": self.check(False),
        }
