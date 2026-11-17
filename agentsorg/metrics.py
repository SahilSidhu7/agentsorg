"""Company numbers (waitlist signups, MRR, reply rate...), with optional targets.

The owner sets them (dashboard, `agentsorg metric`), and agents report one they actually measured with a
footer line `METRIC: <name> = <number>`. Parsed without a model call; every change is logged.
"""
from __future__ import annotations

from datetime import date

import re
import time

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
LINE_RE = re.compile(r"^\s*METRIC:\s*([A-Za-z][\w ]{0,39}?)\s*[=:]\s*(-?[\d,]*\.?\d+)\s*(%|[A-Za-z£$€]{0,6})?\s*$", re.M)


def norm(name: str) -> str:
    n = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")[:40]
    if not NAME_RE.match(n or ""):
        raise ValueError("metric names are short words, e.g. waitlist_signups")
    return n


class Metrics:
    def __init__(self, state):
        self.s = state

    def set(self, name: str, value: float | None = None, *, target: float | None = None, unit: str | None = None,
            source: str = "owner") -> dict:
        name = norm(name)
        cur = self.s.one("SELECT * FROM metrics WHERE name=?", (name,))
        now = time.time()
        if cur is None:
            self.s.x("INSERT INTO metrics(name,value,target,unit,updated,source) VALUES(?,?,?,?,?,?)",
                     (name, value, target, unit or "", now, source))
        else:
            self.s.x("UPDATE metrics SET value=COALESCE(?,value), target=COALESCE(?,target), unit=COALESCE(?,unit), "
                     "updated=?, source=? WHERE name=?", (value, target, unit, now, source, name))
        if value is not None:
            self.s.x("INSERT INTO metric_log(name,value,at,source) VALUES(?,?,?,?)", (name, value, now, source))
        return self.get(name)

    def get(self, name: str) -> dict | None:
        return self.s.one("SELECT * FROM metrics WHERE name=?", (norm(name),))

    def remove(self, name: str) -> bool:
        n = norm(name)
        self.s.x("DELETE FROM metric_log WHERE name=?", (n,))
        return bool(self.s.x("DELETE FROM metrics WHERE name=?", (n,)))

    def all(self) -> list[dict]:
        rows = self.s.q("SELECT * FROM metrics ORDER BY name")
        for r in rows:
            prev = self.s.one("SELECT value FROM metric_log WHERE name=? AND at<? ORDER BY at DESC LIMIT 1",
                              (r["name"], time.time() - 6 * 86400))
            r["week_ago"] = prev["value"] if prev else None
        return rows

    def ingest(self, text: str, source: str) -> list[str]:
        """Agent footer lines `METRIC: name = 12`. Returns what was recorded."""
        got = []
        for m in LINE_RE.finditer(text or ""):
            try:
                v = float(m.group(2).replace(",", ""))
                self.set(m.group(1), v, unit=(m.group(3) or None), source=source)
                got.append(f"{norm(m.group(1))}={v:g}")
            except ValueError:
                continue
        return got

    @staticmethod
    def fmt(r: dict) -> str:
        v = "?" if r["value"] is None else f"{r['value']:g}"
        unit = r.get("unit") or ""
        tgt = f" / {r['target']:g}{unit}" if r.get("target") is not None else ""
        return f"{r['name']}: {v}{unit}{tgt}"

    def facts(self) -> str:
        """Each number with how old it is: a count an agent made last week is history, not today's state."""
        rows = [r for r in self.all() if r["value"] is not None or r.get("target") is not None]
        now = time.time()

        def age(r) -> str:
            if r["value"] is None or r.get("source") == "records" or not r.get("updated"):
                return ""
            days = int((now - r["updated"]) // 86400)
            src = (r.get("source") or "")[:30]
            return (f" (measured {time.strftime('%Y-%m-%d', time.localtime(r['updated']))}"
                    + (f" by {src}" if src else "") + (f", {days} days old: may be out of date" if days >= 3 else "")
                    + ")")
        return ("NUMBERS (counted from client records unless a date says when an agent measured it):\n"
                + "\n".join(f"- {self.fmt(r)}{age(r)}" for r in rows)) if rows else ""


# ---------- objectives ----------
def objectives(org) -> list[dict]:
    """Each objective with where its number stands today."""
    out = []
    for o in org.c.objectives:
        row = dict(o)
        m = org.metrics.get(o["metric"]) if o.get("metric") else None
        row["value"] = m["value"] if m else None
        tgt = o.get("target") if o.get("target") is not None else (m or {}).get("target")
        row["target"] = tgt
        row["percent"] = (round(100 * min(max((row["value"] or 0) / tgt, 0), 1)) if tgt and row["value"] is not None
                          else None)
        if o.get("due"):
            try:
                row["days_left"] = (date.fromisoformat(o["due"]) - date.today()).days
            except ValueError:
                pass
        out.append(row)
    return out


def objective_line(o: dict) -> str:
    s = o["text"]
    if o.get("metric") and o.get("target") is not None:
        s += f" [{o['metric']}: {'?' if o['value'] is None else format(o['value'], 'g')} of {o['target']:g}]"
    if o.get("days_left") is not None:
        s += f" [{o['days_left']} days left]" if o["days_left"] >= 0 else f" [{-o['days_left']} days overdue]"
    if o.get("dept"):
        s += f" (owner: {o['dept']})"
    return s
