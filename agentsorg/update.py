"""Updates: see whether a newer agents.org is out, and install it with the same installer as the site.

Nothing is checked in the background. The site is only asked when you run `agentsorg update` or press
"Check for updates" in Settings, and the request carries nothing but the usual HTTP headers.
"""
from __future__ import annotations

import os
import platform
import subprocess
from pathlib import Path

from . import __version__

SITE = os.environ.get("AGENTSORG_SITE", "https://agentsorg.sahilsidhu.pro").rstrip("/")
WINDOWS = platform.system() == "Windows"


def parse(v: str) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in str(v).strip().lstrip("v").split("."))
    except ValueError:
        return ()


def check(timeout: float = 8.0) -> dict:
    """{current, latest, newer} from the site's latest.json, or an `error` when it can't be reached."""
    import httpx
    out = {"current": __version__, "latest": None, "newer": False}
    try:
        r = httpx.get(f"{SITE}/latest.json", timeout=timeout, follow_redirects=True)
        r.raise_for_status()
        latest = str(r.json()["version"])
    except Exception as e:                       # offline, site down, bad JSON: say so, change nothing
        return {**out, "error": f"couldn't reach {SITE} to check for updates ({type(e).__name__})"}
    if not parse(latest):
        return {**out, "error": f"the site gave an unreadable version: {latest!r}"}
    return {**out, "latest": latest, "newer": parse(latest) > parse(__version__)}


def installer_command() -> list[str]:
    """The one-line install from the site. It upgrades in place and restarts running company services."""
    if WINDOWS:
        return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", f"irm {SITE}/install.ps1 | iex"]
    return ["sh", "-c", f"curl -fsSL {SITE}/install.sh | sh"]


def _windows_window() -> list[str]:
    # The running agentsorg.exe can't be replaced while it runs, so the installer gets its own window,
    # starts after we exit, and stays open so you can read the result.
    script = (f"Start-Sleep 2; try {{ irm {SITE}/install.ps1 | iex }} "
              f"catch {{ Write-Host $_ -ForegroundColor Red }}; Read-Host 'Press Enter to close'")
    return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script]


def run_here() -> int:
    """For `agentsorg update` in a terminal. macOS/Linux: run the installer now and return its exit code.
    Windows: open it in a new window (returns -1, meaning "started elsewhere")."""
    if WINDOWS:
        subprocess.Popen(_windows_window(), creationflags=subprocess.CREATE_NEW_CONSOLE)
        return -1
    return subprocess.run(installer_command()).returncode


def start_detached(log: Path) -> str:
    """For the dashboard: start the installer on its own, so it outlives a service restart."""
    if WINDOWS:
        subprocess.Popen(_windows_window(), creationflags=subprocess.CREATE_NEW_CONSOLE)
        return "The installer opened in a new window."
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "ab") as f:
        subprocess.Popen(["sh", "-c", f"sleep 2; curl -fsSL {SITE}/install.sh | sh"],
                         stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
    return f"The installer is running (output in {log})."
