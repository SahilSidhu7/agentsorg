"""Isolated headless `claude -p` runner (pattern from ethanplusai/harvey).

Token savers:
- `--system-prompt` replaces Claude Code's large default system prompt with our small one.
- `--tools ""` removes every tool schema for text-only departments.
- `--setting-sources ""` + `--strict-mcp-config` + `--disable-slash-commands` + stripped env
  => no CLAUDE.md, hooks, plugins, skills or MCP servers get loaded into the context.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

REQUIRED_FLAGS = ["--output-format", "--tools", "--strict-mcp-config", "--setting-sources",
                  "--disable-slash-commands", "--no-session-persistence", "--system-prompt", "--model"]
STRIP_ENV = ["CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT", "CLAUDE_PROJECT_DIR",
             "CLAUDE_ENV_FILE", "CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD", "CLAUDE_CODE_SIMPLE",
             "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_HOST_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION",
             "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_AUTO_RETRY_ACTIVE", "OLDPWD"]


# relative to the agent's working folder (its department folder)
PROTECTED = ["Edit(instructions.md)", "Write(instructions.md)", "Edit(skills/**)", "Write(skills/**)"]


@dataclass
class RunResult:
    text: str
    ok: bool
    model: str
    usage: list[dict] = field(default_factory=list)   # one row per model actually used
    cost_usd: float = 0.0
    duration_ms: int = 0
    num_turns: int = 0
    error: str = ""

    @property
    def tokens(self) -> int:
        return sum(u["input"] + u["output"] + u["cache_create"] for u in self.usage)


def find_claude(configured: str = "") -> str | None:
    if configured and Path(configured).expanduser().exists():
        return str(Path(configured).expanduser())
    for name in ("claude.exe", "claude"):
        if p := shutil.which(name):
            return p
    home_bin = Path.home() / ".local" / "bin"
    for name in ("claude.exe", "claude"):
        if (home_bin / name).exists():
            return str(home_bin / name)
    return None


def supports(exe: str, flag: str) -> bool:
    try:
        return flag in subprocess.run([exe, "--help"], capture_output=True, text=True, encoding="utf-8",
                                      errors="replace", timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return False


def check_cli(exe: str) -> list[str]:
    """Return list of missing required flags ([] = ok)."""
    out = subprocess.run([exe, "--help"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                         timeout=60).stdout
    return [f for f in REQUIRED_FLAGS if f not in out]


# What an agent's process inherits from the server. Everything else (other tokens, cloud credentials, the
# server's own settings) stays out; the company's scoped keys are added on top (envvault.py).
ENV_KEEP = {"PATH", "HOME", "USER", "USERNAME", "LOGNAME", "SHELL", "LANG", "LANGUAGE", "TERM", "TZ", "TMPDIR",
            "TEMP", "TMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES",
            "PROGRAMFILES(X86)", "PROGRAMW6432", "COMMONPROGRAMFILES", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC",
            "PATHEXT", "HOMEDRIVE", "HOMEPATH", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS",
            "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR", "CLAUDE_CONFIG_DIR",
            "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
            "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS", "REQUESTS_CA_BUNDLE"}
ENV_KEEP_PREFIX = ("LC_", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_")


def runtime_env(cwd: str, extra: dict | None = None) -> dict:
    env = {k: v for k, v in os.environ.items()
           if (k.upper() in ENV_KEEP or k in ENV_KEEP or k.startswith(ENV_KEEP_PREFIX)) and k not in STRIP_ENV}
    env["PATH"] = clean_path(env.get("PATH", ""))
    env.update(extra or {})                      # the company's keys (see envvault.py)
    env["PWD"] = cwd
    env["DISABLE_AUTOUPDATER"] = "1"
    return env


def clean_path(path: str) -> str:
    """PATH without agentsorg's own virtualenv: agents and tests must use the project's tools, not ours."""
    norm = lambda x: os.path.normcase(os.path.normpath(x)) if x else ""
    sub = "Scripts" if os.name == "nt" else "bin"
    own = {norm(os.path.join(sys.prefix, sub))}
    if os.environ.get("VIRTUAL_ENV"):
        own.add(norm(os.path.join(os.environ["VIRTUAL_ENV"], sub)))
    if sys.prefix == sys.base_prefix and not os.environ.get("VIRTUAL_ENV"):
        return path                              # not in a venv: nothing of ours to hide
    return os.pathsep.join(p for p in path.split(os.pathsep) if norm(p) not in own)


def _rule_paths(path: str) -> list[str]:
    """`//abs/path` forms Claude Code matches. On Windows it uses POSIX-style `//c/Users/...`; add both forms."""
    p = Path(path).expanduser().as_posix()
    out = [p.lstrip("/")]
    m = re.match(r"^([A-Za-z]):/(.*)$", p)
    if m:
        out = [f"{m.group(1).lower()}/{m.group(2)}", f"{m.group(1).upper()}:/{m.group(2)}"]
    return [x.rstrip("/") for x in out]


def deny_path(path: str) -> list[str]:
    """Claude Code permission rules that stop Edit/Write anywhere under an absolute folder."""
    return [r for p in _rule_paths(path) for r in (f"Edit(//{p}/**)", f"Write(//{p}/**)")]


def deny_read(path: str) -> list[str]:
    """Rules that stop Read (and the read-only search tools, which follow Read rules) under a folder."""
    return [r for p in _rule_paths(path) for r in (f"Read(//{p}/**)", f"Edit(//{p}/**)", f"Write(//{p}/**)")]


def secret_paths(home: Path | None = None) -> list[str]:
    """Never readable by any agent: the company's data (keys, token, db), mods (server code), Claude/ssh creds."""
    h = Path.home()
    paths = [h / ".claude", h / ".ssh", h / ".config" / "agentsorg", h / ".aws", h / ".gnupg"]
    if home:
        paths += [Path(home) / "data", Path(home) / "mods"]
    return [str(p) for p in paths]


def sandbox_available() -> bool:
    """Claude Code's Bash sandbox: macOS (seatbelt) or Linux with bubblewrap + socat. Not on Windows."""
    if sys.platform == "darwin":
        return True
    return sys.platform.startswith("linux") and bool(shutil.which("bwrap")) and bool(shutil.which("socat"))


def build_command(exe: str, model: str, system: str, tools: list[str], max_turns: int, effort: str = "",
                  add_dirs: list[str] | None = None, read_only: list[str] | None = None,
                  secrets: list[str] | None = None, sandbox: bool = False) -> list[str]:
    cmd = [exe, "-p", "--output-format", "json", "--model", model,
           "--system-prompt", system,
           "--strict-mcp-config", "--setting-sources", "",
           "--disable-slash-commands", "--no-session-persistence",
           "--max-turns", str(max(1, max_turns))]
    if effort:   # Haiku's adaptive thinking was ~70% of its output tokens on routine tasks; `low` halves output
        cmd += ["--effort", effort]
    if tools:
        cmd += ["--tools", ",".join(tools), "--allowedTools", ",".join(tools), "--permission-mode", "acceptEdits"]
        for d in add_dirs or []:
            cmd += ["--add-dir", d]
        deny = [r for d in secrets or [] for r in deny_read(d)]
        if any(t in tools for t in ("Edit", "Write")):
            deny += [*PROTECTED, *[r for d in read_only or [] for r in deny_path(d)]]
        if deny:
            cmd += ["--disallowedTools", *deny]
        if sandbox and "Bash" in tools:      # Bash ignores the rules above unless it runs inside the sandbox
            cmd += ["--settings", json.dumps({"sandbox": {"enabled": True, "autoAllowBashIfSandboxed": True,
                                                          "allowUnsandboxedCommands": False}})]
    else:
        cmd += ["--tools", ""]
    return cmd


def _result_line(raw: str) -> dict | None:
    """The CLI prints one JSON result object; warnings may follow it, so scan from the end."""
    for line in reversed(raw.strip().splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            p = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(p, dict) and p.get("type") == "result":
            return p
    return None


def parse_payload(raw: str, model: str) -> RunResult:
    p = _result_line(raw) if raw.strip() else None
    if not isinstance(p, dict) or p.get("type") != "result":
        return RunResult(text=raw.strip(), ok=False, model=model, error="unparseable output")
    rows = []
    mu = p.get("modelUsage") or {}
    if mu:
        for m, u in mu.items():
            rows.append({"model": m, "input": int(u.get("inputTokens", 0)), "output": int(u.get("outputTokens", 0)),
                         "cache_read": int(u.get("cacheReadInputTokens", 0)),
                         "cache_create": int(u.get("cacheCreationInputTokens", 0)),
                         "cost_usd": float(u.get("costUSD", 0.0))})
    else:
        u = p.get("usage") or {}
        rows.append({"model": model, "input": int(u.get("input_tokens", 0)), "output": int(u.get("output_tokens", 0)),
                     "cache_read": int(u.get("cache_read_input_tokens", 0)),
                     "cache_create": int(u.get("cache_creation_input_tokens", 0)),
                     "cost_usd": float(p.get("total_cost_usd") or 0.0)})
    sub = str(p.get("subtype") or "")
    err = bool(p.get("is_error")) or sub.startswith("error")
    text = str(p.get("result") or "")
    why = "ran out of working steps (max_turns)" if sub == "error_max_turns" else text or sub
    return RunResult(text=text, ok=not err, model=model, usage=rows,
                     cost_usd=float(p.get("total_cost_usd") or 0.0),
                     duration_ms=int(p.get("duration_ms") or 0), num_turns=int(p.get("num_turns") or 0),
                     error=why if err else "")


def kill_tree(proc: subprocess.Popen) -> None:
    """Stop a claude run and everything it started (Bash children included)."""
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=30)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


def run_claude(exe: str, prompt: str, *, model: str, system: str, tools: list[str] | None = None,
               max_turns: int = 1, cwd: Path | None = None, timeout: int = 900, effort: str = "",
               add_dirs: list[str] | None = None, read_only: list[str] | None = None,
               env: dict | None = None, secrets: list[str] | None = None, sandbox: bool = False,
               on_start: Callable[[subprocess.Popen], None] | None = None) -> RunResult:
    """Blocking call. Prompt via stdin. Text-only calls run in an empty temp dir.
    `on_start` receives the process so the caller can kill it (cancel, shutdown)."""
    tools = tools or []
    tmp = None
    if cwd is None:
        tmp = tempfile.TemporaryDirectory(prefix="agentsorg-")
        cwd = Path(tmp.name)
    proc = None
    try:
        cmd = build_command(exe, model, system, tools, max_turns, effort, add_dirs, read_only, secrets, sandbox)
        group = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
                 else {"start_new_session": True})
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", errors="replace", cwd=str(cwd),
                                env=runtime_env(str(cwd), env), **group)
        if on_start:
            on_start(proc)
        try:
            out, err = proc.communicate(prompt, timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_tree(proc)
            proc.communicate()
            return RunResult(text="", ok=False, model=model, error="timeout")
        res = parse_payload(out, model)
        if not res.ok and not res.error:
            res.error = (err or "claude exited %d" % proc.returncode)[-500:]
        return res
    except OSError as e:
        return RunResult(text="", ok=False, model=model, error=f"could not start claude: {e}")
    finally:
        if proc and proc.poll() is None:
            kill_tree(proc)
        if tmp:
            tmp.cleanup()
