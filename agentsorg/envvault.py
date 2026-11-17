"""Keys and tokens the company's agents may use (API keys, webhooks, Claude login tokens).

Stored per company in data/env.json (owner-only permissions). Values never go into a prompt, a
report or the dashboard: agents only learn the NAMES and get the values as environment variables
of their own claude process, and any value that shows up in an agent's reply is masked before it
is saved. Each key can be limited to some departments.

Two names are special and apply to every Claude call of the company, not to tool use:
  CLAUDE_CODE_OAUTH_TOKEN   a long-lived subscription token from `claude setup-token`
  ANTHROPIC_API_KEY         bills the API account instead of the subscription
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

from .fsutil import atomic_write

_LOCK = threading.RLock()


class VaultCorrupt(RuntimeError):
    """env.json exists but can't be read: refuse to overwrite it (that would wipe every key)."""

CLAUDE_KEYS = {"CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"}
NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
FORBIDDEN = {"PATH", "HOME", "USER", "SHELL", "PWD", "LANG", "TERM", "TMPDIR", "TEMP", "TMP", "PYTHONPATH",
             "LD_PRELOAD", "LD_LIBRARY_PATH", "DYLD_INSERT_LIBRARIES", "NODE_OPTIONS", "SYSTEMROOT", "APPDATA",
             "USERPROFILE", "COMSPEC", "CLAUDE_CONFIG_DIR", "DISABLE_AUTOUPDATER", "AGENTSORG_HOME"}
MIN_MASK = 6          # shorter values aren't masked in replies (too many false hits)
OWNER_ONLY = "none"   # depts: ["none"] = no agent gets it; only agentsorg itself (e.g. sending approved email)
OWNER_DEFAULT = ("SMTP_", "MAIL_", "RESEND_", "OWNER_", "NOTIFY_", "IMAP_")   # new keys with these prefixes start owner-only


def mask(v: str) -> str:
    return (v[:4] + "…" + v[-2:]) if len(v) > 10 else "•" * min(len(v), 6)


class EnvVault:
    def __init__(self, home: Path):
        self.path = home / "data" / "env.json"

    # ---------- storage ----------
    def _load(self, strict: bool = False) -> dict:
        """strict=True (before a write) raises VaultCorrupt on an unreadable file instead of returning {}."""
        if not self.path.exists():
            return {}
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            if strict:
                raise VaultCorrupt(f"{self.path} can't be read ({e}); fix or restore it (a copy is kept as "
                                   f"env.json.bak) before changing keys") from e
            return {}
        if not isinstance(d, dict):
            if strict:
                raise VaultCorrupt(f"{self.path} is not a key list")
            return {}
        return d

    def _save(self, d: dict) -> None:
        atomic_write(self.path, json.dumps(d, indent=1), mode=0o600, backup=True)

    # ---------- edit ----------
    def set(self, name: str, value: str | None = None, *, depts: list[str] | None = None,
            note: str | None = None) -> dict:
        name = name.strip().upper()
        if not NAME_RE.match(name):
            raise ValueError("name must be letters, digits and _, starting with a letter (e.g. RESEND_API_KEY)")
        if name in FORBIDDEN:
            raise ValueError(f"{name} is a system variable; pick another name")
        with _LOCK:
            return self._set(name, value, depts, note)

    def _set(self, name, value, depts, note) -> dict:
        d = self._load(strict=True)
        cur = d.get(name, {})
        if value is None and not cur:
            raise ValueError("a new key needs a value")
        if value is not None:
            value = value.strip()
            if not value or "\n" in value:
                raise ValueError("value must be one non-empty line")
        if depts is None:
            depts = cur.get("depts") if cur else ([OWNER_ONLY] if name.startswith(OWNER_DEFAULT) else [])
        d[name] = {"value": value if value is not None else cur["value"],
                   "depts": [x for x in depts if x],
                   "note": (note if note is not None else cur.get("note", "")).strip()[:200],
                   "updated": time.time()}
        self._save(d)
        return self.public()[name]

    def remove(self, name: str) -> bool:
        with _LOCK:
            d = self._load(strict=True)
            if d.pop(name.strip().upper(), None) is None:
                return False
            self._save(d)
            return True

    def import_dotenv(self, text: str, depts: list[str] | None = None) -> list[str]:
        added = []
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.removeprefix("export ").split("=", 1)
            v = v.strip().strip('"').strip("'")
            try:
                self.set(k.strip(), v, depts=depts)
                added.append(k.strip().upper())
            except ValueError:
                continue
        return added

    # ---------- read ----------
    def public(self) -> dict:
        """Everything except the values (masked)."""
        return {k: {"masked": mask(v.get("value", "")), "depts": v.get("depts", []), "note": v.get("note", ""),
                    "updated": v.get("updated", 0), "claude": k in CLAUDE_KEYS}
                for k, v in sorted(self._load().items())}

    def _applies(self, v: dict, dept: str | None) -> bool:
        if OWNER_ONLY in (v.get("depts") or []):
            return False
        return not v.get("depts") or (dept is not None and dept in v["depts"])

    def env_for(self, dept: str | None, tools: bool) -> dict:
        """Environment for one claude process: Claude login keys always; the rest only for tool users."""
        out = {}
        for k, v in self._load().items():
            if k in CLAUDE_KEYS or (tools and self._applies(v, dept)):
                out[k] = v.get("value", "")
        return out

    def values(self) -> dict:
        """Every value, for agentsorg's own use (e.g. sending an approved email). Never for prompts."""
        return {k: v.get("value", "") for k, v in self._load().items()}

    def names_for(self, dept: str) -> list[tuple[str, str]]:
        return [(k, v.get("note", "")) for k, v in sorted(self._load().items())
                if k not in CLAUDE_KEYS and self._applies(v, dept)]

    def redact(self, text: str) -> str:
        if not text:
            return text
        for k, v in self._load().items():
            val = v.get("value", "")
            if len(val) >= MIN_MASK and val in text:
                text = text.replace(val, f"[{k}]")
        return text
