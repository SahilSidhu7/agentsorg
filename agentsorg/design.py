"""Design the leanest office for what the user describes (1 Sonnet call, once), then apply it."""
from __future__ import annotations

import json
import re

from . import blueprints
from .config import RESERVED

DESIGN_PROMPT = """You design a virtual company of Claude agents that works like a real company and stays lean.
A real company covers every FUNCTION below, from knowing the market to keeping customers. Your design must answer
for each one that matters at this stage: a department does it (list it in that department's "functions"). Early on,
one department carries several functions. The owner adds teams later as the business grows.
Rules:
- 2-5 departments, sized to the STAGE in the owner's answers: at "idea" or "building" 2-3 teams carrying several
  functions each; with paying customers, delivery and support need real owners. 1-3 members per department, max 10.
- research and marketing are never left out: now, each is a department or a NAMED member of one (role says so).
- The first function in a department's "functions" is its main one. The first member is its head.
- tier = default model: 1 = Haiku (default: short, formulaic, high-volume work), 2 = Sonnet (code, long-form writing,
  real analysis). Never 3 as a default.
- max_tier = highest model the member may escalate to. Use 3 (Opus) for at most one head that does hard reasoning.
- tools: ["Read","Edit","Write","Glob","Grep"] only for a department that must write code or files, otherwise [].
- rules: <=50 words of department-specific working rules. skill: <=70 words of reusable know-how for the domain.
  When folders are described, mention in rules which linked folder(s) the department works in and what's there.
Existing folders: if the user describes folders (with a machine label like "PC1"/"this-pc" and a path, plus what
they hold), shape departments around how the work is actually organised, and map EVERY described folder to the
one department that should work on it, in "sources". Only use folders the user described; never invent paths.
A department that edits code or files in a folder needs the file tools above; one that only reads docs doesn't.
Also write up to 3 measurable goals (each with a number to track). Base them on what the user said and on THE
EXISTING PROJECT, never on guesses.
Output ONLY JSON, no prose:
{"name":"<short company name>","about":"<=30 words: what the company does and for whom",
 "goals":[{"goal":"<measurable goal>","metric":"snake_case_name","target":<number>,"unit":"<optional>",
   "due":"YYYY-MM-DD","dept":"<department id that owns it>"}],
 "departments":[{"id":"slug","name":"Display Name","mission":"<=15 words","functions":["<function id>"],"tools":[],
   "rules":"...","skill":"...","members":[{"id":"slug","name":"<first name>","role":"<=8 words","tier":1,"max_tier":2}]}],
 "sources":[{"name":"short-slug","dept":"<department id>","origin":"<machine label> <path exactly as given>",
   "why":"<=10 words"}]}
Each function's standard numbers and playbook are added automatically: don't repeat them in skill.
Prefer metrics the company counts itself from its client records: leads, reply_rate, customers, revenue_month, mrr."""

TOOLS = ["Read", "Edit", "Write", "Glob", "Grep"]
MAX_DESCRIPTION = 16000        # room for folder outlines and project rules files
OUTLINE_SKIP = {"node_modules", ".git", ".venv", "venv", "__pycache__", "dist", "build", ".next", "target",
                ".idea", ".vscode", ".pytest_cache", ".mypy_cache", ".cache", "coverage", ".wrangler"}


def folder_outline(path: str, label: str = "this-pc", depth: int = 2, max_lines: int = 50) -> str:
    """Deterministic outline of a local folder for the designer (no model call): folders with file counts/types."""
    from collections import Counter
    from pathlib import Path
    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"{root} is not a folder")
    lines = [f"FOLDER {label} {root}"]

    def describe(d: Path) -> str:
        files = [p for p in d.iterdir() if p.is_file() and not p.name.startswith(".")]
        kinds = Counter((p.suffix.lower().lstrip(".") or "noext") for p in files)
        top = ", ".join(f"{n} {k}" for k, n in kinds.most_common(4))
        return f"{len(files)} files" + (f" ({top})" if top else "")

    def walk(d: Path, level: int):
        if len(lines) >= max_lines:
            return
        subs = sorted(p for p in d.iterdir() if p.is_dir() and p.name not in OUTLINE_SKIP and not p.name.startswith("."))
        for s in subs:
            if len(lines) >= max_lines:
                lines.append("  …")
                return
            lines.append(f"{'  ' * level}{s.name}/: {describe(s)}")
            if level < depth:
                walk(s, level + 1)

    lines.append(f"  ./: {describe(root)}")
    walk(root, 1)
    readme = next((root / n for n in ("README.md", "readme.md", "README.txt") if (root / n).exists()), None)
    if readme:
        gist = " ".join(l.strip("# ").strip() for l in readme.read_text(encoding="utf-8", errors="replace").splitlines()
                        if l.strip() and not l.startswith(("![", "<")))[:300]
        lines.append(f"  README: {gist}")
    return "\n".join(lines)


def _slug(s: str, fallback: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", str(s).lower()).strip("_")[:24]
    return s or fallback


QUESTIONS = [
    ("customer", r"\b(customers?|clients?|users?|buyers?|audience|for (small|busy|clinics?|teams?|founders?|people))\b",
     "Who is your customer? (who pays, and what problem you solve for them)"),
    ("offer", r"\b(sell|selling|price|pricing|subscription|service|product|app|saas|course|agency|shop|store)\b",
     "What do you sell or offer, and at what price (or not decided yet)?"),
    ("stage", r"\b(idea|mvp|launched|live|revenue|paying|beta|prototype|waitlist|stage)\b",
     "Where are you now? (idea, building, launched, paying customers…)"),
    ("target", r"\b(\d+ (customers?|clients?|users?|signups?|sales)|by (the )?(end|jan|feb|mar|apr|may|jun|jul|aug|sep|"
               r"oct|nov|dec|q[1-4])|in \d+ (days|weeks|months)|goal|target)\b",
     "What should be true in 90 days? (one number, e.g. 10 paying clinics)"),
    ("channels", r"\b(linkedin|instagram|facebook|tiktok|youtube|seo|google|ads?|cold (email|call)|referrals?|"
                 r"word of mouth|newsletter|partners?|marketplace|channel)\b",
     "How do customers find you today, or how should they? (e.g. cold email, LinkedIn, referrals, search)"),
]
ANSWER_FIELDS = {"customer": "customer", "offer": "offer", "stage": "stage", "target": "target_90d",
                 "channels": "channels", "pricing": "pricing", "constraints": "constraints"}
STAGE_WORDS = [("growing", r"grow|scal|\b\d{2,}\s+(paying\s+)?(customers?|clients?)"),
               ("first_customers", r"paying|first (customers?|clients?)|revenue|\b[1-9]\s+(customers?|clients?)"),
               ("launched", r"launched|live|released|in production|beta users"),
               ("building", r"building|mvp|prototype|beta|develop|waitlist|almost"),
               ("idea", r"idea|not started|nothing yet|planning|thinking")]


def questions(description: str, pack: str = "") -> list[dict]:
    """Up to 5 short questions for what the description and the project don't already answer (0 tokens)."""
    text = f"{description}\n{pack}".lower()
    return [{"id": k, "q": q} for k, pat, q in QUESTIONS if not re.search(pat, text)][:5]


def with_answers(description: str, answers: dict) -> str:
    extra = [f"{k}: {str(v).strip()[:300]}" for k, v in (answers or {}).items() if str(v).strip()]
    return description.strip() + ("\n\nOWNER'S ANSWERS:\n" + "\n".join(extra) if extra else "")


def stage_of(text: str) -> str:
    """The business stage in the owner's own words -> one of blueprints.STAGES ('' when they didn't say)."""
    low = str(text or "").lower()
    if re.sub(r"[\s-]+", "_", low.strip()) in blueprints.STAGES:
        return re.sub(r"[\s-]+", "_", low.strip())
    low = re.sub(r"\b(no|zero|0|without|not yet) (paying )?(customers?|clients?|revenue|users?)\b", "", low)
    return next((s for s, pat in STAGE_WORDS if re.search(pat, low)), "")


def profile_from(answers: dict) -> dict:
    """The interview answers as business-profile fields. Only what the owner said: nothing is guessed."""
    out = {}
    for k, v in (answers or {}).items():
        f = ANSWER_FIELDS.get(k)
        v = " ".join(str(v or "").split())
        if not f or not v:
            continue
        if f == "stage":
            if s := stage_of(v):
                out["stage"] = s
            continue
        out[f] = v
    if out.get("offer") and not out.get("pricing"):
        m = re.search(r"[^.;]*(?:[$€£₹]\s?\d|\d\s?(?:usd|eur|inr|gbp|/mo|per month|a month))[^.;]*", out["offer"], re.I)
        if m:
            out["pricing"] = m.group(0).strip()
    return out


def set_business(org, fields: dict, replace: bool = False) -> dict:
    """Save profile fields (empty values clear a field only when `replace`). Returns the stored profile."""
    from .config import clean_business
    with org.c.lock:
        cur = {} if replace else dict(org.c.raw.get("business") or {})
        for k, v in (fields or {}).items():
            if k in ANSWER_FIELDS.values():
                if str(v or "").strip():
                    cur[k] = v
                elif replace or v == "":
                    cur.pop(k, None)
        org.c.raw["business"] = clean_business(cur)
        org.c.save()
    org._sys_cache.clear()
    return org.c.business


def clamp_dept(d: dict, room: int = 3, tools: bool = True) -> dict:
    """One department, clamped to a safe, lean shape (1-3 members, tier <= Sonnet by default)."""
    if not isinstance(d, dict):
        raise ValueError("department must be an object")
    did = _slug(d.get("id") or d.get("name"), "")
    if not did or did == "ceo":
        raise ValueError("department needs an id other than ceo")
    if did in RESERVED:
        did = f"{did}_team"
    members, mseen = [], set()
    for j, m in enumerate((d.get("members") or [])[:min(3, room)]):
        if not isinstance(m, dict):
            continue
        mid = _slug(m.get("id") or m.get("name"), f"m{j}")
        if mid in mseen:
            continue
        mseen.add(mid)
        tier = min(max(int(m.get("tier") or 1), 1), 2)
        members.append({"id": mid, "name": str(m.get("name") or mid.title())[:20], "role": str(m.get("role") or "")[:60],
                        "tier": tier, "max_tier": min(max(int(m.get("max_tier") or tier), tier), 3)})
    if not members:
        raise ValueError(f"department {did!r} has no members")
    name, mission = str(d.get("name") or did.title())[:30], str(d.get("mission") or "")[:120]
    return {"id": did, "name": name, "mission": mission,
            "functions": blueprints.clean_functions(d.get("functions")) or blueprints.infer(did, name, mission),
            "tools": [t for t in (d.get("tools") or []) if t in TOOLS] if tools else [],
            "rules": str(d.get("rules") or "")[:400], "skill": str(d.get("skill") or "")[:600], "members": members}


def _write_dept(org, d: dict) -> None:
    org.c.raw.setdefault("departments", {})[d["id"]] = {
        "name": d["name"], "mission": d["mission"],
        **({"functions": d["functions"]} if d.get("functions") else {}),
        **({"tools": d["tools"], "max_turns": 8} if d["tools"] else {}),
        "members": {m["id"]: {k: m[k] for k in ("name", "role", "tier", "max_tier")} for m in d["members"]},
    }
    for text, p in ((d["rules"], org.c.instructions_path(d["id"])), (d["skill"], org.c.dept_skills_dir(d["id"]) / "domain.md")):
        if text:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text.strip() + "\n", encoding="utf-8")


def add_dept(org, d: dict) -> str:
    """Add one department to the running company, keeping the others. Returns its id."""
    d = clamp_dept(d, tools=False)            # abilities are switched on by the owner, on the department's page
    if d["id"] in org.c.depts:
        raise ValueError(f"department {d['id']!r} already exists")
    _write_dept(org, d)
    org.c.save()
    org._sys_cache.clear()
    org.files.ensure_layout()
    blueprints.on_dept_created(org, d["id"])      # the team that carried this function hands it over
    blueprints.equip(org, d["id"])                # playbook and numbers
    return d["id"]


LEAN = {"name": "", "about": "", "departments": [
    {"id": "product", "name": "Product", "mission": "Build and fix the product", "functions": ["product"],
     "tools": ["Read", "Edit", "Write", "Glob", "Grep"],
     "members": [{"id": "lead", "name": "Ada", "role": "product engineer, head", "tier": 2, "max_tier": 3}]},
    {"id": "growth", "name": "Growth", "mission": "Find customers: research, marketing, sales and outreach",
     "functions": ["marketing", "sales", "research"],
     "members": [{"id": "lead", "name": "Max", "role": "growth and sales lead, head", "tier": 2, "max_tier": 2},
                 {"id": "writer", "name": "Ivy", "role": "copywriter and content", "tier": 1, "max_tier": 2},
                 {"id": "research", "name": "Marie", "role": "market and customer researcher", "tier": 2, "max_tier": 2}]},
    {"id": "ops", "name": "Operations", "mission": "Money, delivery, support and admin",
     "functions": ["operations", "finance", "delivery", "support"],
     "members": [{"id": "lead", "name": "Ola", "role": "operations and finance", "tier": 1, "max_tier": 2}]}],
    "later": [{"function": "sales", "when": {"stage": "launched"}, "why": "selling becomes a full-time job"},
              {"function": "research", "when": {"stage": "launched"}, "why": "decisions need steady market evidence"},
              {"function": "delivery", "when": {"stage": "first_customers"}, "why": "paid orders need an owner"},
              {"function": "support", "when": {"metric": "customers", "gte": 5}, "why": "customers need fast answers"},
              {"function": "finance", "when": {"metric": "customers", "gte": 10}, "why": "revenue needs tracking"}]}


def update_dept(org, did: str, **f) -> None:
    """Rename / re-mission a department and edit its instructions.md and skill file."""
    if did not in org.c.depts:
        raise KeyError(f"unknown department {did!r}")
    with org.c.lock:
        d = org.c.raw["departments"][did]
        if f.get("name") is not None:
            d["name"] = str(f["name"]).strip()[:30] or d.get("name", did)
        if f.get("mission") is not None:
            d["mission"] = str(f["mission"]).strip()[:160]
        org.c.save()
    for key, path in (("instructions", org.c.instructions_path(did)), ("skill", org.c.dept_skills_dir(did) / "domain.md")):
        if f.get(key) is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(f[key]).strip()[:8000] + "\n", encoding="utf-8")
    org._sys_cache.clear()


def remove_dept(org, did: str) -> str:
    """Remove a department. Its folder is kept under a dated archive name; open work is archived."""
    import time as _t
    if did not in org.c.depts:
        raise KeyError(f"unknown department {did!r}")
    if len(org.c.depts) <= 1:
        raise ValueError("the company needs at least one department")
    if any(s.dept == did for s in org.files.sources().values()):
        raise ValueError("move or unlink this department's folders first (Files page)")
    with org.c.lock:
        org.c.raw["departments"].pop(did, None)
        org.c.save()
    src = org.c.home / did
    dest = org.c.home / f"_archived_{did}_{_t.strftime('%Y%m%d-%H%M%S')}"
    if src.exists():
        src.rename(dest)
    org._sys_cache.clear()
    org.archive_orphans()
    org.log_decision("department", did, "removed", f"folder kept as {dest.name}")
    return dest.name


def validate(plan: dict, stage: str = "") -> dict:
    """Clamp whatever the model returned into a safe, lean structure that covers every company function."""
    if not isinstance(plan, dict) or not isinstance(plan.get("departments"), list) or not plan["departments"]:
        raise ValueError("design has no departments")
    out = {"name": str(plan.get("name") or "agents.org")[:40], "about": str(plan.get("about") or "")[:240],
           "departments": []}
    stage = stage or str(plan.get("stage") or "") or "idea"
    seen, total = set(), 0
    for i, d in enumerate(plan["departments"][:5]):
        did = _slug(d.get("id") or d.get("name"), f"dept{i}")
        if did == "ceo":                        # the CEO is the router, not a department
            continue
        if did in RESERVED:                     # department folders sit beside data/, mods/, shared/...
            did = f"{did}_team"
        if did in seen:
            continue
        seen.add(did)
        try:
            dept = clamp_dept({**d, "id": did}, room=12 - total)
        except ValueError:
            continue
        total += len(dept["members"])
        out["departments"].append(dept)
    if not out["departments"]:
        raise ValueError("design has no usable departments")
    # every function is covered now or scheduled for later; research and marketing always have a named owner
    out["departments"], out["later"] = blueprints.cover(out["departments"], blueprints.clean_later(plan.get("later")),
                                                        stage)
    out["stage"] = stage
    ids = {d["id"] for d in out["departments"]}
    out["goals"] = []
    for g in (plan.get("goals") or [])[:3]:
        if isinstance(g, dict) and str(g.get("goal") or "").strip():
            try:
                target = float(g["target"]) if g.get("target") is not None else None
            except (TypeError, ValueError):
                target = None
            out["goals"].append({"goal": str(g["goal"])[:200],
                                 "metric": re.sub(r"[^a-z0-9_]+", "_", str(g.get("metric") or "").lower()).strip("_")[:40],
                                 "target": target, "unit": str(g.get("unit") or "")[:10],
                                 "due": str(g.get("due") or "") if re.match(r"^\d{4}-\d{2}-\d{2}$", str(g.get("due") or ""))
                                 else "", "dept": g.get("dept") if g.get("dept") in ids else ""})
    out["projects"] = [{"title": str(p.get("title") or p.get("command"))[:80], "dept": p.get("dept") if p.get("dept") in ids
                        else "", "command": str(p.get("command"))[:600]}
                       for p in (plan.get("projects") or [])[:5] if isinstance(p, dict) and str(p.get("command") or "").strip()]
    out["routines"] = [{"dept": r["dept"], "every": str(r.get("every") or "")[:30], "task": str(r.get("task"))[:400]}
                       for r in (plan.get("routines") or [])[:2]
                       if isinstance(r, dict) and r.get("dept") in ids and str(r.get("task") or "").strip()]
    # opus allowed for at most one head
    opus = [m for d in out["departments"] for m in d["members"] if m["max_tier"] == 3]
    for m in opus[1:]:
        m["max_tier"] = 2
    # folder -> department mapping (only for departments that exist in this plan)
    dept_ids = {d["id"] for d in out["departments"]}
    out["sources"], names = [], set()
    for s in (plan.get("sources") or [])[:12]:
        if not isinstance(s, dict):
            continue
        dept = _slug(s.get("dept", ""), "")
        origin = str(s.get("origin") or "").strip()[:240]
        name = re.sub(r"[^a-z0-9]+", "-", str(s.get("name") or "").lower()).strip("-")[:40]
        if dept not in dept_ids or not origin or not name or name in names:
            continue
        names.add(name)
        out["sources"].append({"name": name, "dept": dept, "origin": origin, "why": str(s.get("why") or "")[:80]})
    return out


def parse(text: str, stage: str = "") -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("designer returned no JSON")
    return validate(json.loads(m.group(0)), stage)


def propose(org, description: str) -> dict:
    ok, why = org.budget.check(auto=False)
    if not ok:
        raise RuntimeError(why)
    brief = org.intake.design_brief() if hasattr(org, "intake") else ""
    stage = stage_of(re.search(r"(?mi)^stage:\s*(.+)$", description).group(1)) \
        if re.search(r"(?mi)^stage:\s*(.+)$", description) else (org.c.business.get("stage") or "")
    prompt = (f"FUNCTIONS (id: what it is for, and when it usually gets its own team):\n{blueprints.summary()}\n\n"
              f"WHAT THE USER NEEDS:\n{description.strip()[:MAX_DESCRIPTION]}")
    if stage:
        prompt += f"\n\nSTAGE: {stage}"
    if brief:
        prompt += f"\n\nTHE EXISTING PROJECT (read from its own files):\n{brief}"
    res = org.call(prompt, model=org.c.models[2], system=DESIGN_PROMPT, dept="_design", task_id=None, auto=False)
    if not res.ok:
        raise RuntimeError(res.error or "design call failed")
    plan = parse(res.text, stage)
    plan["tokens"] = res.tokens
    plan["summary"] = summarize(plan)
    return plan


def summarize(plan: dict) -> str:
    ms = [m for d in plan["departments"] for m in d["members"]]
    h = sum(m["tier"] == 1 for m in ms)
    src = f", {len(plan['sources'])} linked folder(s)" if plan.get("sources") else ""
    later = [x["function"] for x in plan.get("later") or []]
    return (f"{len(plan['departments'])} departments, {len(ms)} employees: {h} default to Haiku, "
            f"{len(ms) - h} to Sonnet, {sum(m['max_tier'] == 3 for m in ms)} may escalate to Opus{src}"
            + (f". Own teams later: {', '.join(later)}" if later else ""))


def prune_untouched_depts(org) -> list[str]:
    """Remove folders of departments the design dropped, but only if nothing in them is the user's:
    every file must be a byte-identical packaged default and sources/ and outputs/ must be empty."""
    import filecmp
    import shutil
    from .config import PKG_DATA, RESERVED
    removed = []
    for d in sorted(p for p in org.home.iterdir() if p.is_dir()):
        if d.name in org.c.depts or d.name in RESERVED or d.name.startswith((".", "_")):
            continue
        defaults = {"instructions.md": PKG_DATA / "prompts" / f"{d.name}.md",
                    **{f"skills/{s.name}": s for s in (PKG_DATA / "skills" / d.name).glob("*.md")}}
        ok = (d / "instructions.md").exists()
        for f in d.rglob("*"):
            rel = f.relative_to(d).as_posix()
            if f.is_symlink() or (f.is_file() and not (rel in defaults and defaults[rel].exists()
                                                      and filecmp.cmp(f, defaults[rel], shallow=False))):
                ok = False
                break
            if f.is_dir() and rel not in ("skills", "sources", "outputs"):
                ok = False
                break
        if ok:
            shutil.rmtree(d)
            removed.append(d.name)
    return removed


def apply_company_plan(org, plan: dict) -> dict:
    """Goals and the number each is measured by. Nothing runs until the owner gives a command."""
    out = {"goals": 0, "routines": 0, "projects": 0}
    goals = [{"text": g["goal"], **{k: g[k] for k in ("metric", "target", "due", "dept") if g.get(k) not in (None, "")}}
             for g in plan.get("goals") or []]
    if goals:
        with org.c.lock:
            cur = list(org.c.raw.get("goals") or [])
            have = set(org.c.goals)
            org.c.raw["goals"] = (cur + [g if len(g) > 1 else g["text"] for g in goals if g["text"] not in have])[:12]
            org.c.save()
        out["goals"] = len(goals)
    for g in plan.get("goals") or []:
        if g.get("metric"):
            try:
                org.metrics.set(g["metric"], None, target=g.get("target"), unit=g.get("unit") or "", source="plan")
            except (ValueError, TypeError):
                pass
    org.log_decision("design", plan.get("name", "company"), "office built",
                     f"{len(plan.get('departments', []))} departments, {out['goals']} goals, {out['projects']} first projects")
    return out


def apply(org, plan: dict, description: str = "", answers: dict | None = None) -> list[str]:
    """Apply the design. Returns the names of folders newly linked."""
    if prof := profile_from(answers or {}):
        set_business(org, prof)
    plan = validate(plan, org.c.business.get("stage") or str(plan.get("stage") or ""))
    raw = org.c.raw
    raw["name"] = plan["name"]
    raw["about"] = plan["about"]
    raw["departments"] = {}
    for d in plan["departments"]:
        _write_dept(org, d)
    org.c.save()
    org._sys_cache.clear()
    org.archive_orphans()
    linked = []
    existing = org.files.sources()
    for s in plan.get("sources") or []:
        if s["name"] in existing:
            continue                         # already linked: keep its sync state
        org.files.add(s["name"], s["dept"], s["origin"])
        linked.append(s["name"])
    org.files.ensure_layout()
    prune_untouched_depts(org)
    apply_company_plan(org, plan)
    for d in plan["departments"]:
        blueprints.equip(org, d["id"])                        # playbooks and numbers
    if description:
        brief = re.sub(r"(?m)^FOLDER .*$|^  .*$", "", description).strip() or description.strip()
        org.kb.add("Company brief", f"{plan['about']} Owner's words: {brief[:400]}", tags="brief")
    org.s.set("designed", True)
    return linked
