"""Join a company running on another machine over SSH: dashboard via tunnel, or the terminal console."""
from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import time
import webbrowser

# agentsorg may not be on PATH for non-interactive ssh sessions (pipx -> ~/.local/bin)
REMOTE_BINS = ["~/.local/bin/agentsorg", "agentsorg", "%USERPROFILE%\\.local\\bin\\agentsorg.exe"]


def _ssh() -> str:
    s = shutil.which("ssh")
    if not s:
        sys.exit("ssh client not found. Install OpenSSH.")
    return s


def _target(remote_home: str | None, company: str | None) -> str:
    return (f" --home {remote_home}" if remote_home else "") + (f" --company {company}" if company else "")


def remote_info(host: str, remote_home: str | None, ssh_opts: list[str], bins: list[str] | None = None,
                company: str | None = None) -> tuple[str, dict]:
    """Find a working remote agentsorg and return (bin, info{port, token, home, running})."""
    home = _target(remote_home, company)
    last = ""
    for b in bins or REMOTE_BINS:
        p = subprocess.run([_ssh(), *ssh_opts, "-o", "BatchMode=yes", host, f"{b}{home} info --json"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        if p.returncode == 0:
            try:
                return b, json.loads(p.stdout.strip().splitlines()[-1])
            except (json.JSONDecodeError, IndexError):
                last = p.stdout[-300:]
                continue
        last = (p.stderr or p.stdout)[-300:]
    sys.exit(f"Could not reach agentsorg on {host}.\n{last.strip()}\n"
             f"Check: `ssh {host}` works without a password prompt, and agentsorg is installed + initialised there.")


def _free_port(start: int) -> int:
    for p in range(start, start + 50):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:
                return p
    raise RuntimeError("no free local port")


def join_dashboard(host: str, remote_home: str | None = None, local_port: int | None = None,
                   ssh_opts: list[str] | None = None, open_browser: bool = True, bins: list[str] | None = None,
                   company: str | None = None) -> None:
    ssh_opts = ssh_opts or []
    b, info = remote_info(host, remote_home, ssh_opts, bins, company)
    if not info.get("running"):
        print(f"Company on {host} is not running. Starting it there as a service…")
        subprocess.run([_ssh(), *ssh_opts, host, f"{b}{_target(remote_home, company)} service install"], timeout=120)
        time.sleep(3)
        b, info = remote_info(host, remote_home, ssh_opts, [b], company)
    lp = _free_port(local_port or int(info["port"]))
    tunnel = subprocess.Popen([_ssh(), *ssh_opts, "-N", "-o", "ExitOnForwardFailure=yes",
                               "-L", f"{lp}:127.0.0.1:{info['port']}", host])
    for _ in range(40):                      # wait for the tunnel
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", lp)) == 0:
                break
        if tunnel.poll() is not None:
            sys.exit("ssh tunnel failed to start")
        time.sleep(0.25)
    url = f"http://127.0.0.1:{lp}/?token={info['token']}"
    print(f"Joined {info.get('company', 'company')} on {host} ({info['home']})")
    print(f"Dashboard: {url}\nPress Ctrl+C to leave (closes the tunnel; the company keeps running).", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        tunnel.wait()
    except KeyboardInterrupt:
        pass
    finally:
        tunnel.terminate()


def join_terminal(host: str, remote_home: str | None = None, ssh_opts: list[str] | None = None,
                  bins: list[str] | None = None, company: str | None = None) -> int:
    ssh_opts = ssh_opts or []
    b, _ = remote_info(host, remote_home, ssh_opts, bins, company)
    return subprocess.call([_ssh(), *ssh_opts, "-t", host, f"{b}{_target(remote_home, company)} console"])
