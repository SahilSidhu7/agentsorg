"""Safe file writes, owner-only permissions and logging."""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import tempfile
import threading
from pathlib import Path

LOG_BYTES = 5 * 1024 * 1024
LOG_KEEP = 3


def atomic_write(path: Path | str, data: str | bytes, mode: int | None = None, backup: bool = False) -> None:
    """Write via a unique temp file + fsync + rename: readers see the old file or the new one, never half of it.
    `mode` sets permissions before any data is written (e.g. 0o600 for secrets)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = data.encode("utf-8") if isinstance(data, str) else data
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        if mode is not None:
            try:
                os.chmod(tmp, mode)
            except OSError:
                pass
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        if backup and path.exists():
            try:
                bak = path.with_name(path.name + ".bak")
                bak.write_bytes(path.read_bytes())
                if mode is not None:
                    os.chmod(bak, mode)
            except OSError:
                pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def private_file(path: Path | str) -> None:
    """Owner-only (0600) where the OS supports it."""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


class _ToLog:
    """stdout/stderr replacement in service mode: lines go into the rotating log."""

    def __init__(self, logger: logging.Logger, level: int):
        self.logger, self.level, self._buf, self._lock = logger, level, "", threading.Lock()

    def write(self, s: str) -> int:
        with self._lock:
            self._buf += s
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                if line.strip():
                    self.logger.log(self.level, line)
        return len(s)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False


def setup_logging(home: Path, logfile: str | None = None) -> Path:
    """Rotating log at data/server.log (or `logfile`), owner-only. In service mode print() goes there too."""
    path = Path(logfile) if logfile else Path(home) / "data" / "server.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.touch()
    private_file(path)
    root = logging.getLogger("agentsorg")
    if not any(isinstance(h, logging.handlers.RotatingFileHandler) for h in root.handlers):
        h = logging.handlers.RotatingFileHandler(path, maxBytes=LOG_BYTES, backupCount=LOG_KEEP, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        root.addHandler(h)
        root.setLevel(logging.INFO)
    if logfile:
        sys.stdout = _ToLog(root, logging.INFO)
        sys.stderr = _ToLog(root, logging.WARNING)
    return path
