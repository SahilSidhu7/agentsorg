"""The company's records: who it is selling to (the pipeline), what was said, and the money that came in.

A contact moves lead -> contacted -> replied -> qualified -> proposal -> won (a customer), or to lost; a customer
can become churned. Records are written three ways, none of which costs a model call:
- agents end a reply with `LEAD: name | email | org | source` or `CLIENT: email | stage | next step`;
- the owner's Send click on an Outbox email logs it and moves a lead to contacted;
- inbound mail and product events (inbound.py), and the owner's own edits on the Clients page.
The company's numbers (leads, reply_rate, customers, revenue_month, mrr) are counted from these records.
"""
from __future__ import annotations

import csv
import io
import re
import time
from datetime import date, datetime, timedelta

STAGES = ["lead", "contacted", "replied", "qualified", "proposal", "won", "lost", "churned"]
OPEN = ("lead", "contacted", "replied", "qualified", "proposal")
ORDER = {s: i for i, s in enumerate(["lead", "contacted", "replied", "qualified", "proposal", "won"])}
STAGE_WORDS = {"customer": "won", "client": "won", "paid": "won", "closed": "won", "closed won": "won",
               "closed lost": "lost", "new": "lead", "prospect": "lead", "interested": "replied",
               "meeting": "qualified", "demo": "qualified", "quote": "proposal", "negotiation": "proposal",
               "cancelled": "churned", "canceled": "churned"}
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
LEAD_RE = re.compile(r"^\s*LEAD:\s*(.+)$", re.M)
CLIENT_RE = re.compile(r"^\s*CLIENT:\s*(.+)$", re.M)
FOLLOW_UP_DAYS = 4
MAX_FOLLOW_UPS = 3
DERIVED = ("leads", "reply_rate", "customers", "revenue_month", "mrr")


def norm_email(s: str) -> str:
    m = EMAIL_RE.search(str(s or ""))
    return m.group(0).lower() if m else ""


def norm_stage(s: str) -> str:
    s = re.sub(r"[^a-z ]+", " ", str(s or "").lower()).strip()
    return s if s in STAGES else STAGE_WORDS.get(s, "")


class Clients:
    def __init__(self, org):
        self.org = org
        self.s = org.s

    # ---------- contacts ----------
    def get(self, cid: int) -> dict | None:
        return self.s.one("SELECT * FROM contacts WHERE id=?", (cid,))

    def by_email(self, email: str) -> dict | None:
        e = norm_email(email)
        return self.s.one("SELECT * FROM contacts WHERE email=?", (e,)) if e else None

    def all(self, stage: str | None = None, limit: int = 500) -> list[dict]:
        if stage:
            return self.s.q("SELECT * FROM contacts WHERE stage=? ORDER BY updated DESC LIMIT ?", (stage, limit))
        return self.s.q("SELECT * FROM contacts ORDER BY updated DESC LIMIT ?", (limit,))

    def upsert(self, email: str, *, name: str = "", org: str = "", source: str = "", stage: str = "",
               value: float | None = None, currency: str = "", dept: str = "", next_step: str | None = None,
               next_date: str | None = None, notes: str | None = None, trust: str = "", force: bool = False) -> dict:
        """Create or update the contact with this email. A stage only moves forward unless `force` (the owner);
        unverified sources never overwrite what a trusted one wrote."""
        e = norm_email(email)
        if not e:
            raise ValueError("a contact needs an email address")
        now = time.time()
        cur = self.s.one("SELECT * FROM contacts WHERE email=?", (e,))
        stage = norm_stage(stage)
        if cur is None:
            cid = self.s.x(
                "INSERT INTO contacts(created,updated,name,org,email,source,stage,value,currency,dept,next_step,"
                "next_date,notes,trust) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (now, now, name.strip()[:80], org.strip()[:80], e, source.strip()[:60], stage or "lead", value,
                 currency[:6], dept, (next_step or "")[:200], next_date or "", (notes or "")[:1000], trust))
            self.refresh_numbers()
            return self.get(cid)
        f: dict = {}
        weak = trust == "untrusted" and (cur.get("trust") or "") != "untrusted"
        for k, v, n in (("name", name, 80), ("org", org, 80), ("source", source, 60), ("currency", currency, 6),
                        ("dept", dept, 24)):
            if v and (force or not cur.get(k)) and not (weak and cur.get(k)):
                f[k] = v.strip()[:n]
        if value is not None and (force or not weak):
            f["value"] = value
        if next_step is not None and (force or not weak):
            f["next_step"] = next_step[:200]
        if next_date is not None and (force or not weak):
            f["next_date"] = next_date
        if notes is not None and force:
            f["notes"] = notes[:1000]
        if stage and stage != cur["stage"] and not weak and (force or self._forward(cur["stage"], stage)):
            f["stage"] = stage
        if f:
            f["updated"] = now
            self.s.x(f"UPDATE contacts SET {','.join(f'{k}=?' for k in f)} WHERE id=?", (*f.values(), cur["id"]))
            if "stage" in f:
                self.log(cur["id"], "stage", f"{cur['stage']} -> {f['stage']}")
                self.refresh_numbers()
        return self.get(cur["id"])

    @staticmethod
    def _forward(old: str, new: str) -> bool:
        if new in ("lost", "churned"):
            return (new == "churned") == (old == "won")
        return old in ORDER and new in ORDER and ORDER[new] > ORDER[old]

    def remove(self, cid: int) -> None:
        self.s.x("DELETE FROM interactions WHERE contact_id=?", (cid,))
        self.s.x("DELETE FROM contacts WHERE id=?", (cid,))
        self.refresh_numbers()

    # ---------- what was said ----------
    def log(self, cid: int, kind: str, summary: str, outbox_id: int | None = None, task_id: int | None = None) -> int:
        return self.s.x("INSERT INTO interactions(contact_id,at,kind,summary,outbox_id,task_id) VALUES(?,?,?,?,?,?)",
                        (cid, time.time(), kind, self.org.env.redact(str(summary))[:4000], outbox_id, task_id))

    def timeline(self, cid: int, limit: int = 30) -> list[dict]:
        return self.s.q("SELECT * FROM interactions WHERE contact_id=? ORDER BY id DESC LIMIT ?", (cid, limit))

    def sent(self, to: str, subject: str, outbox_id: int | None = None, dept: str = "") -> dict | None:
        """The owner sent an email: the contact exists from now on, a lead becomes contacted, a follow-up is due."""
        e = norm_email(to)
        if not e:
            return None
        c = self.upsert(e, source="outbox", dept=dept)
        self.log(c["id"], "email_out", subject[:200], outbox_id=outbox_id)
        nxt = (date.today() + timedelta(days=FOLLOW_UP_DAYS)).isoformat()
        return self.upsert(e, stage="contacted" if c["stage"] == "lead" else "", next_date=nxt,
                           next_step=c.get("next_step") or "follow up if no reply")

    # ---------- money ----------
    def add_revenue(self, amount: float, *, email: str = "", contact_id: int | None = None, currency: str = "",
                    kind: str = "one-off", source: str = "owner", ref: str = "", at: float | None = None) -> dict:
        kind = kind if kind in ("one-off", "recurring", "refund") else "one-off"
        amount = -abs(float(amount)) if kind == "refund" else float(amount)
        if ref and (dup := self.s.one("SELECT * FROM revenue WHERE ref=?", (ref,))):
            return dup                                   # the same payment event delivered twice
        c = self.get(contact_id) if contact_id else (self.upsert(email, source=source) if norm_email(email) else None)
        rid = self.s.x("INSERT INTO revenue(at,contact_id,amount,currency,kind,source,ref) VALUES(?,?,?,?,?,?,?)",
                       (at or time.time(), c["id"] if c else None, amount, currency.upper()[:6], kind, source, ref[:120]))
        if c:
            self.log(c["id"], "payment", f"{kind}: {amount:g} {currency.upper()}".strip())
            if kind != "refund" and c["stage"] != "won":
                self.upsert(c["email"], stage="won", force=True)
        self.refresh_numbers()
        return self.s.one("SELECT * FROM revenue WHERE id=?", (rid,))

    def revenue(self, limit: int = 100) -> list[dict]:
        return self.s.q("SELECT r.*, c.name, c.email FROM revenue r LEFT JOIN contacts c ON c.id=r.contact_id "
                        "ORDER BY r.at DESC LIMIT ?", (limit,))

    # ---------- numbers, counted not typed ----------
    def counts(self) -> dict[str, int]:
        by = {r["stage"]: r["n"] for r in self.s.q("SELECT stage, COUNT(*) n FROM contacts GROUP BY stage")}
        return {s: by.get(s, 0) for s in STAGES}

    def numbers(self) -> dict[str, float]:
        c = self.counts()
        if not sum(c.values()) and not self.s.one("SELECT COUNT(*) n FROM revenue")["n"]:
            return {}
        written = sum(c[s] for s in ("contacted", "replied", "qualified", "proposal", "won", "lost", "churned"))
        replied = self.s.one("SELECT COUNT(DISTINCT contact_id) n FROM interactions WHERE kind='email_in'")["n"]
        replied = max(replied, sum(c[s] for s in ("replied", "qualified", "proposal")))
        month = datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()
        rev = self.s.one("SELECT COALESCE(SUM(amount),0) t FROM revenue WHERE at>=?", (month,))["t"]
        mrr = self.s.one("SELECT COALESCE(SUM(amount),0) t FROM revenue WHERE kind='recurring' AND at>=?",
                         (time.time() - 31 * 86400,))["t"]
        return {"leads": sum(c[s] for s in OPEN), "customers": c["won"],
                "reply_rate": round(100 * min(replied, written) / written, 1) if written else 0.0,
                "revenue_month": round(rev, 2), "mrr": round(mrr, 2)}

    def refresh_numbers(self) -> None:
        for name, v in self.numbers().items():
            cur = self.org.metrics.get(name)
            if cur is None or cur["value"] != v:
                self.org.metrics.set(name, v, unit="%" if name == "reply_rate" else None, source="records")

    # ---------- what agents see ----------
    def due(self, today: str | None = None) -> list[dict]:
        today = today or date.today().isoformat()
        return self.s.q(f"SELECT * FROM contacts WHERE stage IN ({','.join('?' * len(OPEN))}) AND next_date!='' "
                        "AND next_date<=? ORDER BY next_date LIMIT 50", (*OPEN, today))

    def facts(self) -> str:
        c = self.counts()
        if not sum(c.values()):
            return ""
        n = self.numbers()
        lines = ["PIPELINE (the company's client records; counted, not estimated):",
                 "- " + " · ".join(f"{s} {c[s]}" for s in STAGES if c[s]),
                 f"- follow-ups due: {len(self.due())} · reply rate {n['reply_rate']:g}% · "
                 f"revenue this month {n['revenue_month']:g} · recurring (31 days) {n['mrr']:g}"]
        hot = self.s.q("SELECT name, email, org, stage, next_step FROM contacts WHERE stage IN "
                       "('replied','qualified','proposal') ORDER BY updated DESC LIMIT 5")
        if hot:
            lines.append("- furthest along: " + "; ".join(
                f"{h['name'] or h['email']}{' (' + h['org'] + ')' if h['org'] else ''} [{h['stage']}]"
                + (f" next: {h['next_step'][:50]}" if h["next_step"] else "") for h in hot))
        return "\n".join(lines)

    def context(self, body: str) -> str:
        """The record of any contact a task names (by email), for the agent doing that task."""
        out = []
        for e in list(dict.fromkeys(m.lower() for m in EMAIL_RE.findall(body or "")))[:3]:
            if c := self.by_email(e):
                out.append(self.card(c))
        return "\n\n".join(out)

    def card(self, c: dict, n: int = 4) -> str:
        head = (f"{c['name'] or c['email']} <{c['email']}>" + (f", {c['org']}" if c["org"] else "")
                + f" · stage {c['stage']}" + (f" · next: {c['next_step']}" if c["next_step"] else "")
                + (" · [unverified record]" if c.get("trust") == "untrusted" else ""))
        rows = [f"  {time.strftime('%Y-%m-%d', time.localtime(i['at']))} {i['kind']}: "
                f"{' '.join(i['summary'].split())[:200]}" for i in reversed(self.timeline(c["id"], n))]
        return "\n".join([head, *rows])

    # ---------- agents' footer lines ----------
    def ingest(self, text: str, dept: str, task_id: int, trust: str = "") -> list[str]:
        got = []
        for m in LEAD_RE.finditer(text or ""):
            parts = [p.strip() for p in m.group(1).split("|")]
            email = next((norm_email(p) for p in parts if norm_email(p)), "")
            if not email:
                continue
            rest = [p for p in parts if norm_email(p) != email and p and p.lower() not in ("none", "-", "n/a")]
            had = self.by_email(email)
            c = self.upsert(email, name=rest[0] if rest else "", org=rest[1] if len(rest) > 1 else "",
                            source=(rest[2] if len(rest) > 2 else f"task #{task_id}"), dept=dept, trust=trust,
                            **({} if had else {"next_date": date.today().isoformat(),      # a first email gets drafted
                                               "next_step": "first email"}))
            if not had:
                self.log(c["id"], "note", f"added by {dept} (task #{task_id})", task_id=task_id)
                got.append(f"lead {email}")
        for m in CLIENT_RE.finditer(text or ""):
            parts = [p.strip() for p in m.group(1).split("|")]
            email = norm_email(parts[0]) if parts else ""
            if not email or len(parts) < 2:
                continue
            stage = norm_stage(parts[1])
            step = parts[2][:200] if len(parts) > 2 else None
            before = self.by_email(email)
            c = self.upsert(email, stage=stage, next_step=step, dept=dept, trust=trust)
            if not before or before["stage"] != c["stage"] or (step and before.get("next_step") != c.get("next_step")):
                got.append(f"client {email} {c['stage']}")
        return got

    # ---------- moving an existing business in ----------
    def import_csv(self, text: str, source: str = "import") -> dict:
        """Rows of name, email, org/company, stage/status, value, notes. Unknown columns are ignored."""
        rows = list(csv.reader(io.StringIO(text.strip())))
        if not rows:
            return {"added": 0, "updated": 0, "skipped": 0}
        head = [h.strip().lower() for h in rows[0]]
        if not any(h in ("email", "e-mail", "mail", "email address") for h in head):
            head, body = ["name", "email", "org", "stage", "value", "notes"], rows
        else:
            body = rows[1:]

        def col(r, *names):
            for n in names:
                if n in head and head.index(n) < len(r) and r[head.index(n)].strip():
                    return r[head.index(n)].strip()
            return ""
        out = {"added": 0, "updated": 0, "skipped": 0}
        for r in body[:5000]:
            email = norm_email(col(r, "email", "e-mail", "mail", "email address") or " ".join(r))
            if not email:
                out["skipped"] += 1
                continue
            try:
                val = float(re.sub(r"[^\d.]", "", col(r, "value", "amount", "deal value")) or "x")
            except ValueError:
                val = None
            had = self.by_email(email)
            self.upsert(email, name=col(r, "name", "full name", "contact"), org=col(r, "org", "company", "business",
                                                                                   "organisation", "organization"),
                        stage=col(r, "stage", "status"), value=val, source=col(r, "source") or source,
                        notes=col(r, "notes", "note") or None, force=not had)
            out["updated" if had else "added"] += 1
        self.refresh_numbers()
        return out
