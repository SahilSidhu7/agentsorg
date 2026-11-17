"""Run a company as a background service: systemd --user (Linux), launchd (macOS), Task Scheduler (Windows).

Each company gets its own service, named after it (agentsorg-<company>), so several can run on one machine.
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from .runtime import find_claude

SYSTEM = platform.system()
LEGACY = "agentsorg"          # the single service name used before 0.3


def unit_name(company: str) -> str:
    return f"agentsorg-{company}"


def _python() -> str:
    exe = Path(sys.executable)
    if SYSTEM == "Windows":            # pythonw = no console window for the background task
        w = exe.with_name("pythonw.exe")
        if w.exists():
            return str(w)
    return str(exe)


def _cmd(home: Path, port: int, host: str) -> list[str]:
    return [_python(), "-m", "agentsorg", "--home", str(home), "start", "--no-browser", "--port", str(port),
            "--host", host, "--log", str(home / "data" / "server.log")]


def _run(cmd: list[str]) -> tuple[int, str]:
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return p.returncode, (p.stdout + p.stderr).strip()


def _path_env() -> str:
    """PATH for the service, including the claude CLI's folder (services don't load your shell profile)."""
    dirs = []
    if c := find_claude():
        dirs.append(str(Path(c).parent))
    dirs += [str(Path(sys.executable).parent), str(Path.home() / ".local" / "bin")]
    return os.pathsep.join(dict.fromkeys(dirs + os.environ.get("PATH", "").split(os.pathsep)))


# ---------- Linux: systemd --user ----------
UNIT_DIR = Path.home() / ".config" / "systemd" / "user"


def _unit_path(name: str) -> Path:
    return UNIT_DIR / f"{name}.service"


def _unit(home: Path, port: int, host: str) -> str:
    cmd = " ".join(f'"{c}"' if " " in c else c for c in _cmd(home, port, host))
    return f"""[Unit]
Description=agents.org company {home.name} ({home})
After=network-online.target

[Service]
ExecStart={cmd}
Environment=PATH={_path_env()}
Environment=PYTHONUNBUFFERED=1
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
"""


def _retire_legacy_linux(home: Path) -> str:
    """The pre-0.3 `agentsorg` unit served one company; if it served this one, replace it."""
    p = _unit_path(LEGACY)
    if p.exists() and f"--home {home}" in p.read_text(encoding="utf-8"):
        _run(["systemctl", "--user", "disable", "--now", LEGACY])
        p.unlink(missing_ok=True)
        return "retired the old single `agentsorg` service (it served this company)\n"
    return ""


def _linux_install(home: Path, name: str, port: int, host: str) -> str:
    UNIT_DIR.mkdir(parents=True, exist_ok=True)
    note = _retire_legacy_linux(home)
    _unit_path(name).write_text(_unit(home, port, host))
    for c in (["systemctl", "--user", "daemon-reload"], ["systemctl", "--user", "enable", "--now", name]):
        rc, o = _run(c)
        if rc:
            return f"systemd failed: {o}"
    rc, _ = _run(["loginctl", "enable-linger", os.environ.get("USER", "")])
    linger = ("keeps running after you log out" if rc == 0 else
              f"run `sudo loginctl enable-linger {os.environ.get('USER', '$USER')}` to keep it running after logout")
    return f"{note}installed {_unit_path(name)} ({linger})"


def _linux_uninstall(name: str) -> str:
    _run(["systemctl", "--user", "disable", "--now", name])
    _unit_path(name).unlink(missing_ok=True)
    _run(["systemctl", "--user", "daemon-reload"])
    return f"removed systemd user service {name}"


def _linux_status(name: str) -> str:
    return _run(["systemctl", "--user", "--no-pager", "status", name])[1] or "not installed"


# ---------- macOS: launchd ----------
AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"


def _plist(name: str) -> Path:
    return AGENTS_DIR / f"org.{name}.plist"


def _mac_install(home: Path, name: str, port: int, host: str) -> str:
    legacy = AGENTS_DIR / "org.agentsorg.plist"
    note = ""
    if legacy.exists() and str(home) in legacy.read_text(encoding="utf-8"):
        _run(["launchctl", "unload", "-w", str(legacy)])
        legacy.unlink(missing_ok=True)
        note = "retired the old single agentsorg launch agent\n"
    args = "".join(f"<string>{c}</string>" for c in _cmd(home, port, host))
    AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    _plist(name).write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>org.{name}</string>
<key>ProgramArguments</key><array>{args}</array>
<key>EnvironmentVariables</key><dict><key>PATH</key><string>{_path_env()}</string></dict>
<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
</dict></plist>
""")
    _run(["launchctl", "unload", str(_plist(name))])
    rc, o = _run(["launchctl", "load", "-w", str(_plist(name))])
    return f"{note}installed {_plist(name)}" if rc == 0 else f"launchctl failed: {o}"


def _mac_uninstall(name: str) -> str:
    _run(["launchctl", "unload", "-w", str(_plist(name))])
    _plist(name).unlink(missing_ok=True)
    return f"removed launchd agent org.{name}"


def _mac_status(name: str) -> str:
    rc, o = _run(["launchctl", "list", f"org.{name}"])
    return o if rc == 0 else "not installed"


# ---------- Windows: Task Scheduler (at logon, restarts on failure, no time limit) ----------
def _ps(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _win_install(home: Path, name: str, port: int, host: str) -> str:
    c = _cmd(home, port, host)
    rc, o = _run(["schtasks", "/Query", "/TN", LEGACY, "/V", "/FO", "LIST"])
    note = ""
    if rc == 0 and str(home) in o:
        _run(["schtasks", "/End", "/TN", LEGACY])
        _run(["schtasks", "/Delete", "/TN", LEGACY, "/F"])
        note = "retired the old single agentsorg scheduled task\n"
    script = (f"$a=New-ScheduledTaskAction -Execute {_ps(c[0])} -Argument {_ps(subprocess.list2cmdline(c[1:]))};"
              "$t=New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME;"
              "$s=New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 5 "
              "-RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
              "-StartWhenAvailable -MultipleInstances IgnoreNew;"
              f"Register-ScheduledTask -TaskName {_ps(name)} -Action $a -Trigger $t -Settings $s -Force | Out-Null;"
              f"Start-ScheduledTask -TaskName {_ps(name)}")
    rc, o = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script])
    if rc:
        return f"Register-ScheduledTask failed: {o}"
    return f"{note}installed Windows scheduled task '{name}' (starts at logon, restarts on failure, started now)"


def _win_uninstall(name: str) -> str:
    _run(["schtasks", "/End", "/TN", name])
    rc, o = _run(["schtasks", "/Delete", "/TN", name, "/F"])
    return f"removed scheduled task {name}" if rc == 0 else o


def _win_status(name: str) -> str:
    rc, o = _run(["schtasks", "/Query", "/TN", name, "/V", "/FO", "LIST"])
    if rc:
        return "not installed"
    keep = ("TaskName", "Status", "Last Run Time", "Last Result", "Task To Run")
    return "\n".join(l for l in o.splitlines() if l.split(":")[0].strip() in keep)


# ---------- public ----------
def show(home: Path, company: str, port: int, host: str = "127.0.0.1") -> str:
    """What `install` would set up, without changing anything."""
    name = unit_name(company)
    if SYSTEM == "Linux":
        return f"# {_unit_path(name)}\n{_unit(home, port, host)}"
    if SYSTEM == "Darwin":
        return f"# {_plist(name)}\nProgramArguments: {_cmd(home, port, host)}"
    return f"# Windows scheduled task '{name}' (at logon, no time limit, restarts on failure)\n" \
           f"{subprocess.list2cmdline(_cmd(home, port, host))}"


def install(home: Path, company: str, port: int, host: str = "127.0.0.1") -> str:
    name = unit_name(company)
    if SYSTEM == "Linux":
        if not shutil.which("systemctl"):
            return "systemctl not found: run `agentsorg start` under your init system, tmux or screen instead"
        return _linux_install(home, name, port, host)
    if SYSTEM == "Darwin":
        return _mac_install(home, name, port, host)
    if SYSTEM == "Windows":
        return _win_install(home, name, port, host)
    return f"unsupported OS {SYSTEM}"


def uninstall(company: str) -> str:
    name = unit_name(company)
    return {"Linux": _linux_uninstall, "Darwin": _mac_uninstall, "Windows": _win_uninstall}.get(
        SYSTEM, lambda n: "unsupported")(name)


def status(company: str) -> str:
    name = unit_name(company)
    return {"Linux": _linux_status, "Darwin": _mac_status, "Windows": _win_status}.get(
        SYSTEM, lambda n: "unsupported")(name)


def is_installed(company: str) -> bool:
    name = unit_name(company)
    if SYSTEM == "Linux":
        return _unit_path(name).exists()
    if SYSTEM == "Darwin":
        return _plist(name).exists()
    return _run(["schtasks", "/Query", "/TN", name])[0] == 0


def restart(company: str) -> str:
    name = unit_name(company)
    if SYSTEM == "Linux":
        return _run(["systemctl", "--user", "restart", name])[1] or f"restarted {name}"
    if SYSTEM == "Darwin":
        _run(["launchctl", "unload", str(_plist(name))])
        return _run(["launchctl", "load", "-w", str(_plist(name))])[1] or f"restarted {name}"
    _run(["schtasks", "/End", "/TN", name])
    return _run(["schtasks", "/Run", "/TN", name])[1]
