"""New departments the CEO agent proposes. Nothing exists until the owner approves it on the dashboard or CLI.

A proposal also holds the tasks the CEO meant for that department; they are queued when it is approved.
"""
from __future__ import annotations

import threading
import time

from . import design

_LOCK = threading.RLock()     # read-modify-write of one settings value

KEY = "dept_proposals"


class Proposals:
    def __init__(self, org):
        self.org = org

    def all(self) -> list[dict]:
        return list(self.org.s.get(KEY, []))

    def _save(self, items: list[dict]) -> None:
        self.org.s.set(KEY, items)

    def get(self, pid: str) -> dict:
        for p in self.all():
            if p["id"] == pid:
                return p
        raise KeyError(f"no proposal {pid!r}")

    def add(self, d: dict, why: str = "") -> dict:
        with _LOCK:
            return self._add(d, why)

    def _add(self, d: dict, why: str) -> dict:
        dept = design.clamp_dept(d)
        if dept["id"] in self.org.c.depts:
            raise ValueError(f"department {dept['id']!r} already exists")
        items = [p for p in self.all() if p["id"] != dept["id"]]
        p = {"id": dept["id"], "dept": dept, "why": str(why or "")[:200], "created": time.time(), "tasks": []}
        items.append(p)
        self._save(items[-10:])
        return p

    def hold(self, pid: str, task: dict) -> None:
        """Park a task for a department that doesn't exist yet."""
        with _LOCK:
            self._hold(pid, task)

    def _hold(self, pid: str, task: dict) -> None:
        items = self.all()
        for p in items:
            if p["id"] == pid:
                p["tasks"] = [*p["tasks"], {k: task.get(k) for k in ("task", "member", "tier")}][:8]
        self._save(items)

    def approve(self, pid: str) -> dict:
        with _LOCK:
            return self._approve(pid)

    def _approve(self, pid: str) -> dict:
        p = self.get(pid)
        did = design.add_dept(self.org, p["dept"])
        ids = [self.org.submit(did, str(t.get("task") or ""), member=t.get("member"), tier=t.get("tier"),
                               source="ceo", priority=1) for t in p["tasks"] if t.get("task")]
        self._save([x for x in self.all() if x["id"] != pid])
        self.org.log_decision("department", did, "created", p.get("why", ""))
        return {"dept": did, "tasks": ids}

    def reject(self, pid: str) -> None:
        with _LOCK:
            p = self.get(pid)
            self._save([x for x in self.all() if x["id"] != pid])
        self.org.log_decision("department", pid, "rejected", p.get("why", ""))
