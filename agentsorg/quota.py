"""Read Claude subscription utilization (5h / 7d windows), same source as `/usage` in Claude Code.

Undocumented endpoint (pattern from ethanplusai/harvey). Returns None on any failure so the
budget falls back to call counting.

The endpoint rate-limits hard, and every company on a machine (plus tools like Harvey) shares one
subscription, so the last reading is kept in one machine-wide file: all processes read it, only one
fetches, a 429 backs off for the time the server asks, and a recent good reading keeps being used
(marked stale) instead of dropping to "unreadable".
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

import httpx

from .config import CONFIG_DIR

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_BETA = "oauth-2025-04-20"
TTL_OK = 300                 # refetch a good reading after 5 min
TTL_FAIL = 120               # retry after a plain failure
BACKOFF_429 = 900            # rate limited and no Retry-After: wait 15 min
STALE_OK = 3 * 3600          # a good reading this old is still better than call caps
CACHE = CONFIG_DIR / "quota.json"


def _cred_paths() -> list[Path]:
    paths = []
    if d := os.environ.get("CLAUDE_CONFIG_DIR"):
        paths.append(Path(d) / ".credentials.json")
    paths.append(Path.home() / ".claude" / ".credentials.json")
    return paths


def _oauth() -> tuple[str | None, float | None]:
    """(access token, expiry epoch seconds or None)."""
    if tok := os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):   # long-lived token from `claude setup-token`
        return tok, None
    for p in _cred_paths():
        try:
            o = json.loads(p.read_text(encoding="utf-8")).get("claudeAiOauth") or {}
            if o.get("accessToken"):
                exp = o.get("expiresAt")
                return o["accessToken"], (exp / 1000 if exp else None)
        except (OSError, json.JSONDecodeError, AttributeError):
            continue
    return None, None


def _current(windows: dict | None, now: float) -> dict | None:
    """A reading of a window that has since reset says nothing about now: count that window as empty."""
    if not windows:
        return windows
    out = {}
    for name, w in windows.items():
        try:
            ended = datetime.fromisoformat(str(w.get("resets_at") or "").replace("Z", "+00:00")).timestamp() <= now
        except ValueError:
            ended = False
        out[name] = {**w, "utilization": 0.0} if ended else w
    return out


def oauth_token() -> str | None:
    return _oauth()[0]


class Quota:
    def __init__(self, cache: Path | None = None):
        self.cache = cache or CACHE
        self._mem: dict | None = None

    # ---------- shared cache ----------
    def _load(self) -> dict:
        try:
            return json.loads(self.cache.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return self._mem or {}

    def _save(self, d: dict) -> None:
        self._mem = d
        try:
            self.cache.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(d), encoding="utf-8")
            os.replace(tmp, self.cache)
        except OSError:
            pass

    # ---------- public ----------
    def status(self, force: bool = False) -> dict:
        """{'windows': {...} | None, 'age': seconds since the reading, 'stale': bool, 'reason': why not fresh}."""
        d = self._load()
        now = time.time()
        due = now >= d.get("next", 0)
        if force or due:
            d = self._refresh(d, now)
        win, at = _current(d.get("windows"), now), d.get("at", 0)
        usable = bool(win) and now - at < STALE_OK
        return {"windows": win if usable else None, "age": int(now - at) if win else None,
                "stale": bool(win) and now - at > TTL_OK * 2, "reason": d.get("reason", "")}

    def utilization(self, force: bool = False) -> dict | None:
        """{'five_hour': {'utilization': 12.0, 'resets_at': '...'}, 'seven_day': {...}} or None."""
        return self.status(force)["windows"]

    def worst(self) -> float | None:
        u = self.utilization()
        if not u:
            return None
        return max(w["utilization"] for w in u.values())

    def _refresh(self, d: dict, now: float) -> dict:
        d = {**d, "next": now + TTL_FAIL}
        self._save(d)                                   # claim the fetch so parallel processes don't stampede
        val, reason, wait = self._fetch()
        if val:
            d.update(windows=val, at=now, reason="", next=now + TTL_OK)
        else:
            d.update(reason=reason, next=now + wait)
        self._save(d)
        return d

    def _fetch(self) -> tuple[dict | None, str, int]:
        tok, exp = _oauth()
        if not tok:
            return None, "no Claude login found on this machine", TTL_FAIL
        if exp and exp < time.time():
            # Claude Code refreshes the token whenever it runs; the next agent call fixes this
            return None, "Claude login token expired; it refreshes on the next agent run", TTL_FAIL
        try:
            r = httpx.get(USAGE_URL, headers={"Authorization": f"Bearer {tok}", "anthropic-beta": OAUTH_BETA},
                          timeout=10)
        except Exception as e:
            return None, f"usage check failed: {type(e).__name__}", TTL_FAIL
        if r.status_code == 429:
            try:
                wait = int(r.headers.get("retry-after", "")) or BACKOFF_429
            except ValueError:
                wait = BACKOFF_429
            return None, "usage check rate-limited by Anthropic", max(wait, 60)
        if r.status_code == 401:
            return None, "Claude login token rejected; it refreshes on the next agent run", TTL_FAIL
        if r.status_code != 200:
            return None, f"usage check returned HTTP {r.status_code}", TTL_FAIL
        try:
            data = r.json()
        except ValueError:
            return None, "usage check returned bad JSON", TTL_FAIL
        out = {}
        for key in ("five_hour", "seven_day"):
            v = data.get(key)
            if isinstance(v, dict) and v.get("utilization") is not None:
                out[key] = {"utilization": float(v["utilization"]),
                            "resets_at": v.get("resets_at") or v.get("resetsAt") or ""}
        return (out or None), ("" if out else "usage check returned no windows"), TTL_OK
