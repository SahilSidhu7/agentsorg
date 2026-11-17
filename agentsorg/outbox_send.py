"""Send an approved Outbox email, only when the owner clicks Send. Agents can never call this.

The draft carries its own headers on the first lines:
    To: name@example.com
    Subject: Quick question
    (blank line)
    body...

Channel, from the company's Keys & tokens (first one configured wins):
  RESEND_API_KEY + MAIL_FROM                         https://resend.com HTTP API
  SMTP_HOST + SMTP_USER + SMTP_PASS (+ SMTP_PORT, MAIL_FROM)   any SMTP server (Gmail app password, Zoho, SES...)
"""
from __future__ import annotations

import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import parseaddr

import httpx

HEADER_RE = re.compile(r"^\s*\**(to|subject|cc)\**\s*:\s*(.+?)\s*$", re.I)


def parse_draft(body: str) -> dict:
    """{'to': [...], 'cc': [...], 'subject': str, 'text': str}. Headers are read from the top lines only."""
    lines = (body or "").replace("\r", "").split("\n")
    head: dict[str, str] = {}
    i = 0
    while i < len(lines) and i < 8:
        m = HEADER_RE.match(lines[i])
        if m:
            head[m.group(1).lower()] = m.group(2).strip().strip("*").strip()
            i += 1
        elif not lines[i].strip() and not head:
            i += 1
        else:
            break
    text = "\n".join(lines[i:]).strip()

    def addrs(v: str) -> list[str]:
        out = []
        for part in re.split(r"[,;]", v or ""):
            a = parseaddr(part.strip())[1]
            if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", a):
                out.append(a)
        return out
    return {"to": addrs(head.get("to", "")), "cc": addrs(head.get("cc", "")), "subject": head.get("subject", ""),
            "text": text}


def channel(env: dict) -> str:
    if env.get("RESEND_API_KEY") and env.get("MAIL_FROM"):
        return "resend"
    if env.get("SMTP_HOST") and env.get("SMTP_USER") and env.get("SMTP_PASS"):
        return "smtp"
    return ""


def check(body: str, env: dict) -> tuple[dict, str]:
    """(parsed draft, problem or '')."""
    d = parse_draft(body)
    if not d["to"]:
        return d, "no valid To: line at the top of the draft"
    if not d["subject"]:
        return d, "no Subject: line at the top of the draft"
    if not d["text"]:
        return d, "the draft has no body"
    if not channel(env):
        return d, ("email sending isn't set up: add RESEND_API_KEY + MAIL_FROM, or SMTP_HOST + SMTP_USER + SMTP_PASS "
                   "(+ MAIL_FROM) on the Keys & tokens page")
    return d, ""


def send(body: str, env: dict) -> str:
    d, problem = check(body, env)
    if problem:
        raise ValueError(problem)
    ch = channel(env)
    sender = env.get("MAIL_FROM") or env.get("SMTP_USER", "")
    if ch == "resend":
        r = httpx.post("https://api.resend.com/emails", timeout=20,
                       headers={"Authorization": f"Bearer {env['RESEND_API_KEY']}"},
                       json={"from": sender, "to": d["to"], "cc": d["cc"] or None, "subject": d["subject"],
                             "text": d["text"]})
        if r.status_code >= 300:
            raise ValueError(f"Resend refused it (HTTP {r.status_code}): {r.text[:200]}")
        return f"sent to {', '.join(d['to'])} via Resend"
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = sender, ", ".join(d["to"]), d["subject"]
    if d["cc"]:
        msg["Cc"] = ", ".join(d["cc"])
    msg.set_content(d["text"])
    port = int(env.get("SMTP_PORT") or 587)
    ctx = ssl.create_default_context()
    try:
        if port == 465:
            with smtplib.SMTP_SSL(env["SMTP_HOST"], port, context=ctx, timeout=30) as s:
                s.login(env["SMTP_USER"], env["SMTP_PASS"])
                s.send_message(msg)
        else:
            with smtplib.SMTP(env["SMTP_HOST"], port, timeout=30) as s:
                s.starttls(context=ctx)
                s.login(env["SMTP_USER"], env["SMTP_PASS"])
                s.send_message(msg)
    except (smtplib.SMTPException, OSError) as e:
        raise ValueError(f"SMTP failed: {type(e).__name__}: {str(e)[:200]}")
    return f"sent to {', '.join(d['to'])} via {env['SMTP_HOST']}"
