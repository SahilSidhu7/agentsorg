"""Background loop: recover crashes, start queued work, retry what waited for plan usage, run the owner's own
schedules, finish projects, close stale results, daily backup. It never starts work the owner didn't ask for.
Every error is logged and shown to the owner; the loop itself never stops."""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime


log = logging.getLogger("agentsorg")


def parse_hhmm(value: str, default: str = "18:00") -> tuple[int, int]:
    """'9:00', '09:00', '9' -> (9, 0). Bad values fall back to the default."""
    for v in (str(value or "").strip(), default):
        try:
            h, _, m = v.partition(":")
            h, m = int(h), int(m or 0)
            if 0 <= h < 24 and 0 <= m < 60:
                return h, m
        except ValueError:
            continue
    return 18, 0


def past(hhmm: str) -> bool:
    h, m = parse_hhmm(hhmm)
    now = datetime.now()
    return (now.hour, now.minute) >= (h, m)


class Heartbeat:
    def __init__(self, org):
        self.org = org
        self._stop = threading.Event()
        self.last_tick = 0.0
        self.last_msg = "not started"
        self.errors = 0
        self.thread = threading.Thread(target=self._loop, daemon=True, name="heartbeat")

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self._stop.set()

    def tick_now(self) -> str:
        """Kept for the CLI's `tick`: one pass of the loop."""
        self.run_once()
        self.last_tick = time.time()
        self.last_msg = f"{datetime.now():%H:%M} ran once"
        return self.last_msg

    def _step(self, name: str, fn) -> None:
        try:
            fn()
        except Exception as e:                     # one failing step must not stop the others
            self.errors += 1
            log.exception("heartbeat step %s failed", name)
            self.org.s.set("heartbeat_error", {"at": time.time(), "step": name,
                                               "error": f"{type(e).__name__}: {e}"[:300]})

    def run_once(self) -> None:
        org = self.org
        self._step("recover", org.recover)
        self._step("pump", org.pump)
        self._step("retry", org.retry_blocked)
        self._step("routines", org.routines.due)
        self._step("projects", org.projects.sweep)
        self._step("plans", org.projects.retry_waiting)
        self._step("expire", org.projects.expire)
        for name, fn in getattr(org, "daily_jobs", lambda: [])():
            self._step(name, fn)

    def _loop(self) -> None:
        while not self._stop.wait(20):
            self.run_once()
