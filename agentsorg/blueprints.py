"""Department blueprints: the functions every company needs, and when each one becomes its own team.

A real company covers the whole value chain: research -> product -> marketing -> sales -> delivery -> support, with
finance and operations keeping it running. A blueprint (data/blueprints/<function>.yaml) says what a function is
for, who staffs it, the numbers it answers for, its recurring duties, its playbook, and the business stage at which
it deserves its own department. Until then another team carries it.

The org plan is the company's answer for every function: a department has it NOW, or it comes LATER with a trigger
(a stage, or a number). Everything here is deterministic (0 tokens).
"""
from __future__ import annotations

import re
import time
from functools import lru_cache
from pathlib import Path

import yaml

FUNCTIONS = ["research", "product", "marketing", "sales", "delivery", "support", "finance", "operations"]
ESSENTIAL = ("research", "marketing")           # never dropped: at least a named member with duties
STAGES = ["idea", "building", "launched", "first_customers", "growing"]
STAGE_NAMES = {"idea": "idea", "building": "building it", "launched": "launched, no paying customers yet",
               "first_customers": "first paying customers", "growing": "growing"}
ALIASES = {"engineering": "product", "product": "product", "growth": "marketing", "ops": "operations",
           "client_delivery": "delivery", "success": "support", "customer_success": "support"}
PLAN_KEY = "org_plan"


@lru_cache(maxsize=1)
def all() -> dict[str, dict]:      # noqa: A001  (reads like blueprints.all())
    d = Path(__file__).parent / "data" / "blueprints"
    out = {}
    for fn in FUNCTIONS:
        p = d / f"{fn}.yaml"
        if p.exists():
            b = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            b.setdefault("id", fn)
            out[fn] = b
    return out


def summary() -> str:
    """One line per function for the designer prompt."""
    return "\n".join(f"- {b['id']}: {b['mission']} Own team from: {when_text(b.get('starts_at') or {})}."
                     for b in all().values())


def when_text(w: dict) -> str:
    if w.get("metric"):
        return f"{w['metric']} >= {w.get('gte', 1):g}"
    return f"stage \"{STAGE_NAMES.get(w.get('stage'), w.get('stage') or 'idea')}\""


def clean_functions(fs) -> list[str]:
    return list(dict.fromkeys(f for f in (fs or []) if f in FUNCTIONS))


def infer(did: str, name: str = "", mission: str = "") -> list[str]:
    """Which functions a department with no `functions:` covers, from its id, name and mission."""
    did = did.lower()
    if did in FUNCTIONS:
        base = [did]
    elif did in ALIASES:
        base = [ALIASES[did]]
    else:
        base = []
    text = f"{did} {name} {mission}".lower()
    for fn, b in all().items():
        if fn not in base and re.search(rf"\b({b.get('keywords', fn)})", text):
            base.append(fn)
    return base[:4]


def dept_for(company, fn: str):
    """The department that carries a function today (first one that lists it), or None."""
    for d in company.depts.values():
        if fn in d.functions:
            return d
    return None


def guess_dept(company, text: str) -> str | None:
    """Route free text to the department whose function matches it (replaces five hardcoded ids)."""
    t = text.lower()
    for fn in ("product", "marketing", "sales", "support", "delivery", "finance", "operations", "research"):
        if re.search(all()[fn].get("keywords", fn), t) and (d := dept_for(company, fn)):
            return d.id
    return None


def as_dept(fn: str, taken: set[str] = frozenset()) -> dict:
    """A blueprint as a department the designer or a proposal can create."""
    b = all()[fn]
    did = fn if fn not in taken else f"{fn}_team"
    return {"id": did, "name": b["name"], "mission": b["mission"], "tools": list(b.get("tools") or []),
            "functions": [fn], "rules": "", "skill": "", "members": [dict(m) for m in b["members"]]}


# ---------- coverage ----------
def reached(when: dict, stage: str, numbers: dict[str, float]) -> bool:
    if when.get("metric"):
        v = numbers.get(str(when["metric"]))
        return v is not None and v >= float(when.get("gte", 1))
    want = when.get("stage") or "idea"
    return want in STAGES and stage in STAGES and STAGES.index(stage) >= STAGES.index(want)


def cover(depts: list[dict], later: list[dict], stage: str = "idea") -> tuple[list[dict], list[dict]]:
    """Make a design cover every function. A missing essential function (research, marketing) is given to the
    best-fitting department with a named member; any other missing function is scheduled for later with its
    blueprint's trigger. Returns (departments, later)."""
    for d in depts:
        d["functions"] = clean_functions(d.get("functions")) or infer(d["id"], d.get("name", ""), d.get("mission", ""))
    have = {f for d in depts for f in d["functions"]}
    own = {d["functions"][0] for d in depts if d["functions"]}
    later = [x for x in later if x["function"] not in own]      # folded now, own team later: both are kept
    planned = {x["function"] for x in later}
    for fn in FUNCTIONS:
        if fn in have or (fn in planned and fn not in ESSENTIAL):
            continue
        b = all()[fn]
        if fn in ESSENTIAL or fn == "product":
            host = next((d for f in b.get("folds_into") or [] for d in depts if f in d["functions"]), depts[0])
            host["functions"].append(fn)
            if fn in ESSENTIAL and len(host["members"]) < 3:      # a named person owns it, not "someone"
                m = dict(b["members"][0])
                m["id"] = fn if fn not in {x["id"] for x in host["members"]} else f"{fn}_lead"
                m["role"] = m["role"].replace(", head", "")
                host["members"].append(m)
            later = [x for x in later if x["function"] != fn] + (
                [{"function": fn, "when": dict(b.get("starts_at") or {}), "why": f"{b['name']} gets its own team"}]
                if not reached(b.get("starts_at") or {}, stage, {}) else [])
            have.add(fn)
        else:
            later.append({"function": fn, "when": dict(b.get("starts_at") or {"stage": "growing"}),
                          "why": f"{b['name']}: {b['mission']}"})
    return depts, later


def clean_later(items) -> list[dict]:
    out, seen = [], set()
    for x in items or []:
        if not isinstance(x, dict) or x.get("function") not in FUNCTIONS or x["function"] in seen:
            continue
        seen.add(x["function"])
        w = x.get("when") if isinstance(x.get("when"), dict) else {}
        when = {}
        if w.get("metric"):
            try:
                when = {"metric": re.sub(r"[^a-z0-9_]+", "_", str(w["metric"]).lower()).strip("_")[:40],
                        "gte": float(w.get("gte", 1))}
            except (TypeError, ValueError):
                when = {}
        elif w.get("stage") in STAGES:
            when = {"stage": w["stage"]}
        out.append({"function": x["function"], "when": when or dict(all()[x["function"]].get("starts_at") or {}),
                    "why": str(x.get("why") or "")[:160]})
    return out


# ---------- equip a department with what its functions need ----------
def equip(org, did: str, duties: bool = False) -> dict:
    """Playbooks and the numbers a department answers for. Idempotent. Duties and abilities are the owner's call:
    nothing is scheduled or requested here."""
    dept = org.c.depts.get(did)
    out = {"skills": 0, "kpis": 0}
    if not dept:
        return out
    for fn in dept.functions:
        b = all().get(fn)
        if not b:
            continue
        p = org.c.dept_skills_dir(did) / f"{fn}.md"
        if b.get("skill") and not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(b["skill"].strip() + "\n", encoding="utf-8")
            out["skills"] += 1
        for k in b.get("kpis") or []:
            try:
                if org.metrics.get(k["metric"]) is None:
                    org.metrics.set(k["metric"], None, unit=k.get("unit") or "", source="blueprint")
                    out["kpis"] += 1
            except ValueError:
                pass
    org._sys_cache.clear()
    return out


# ---------- the org plan and its triggers ----------
def plan(org) -> dict:
    return org.s.get(PLAN_KEY) or {"later": []}


def save_later(org, later: list[dict]) -> None:
    p = plan(org)
    p["later"] = clean_later(later)
    org.s.set(PLAN_KEY, p)


def roadmap(org) -> dict:
    """What the Company page shows: who carries each function now, and what comes later and when."""
    now, later = [], []
    waiting = {x["function"]: x for x in plan(org).get("later") or []}
    for fn in FUNCTIONS:
        b = all()[fn]
        d = dept_for(org.c, fn)
        own = bool(d and d.functions and d.functions[0] == fn)
        if d:
            now.append({"function": fn, "name": b["name"], "dept": d.id, "dept_name": d.name, "own_team": own})
        if fn in waiting and not own:
            w = waiting[fn]
            later.append({"function": fn, "name": b["name"], "when": when_text(w["when"]), "why": w.get("why", ""),
                          "state": w.get("state", "planned")})
        elif not d:
            later.append({"function": fn, "name": b["name"], "when": "not planned", "why": b["mission"],
                          "state": "uncovered"})
    return {"now": now, "later": later, "stage": org.c.business.get("stage", "")}


def numbers(org) -> dict[str, float]:
    return {m["name"]: m["value"] for m in org.metrics.all() if m["value"] is not None}


def on_dept_created(org, did: str) -> None:
    """A function that got its own team is no longer carried by the team that folded it in."""
    dept = org.c.depts.get(did)
    if not dept or not dept.functions:
        return
    own = dept.functions[0]
    with org.c.lock:
        changed = False
        for oid, raw in (org.c.raw.get("departments") or {}).items():
            fs = raw.get("functions") or []
            if oid != did and own in fs and len(fs) > 1:
                raw["functions"] = [f for f in fs if f != own]
                changed = True
        if changed:
            org.c.save()


def reorganise(org) -> dict:
    """Map an existing company's departments to the functions they already do, without losing anything: every
    department, folder, task and ability stays. A function nobody does is folded into the team its blueprint names.
    New teams are the owner's call (Company page)."""
    with org.c.lock:
        for did, raw in (org.c.raw.get("departments") or {}).items():
            if not clean_functions(raw.get("functions")):
                raw["functions"] = infer(did, raw.get("name", ""), raw.get("mission", ""))
        org.c.save()
    have = {f for d in org.c.depts.values() for f in d.functions}
    for fn in FUNCTIONS:
        if fn in have:
            continue
        host = next((d for f in all()[fn].get("folds_into") or [] for d in org.c.depts.values() if f in d.functions),
                    None)
        if host:
            with org.c.lock:
                raw = org.c.raw["departments"][host.id]
                raw["functions"] = list(dict.fromkeys([*(raw.get("functions") or host.functions), fn]))
                org.c.save()
            have.add(fn)
    equipped = {did: equip(org, did) for did in list(org.c.depts)}
    uncovered = [fn for fn in FUNCTIONS if fn not in have]
    org.log_decision("design", org.c.name, "reorganised",
                     f"{len(org.c.depts)} departments kept; not covered: {', '.join(uncovered) or 'none'}")
    return {"kept": list(org.c.depts), "proposed": [], "later": uncovered, "equipped": equipped}
