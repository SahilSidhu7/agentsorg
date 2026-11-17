"""The company's own services on this machine (systemd units, timers, docker containers):
status, start / stop / restart and logs, from the CLI and the dashboard.

company.yaml:
  services:
    harvey-run: {kind: systemd, unit: harvey-run.service, scope: system, dept: sales, about: "Cold email heartbeat"}
    chatbot-ingest: {kind: systemd, unit: chatbot-ingest.timer, scope: user}
    booking-api: {kind: docker, container: deploy-api-1}

System units and docker usually need root to control. `agentsorg services permissions` prints a sudoers file that
allows exactly start/stop/restart of the listed units (and docker containers) for this user, nothing else.
Status and logs only need read access (the `adm` / `systemd-journal` group for system units).
"""
from __future__ import annotations

import getpass
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass

ACTIONS = ("start", "stop", "restart")
SHOW_PROPS = ["Description", "ActiveState", "SubState", "ActiveEnterTimestamp", "MainPID", "MemoryCurrent",
              "UnitFileState", "NextElapseUSecRealtime", "Result"]


@dataclass
class Svc:
    name: str
    kind: str = "systemd"          # systemd | docker
    unit: str = ""                 # systemd unit (name.service / name.timer)
    scope: str = "system"          # system | user
    container: str = ""            # docker container name
    dept: str = ""
    about: str = ""


def _run(cmd: list[str], timeout: int = 20) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except FileNotFoundError:
        return 127, f"{cmd[0]} not found"
    except subprocess.TimeoutExpired:
        return 124, "timed out"


TARGET_RE = re.compile(r"^[A-Za-z0-9@_.:][A-Za-z0-9@_.:-]{0,127}$")   # unit / container names; no spaces,
                                                                    # commas, globs or leading '-' (sudoers-safe)


def valid_target(name: str) -> bool:
    return bool(name) and bool(TARGET_RE.match(name)) and ".." not in name


def check_svc(kind: str, unit: str, container: str, scope: str) -> None:
    if kind not in ("systemd", "docker"):
        raise ValueError("kind must be systemd or docker")
    if scope not in ("system", "user"):
        raise ValueError("scope must be system or user")
    target = container if kind == "docker" else unit
    if not valid_target(target):
        raise ValueError(f"{'container' if kind == 'docker' else 'unit'} name {target!r} is not allowed: use letters, "
                         "digits and @ _ . : - only")


def _systemctl(scope: str) -> list[str]:
    return ["systemctl", "--user"] if scope == "user" else ["systemctl"]


def supported() -> bool:
    return platform.system() == "Linux" and bool(shutil.which("systemctl"))


class Services:
    def __init__(self, company):
        self.c = company

    # ---------- registry (company.yaml -> services:) ----------
    def all(self) -> dict[str, Svc]:
        out = {}
        for n, v in (self.c.raw.get("services") or {}).items():
            v = v or {}
            out[n] = Svc(name=n, kind=v.get("kind", "systemd"), unit=v.get("unit", ""), scope=v.get("scope", "system"),
                         container=v.get("container", ""), dept=v.get("dept", ""), about=v.get("about", ""))
        return out

    def get(self, name: str) -> Svc:
        s = self.all().get(name)
        if not s:
            raise KeyError(f"no service named {name!r} (see: agentsorg services)")
        return s

    def add(self, name: str, **fields) -> Svc:
        name = re.sub(r"[^a-z0-9_.-]+", "-", name.lower()).strip("-")
        clean = {k: v for k, v in fields.items() if v}
        if not name:
            raise ValueError("service needs a name")
        if clean.get("kind", "systemd") == "systemd" and clean.get("unit") and "." not in clean["unit"]:
            clean["unit"] += ".service"
        check_svc(clean.get("kind", "systemd"), clean.get("unit", ""), clean.get("container", ""),
                  clean.get("scope", "system"))
        self.c.raw.setdefault("services", {})[name] = clean
        self.c.save()
        return self.get(name)

    def remove(self, name: str) -> None:
        self.get(name)
        self.c.raw["services"].pop(name, None)
        self.c.save()

    # ---------- state ----------
    @staticmethod
    def _ok(s: Svc) -> bool:
        return valid_target(s.container if s.kind == "docker" else s.unit) and s.scope in ("system", "user")

    def status(self, s: Svc) -> dict:
        if not self._ok(s):
            return {"state": "invalid", "detail": "name not allowed; remove and add it again", "since": "", "next": ""}
        if s.kind == "docker":
            rc, out = _docker(["inspect", "-f", "{{.State.Status}}|{{.State.StartedAt}}|{{.Config.Image}}", s.container])
            if rc != 0:
                return {"state": "unknown", "detail": _hint(out), "since": "", "next": ""}
            state, since, image = (out.split("|") + ["", "", ""])[:3]
            return {"state": "active" if state == "running" else state, "detail": image, "since": since[:19], "next": ""}
        if not supported():
            return {"state": "unknown", "detail": "systemd isn't available on this machine", "since": "", "next": ""}
        rc, out = _run([*_systemctl(s.scope), "show", s.unit, *[f"-p{p}" for p in SHOW_PROPS]])
        props = dict(l.split("=", 1) for l in out.splitlines() if "=" in l)
        if props.get("UnitFileState") == "" and props.get("ActiveState") == "inactive" and not props.get("Description"):
            return {"state": "missing", "detail": f"{s.unit} isn't installed", "since": "", "next": ""}
        state = props.get("ActiveState", "unknown")
        sub = props.get("SubState", "")
        mem = props.get("MemoryCurrent", "")
        detail = sub + (f" · {int(mem) // 2**20} MB" if mem.isdigit() else "")
        nxt = props.get("NextElapseUSecRealtime", "")
        return {"state": state, "detail": detail, "since": props.get("ActiveEnterTimestamp", ""),
                "next": nxt if nxt and nxt != "n/a" else "", "description": props.get("Description", "")}

    def logs(self, s: Svc, lines: int = 80) -> str:
        if not self._ok(s):
            return "this service's name isn't allowed; remove it and add it again"
        lines = max(10, min(int(lines), 1000))
        if s.kind == "docker":
            rc, out = _docker(["logs", "--tail", "200", s.container], timeout=30)
            return out if rc == 0 else _hint(out)
        unit = s.unit
        if unit.endswith(".timer"):                       # a timer's useful output is its service's
            unit = unit[: -len(".timer")] + ".service"
        scope = ["--user"] if s.scope == "user" else []
        rc, out = _run(["journalctl", *scope, "-u", unit, "-n", str(lines), "--no-pager", "-o", "short-iso"], timeout=30)
        if rc != 0 or "No journal files" in out or "insufficient permissions" in out.lower():
            return _hint(out) + "\n(to read system logs, add this user to the `adm` or `systemd-journal` group)"
        return out

    def control(self, s: Svc, action: str) -> tuple[bool, str]:
        if action not in ACTIONS:
            return False, f"unknown action {action!r}"
        if not self._ok(s):
            return False, "this service's name isn't allowed; remove it and add it again"
        if s.kind == "docker":
            rc, out = _docker([action, s.container], timeout=90)
        elif s.scope == "user":
            rc, out = _run([*_systemctl("user"), action, s.unit], timeout=90)
        else:
            rc, out = _run(["sudo", "-n", shutil.which("systemctl") or "/usr/bin/systemctl", action, s.unit], timeout=90)
        if rc == 0:
            return True, f"{ {'start': 'started', 'stop': 'stopped', 'restart': 'restarted'}[action]} {s.name}"
        if "password" in out.lower() or "sudo" in out.lower():
            return False, (f"needs a one-time permission to {action} {s.name}: run `agentsorg services permissions` "
                           f"on this machine and follow its instructions")
        return False, out[-300:] or f"{action} failed"

    def can_control(self, s: Svc) -> bool:
        if s.kind == "systemd" and s.scope == "user":
            return True
        tool = shutil.which("docker" if s.kind == "docker" else "systemctl") or ""
        target = s.container if s.kind == "docker" else s.unit
        rc, _ = _run(["sudo", "-n", "-l", tool, "restart", target])
        if rc == 0:
            return True
        return s.kind == "docker" and _run(["docker", "inspect", s.container])[0] == 0

    def overview(self) -> list[dict]:
        rows = []
        for s in self.all().values():
            rows.append({"name": s.name, "kind": s.kind, "unit": s.unit or s.container, "scope": s.scope, "dept": s.dept,
                         "about": s.about, **self.status(s)})
        return rows

    def facts(self) -> str:
        """One line per service for the CEO prompt (cheap: no logs)."""
        if not self.all():
            return ""
        rows = self.overview()
        by: dict[str, int] = {}
        for r in rows:
            by[r["state"]] = by.get(r["state"], 0) + 1
        lines = [f"SERVICES: {len(rows)} tracked (" + ", ".join(f"{n} {s}" for s, n in sorted(by.items()))
                 + "); on this machine; only the CEO human can start/stop them, from the dashboard:"]
        for r in rows:
            nxt = f", next run {r['next']}" if r["next"] else ""
            lines.append(f"- {r['name']} ({r['about'] or r['unit']}): {r['state']} {r['detail']}{nxt}".rstrip())
        return "\n".join(lines)

    # ---------- discovery ----------
    def discover(self, hints: list[str], keywords: list[str]) -> list[dict]:
        """systemd units (system + user) whose command or folder mentions one of `hints` (paths) or whose name
        matches one of `keywords`. Nothing is added; returns candidates."""
        if not supported():
            return []
        found, have = [], {s.unit for s in self.all().values()}
        for scope in ("system", "user"):
            rc, out = _run([*_systemctl(scope), "list-units", "--type=service,timer", "--all", "--no-legend",
                            "--plain", "--no-pager"])
            units = [l.split()[0] for l in out.splitlines() if l.strip() and not l.startswith("●")]
            for u in units:
                if u in have or re.match(r"(systemd-|dbus|user@|snap\.|getty|ssh|cron|polkit|udisks|NetworkManager|"
                                          r"ModemManager|wpa_|netplan|apport|unattended|fwupd|cups|avahi|gdm|"
                                          r"accounts-daemon|rsyslog|thermald|irqbalance|multipathd|packagekit|"
                                          r"power-profiles|colord|kerneloops|bluetooth|rtkit|upower|switcheroo|lvm2|"
                                          r"at-spi|gpg|keyboxd|dirmngr|pk-debconf|launchpadlib|nvidia|containerd|"
                                          r"docker\.service|smbd|nmbd|tailscaled|epmd|ollama)", u):
                    continue
                rc, show = _run([*_systemctl(scope), "show", u, "-pDescription", "-pExecStart", "-pWorkingDirectory",
                                 "-pFragmentPath"])
                text = show.lower()
                if u.startswith("agentsorg"):
                    continue                                  # the companies themselves, not their services
                hit = [h for h in hints if h and re.search(re.escape(h.lower().rstrip("/")) + r"(?=[/\s;'\"]|$)", text)] \
                    or [k for k in keywords if k.lower() in u.lower()]
                if hit:
                    desc = next((l.split("=", 1)[1] for l in show.splitlines() if l.startswith("Description=")), "")
                    found.append({"name": u.rsplit(".", 1)[0], "unit": u, "scope": scope, "about": desc,
                                  "why": hit[0]})
        timers = {f["unit"][: -len(".timer")] for f in found if f["unit"].endswith(".timer")}
        # a timer runs its oneshot service: track the timer (it shows the next run; logs come from the service)
        return [f for f in found if not (f["unit"].endswith(".service") and f["unit"][: -len(".service")] in timers)]

    # ---------- permissions ----------
    def permissions(self) -> tuple[str, str]:
        """(sudoers file text, install command) allowing exactly start/stop/restart of the listed system units and
        docker containers for this user."""
        user = getpass.getuser()
        sysctl = shutil.which("systemctl") or "/usr/bin/systemctl"
        docker = shutil.which("docker") or "/usr/bin/docker"
        cmds = []
        for s in self.all().values():
            if not self._ok(s):                       # never write an unchecked name into sudoers
                continue
            if s.kind == "systemd" and s.scope == "system":
                cmds += [f"{sysctl} {a} {s.unit}" for a in ACTIONS]
            elif s.kind == "docker":
                cmds += [f"{docker} {a} {s.container}" for a in ACTIONS]
                cmds += [f"{docker} inspect -f {DOCKER_FMT} {s.container}", f"{docker} logs --tail 200 {s.container}"]
        if not cmds:
            return "", ""
        name = f"agentsorg-{self.c.slug}"
        text = (f"# {name}: lets {user} start/stop/restart only these services without a password (agentsorg)\n"
                f"{user} ALL=(root) NOPASSWD: " + ", \\\n    ".join(cmds) + "\n")
        # validate first: a broken sudoers file can lock you out of sudo, so it is only installed if visudo accepts it
        install = (f"sudo visudo -cf {{file}} && "
                   f"sudo install -m 0440 -o root -g root {{file}} /etc/sudoers.d/{name}")
        return text, install


DOCKER_FMT = "{{.State.Status}}|{{.State.StartedAt}}|{{.Config.Image}}"


def _docker(args: list[str], timeout: int = 20) -> tuple[int, str]:
    """docker directly, else through the one-time sudoers permission (no password prompt, ever)."""
    rc, out = _run(["docker", *args], timeout=timeout)
    if rc != 0 and ("permission denied" in out.lower() or "connect to the docker" in out.lower()):
        rc2, out2 = _run(["sudo", "-n", shutil.which("docker") or "/usr/bin/docker", *args], timeout=timeout)
        if rc2 == 0:
            return rc2, out2
    return rc, out


def _hint(out: str) -> str:
    low = out.lower()
    if ("permission denied" in low or "connect to the docker" in low) and "docker" in low:
        return "no access to docker yet: install the one-time permission (`agentsorg services permissions`)"
    return out[-300:]
