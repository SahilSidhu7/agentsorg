"""Routines: work the company repeats on a schedule ("every Monday 09:00, Sales: pipeline review").

Stored in company.yaml under `routines:`; the time each one last ran lives in the database. The
heartbeat submits a due routine as an ordinary task (source "routine"). Routines run under the
autonomous budget cap: you scheduled them, so they run by themselves and
nothing else, but they never exceed the cap, quiet hours or the pause switch.

Schedules:  hourly · daily 09:00 · weekdays 09:00 · mon 09:00 (any weekday, or several: mon,thu 09:00)
"""
from __future__ import annotations

import re
import secrets
import time
from datetime import datetime, timedelta

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
SCHED_RE = re.compile(r"^(hourly|daily|weekdays|(?:mon|tue|wed|thu|fri|sat|sun)(?:,(?:mon|tue|wed|thu|fri|sat|sun))*)"
                      r"(?:\s+([01]?\d|2[0-3]):([0-5]\d))?$")


def parse(every: str) -> tuple[str, list[int], int, int]:
    """'mon,thu 09:30' -> ('days', [0, 3], 9, 30). Raises ValueError with a readable message."""
    m = SCHED_RE.match((every or "").strip().lower())
    if not m:
        raise ValueError("schedule must look like: hourly · daily 09:00 · weekdays 09:00 · mon 09:00 · mon,thu 17:30")
    kind, h, mi = m.group(1), int(m.group(2) or 9), int(m.group(3) or 0)
    if kind == "hourly":
        return "hourly", [], 0, int(m.group(3) or 0)
    days = list(range(7)) if kind == "daily" else list(range(5)) if kind == "weekdays" \
        else [DAYS.index(d) for d in kind.split(",")]
    return "days", days, h, mi


def last_slot(every: str, now: datetime) -> datetime:
    """The most recent scheduled moment at or before now."""
    kind, days, h, mi = parse(every)
    if kind == "hourly":
        slot = now.replace(minute=mi, second=0, microsecond=0)
        return slot if slot <= now else slot - timedelta(hours=1)
    for back in range(8):
        d = now - timedelta(days=back)
        slot = d.replace(hour=h, minute=mi, second=0, microsecond=0)
        if d.weekday() in days and slot <= now:
            return slot
    return now - timedelta(days=8)


def next_slot(every: str, now: datetime) -> datetime:
    kind, days, h, mi = parse(every)
    if kind == "hourly":
        return last_slot(every, now) + timedelta(hours=1)
    for ahead in range(8):
        d = now + timedelta(days=ahead)
        slot = d.replace(hour=h, minute=mi, second=0, microsecond=0)
        if d.weekday() in days and slot > now:
            return slot
    return now + timedelta(days=7)


class Routines:
    def __init__(self, org):
        self.org = org

    @property
    def c(self):
        return self.org.c

    def all(self) -> list[dict]:
        return [dict(r) for r in (self.c.raw.get("routines") or [])]

    def get(self, rid: str) -> dict:
        for r in self.all():
            if r["id"] == rid:
                return r
        raise KeyError(f"no routine {rid!r}")

    def add(self, dept: str, text: str, every: str, member: str = "", enabled: bool = True) -> dict:
        if dept not in self.c.depts:
            raise ValueError(f"unknown department {dept!r}")
        if not text.strip():
            raise ValueError("a routine needs a task")
        parse(every)
        r = {"id": secrets.token_hex(3), "dept": dept, "member": member or "", "text": text.strip()[:2000],
             "every": every.strip().lower(), "enabled": bool(enabled)}
        self.c.raw.setdefault("routines", []).append(r)
        self.c.save()
        self.org.s.set(f"routine_last:{r['id']}", time.time())      # first run at the next slot, not right now
        return r

    def update(self, rid: str, **kw) -> dict:
        rs = self.c.raw.get("routines") or []
        for r in rs:
            if r["id"] == rid:
                if "every" in kw and kw["every"] is not None:
                    parse(kw["every"])
                if kw.get("dept") and kw["dept"] not in self.c.depts:
                    raise ValueError(f"unknown department {kw['dept']!r}")
                r.update({k: v for k, v in kw.items() if v is not None and k in ("dept", "member", "text", "every", "enabled")})
                self.c.save()
                return dict(r)
        raise KeyError(f"no routine {rid!r}")

    def remove(self, rid: str) -> bool:
        rs = self.c.raw.get("routines") or []
        keep = [r for r in rs if r["id"] != rid]
        if len(keep) == len(rs):
            return False
        self.c.raw["routines"] = keep
        self.c.save()
        return True

    def run_now(self, rid: str) -> int:
        r = self.get(rid)
        self.org.s.set(f"routine_last:{rid}", time.time())
        return self.org.submit(r["dept"], r["text"], member=r["member"] or None, source="routine", priority=4)

    def overview(self, now: datetime | None = None) -> list[dict]:
        now = now or datetime.now()
        out = []
        for r in self.all():
            last = float(self.org.s.get(f"routine_last:{r['id']}", 0) or 0)
            t = self.org.s.one("SELECT id, status, done_line FROM tasks WHERE source='routine' AND dept=? AND body=? "
                               "ORDER BY id DESC LIMIT 1", (r["dept"], r["text"]))
            out.append({**r, "last_run": last, "next": next_slot(r["every"], now).strftime("%a %d %b %H:%M"),
                        "last_task": t})
        return out

    def due(self, now: datetime | None = None) -> list[int]:
        """Submit every enabled routine whose slot passed since it last ran. Returns the new task ids."""
        now = now or datetime.now()
        ids = []
        for r in self.all():
            if not r.get("enabled", True) or r["dept"] not in self.c.depts:
                continue
            try:
                slot = last_slot(r["every"], now).timestamp()
            except ValueError:
                continue
            last = float(self.org.s.get(f"routine_last:{r['id']}", 0) or 0)
            if last < slot:
                ids.append(self.run_now(r["id"]))
        return ids
