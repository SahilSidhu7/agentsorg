"""Daily upkeep: database backup (consistent copy of the live WAL db), pruning old bulky text, and the hook the
heartbeat uses to run any daily/weekly job exactly once per period."""
from __future__ import annotations

import logging
import shutil
import time
from datetime import datetime
from pathlib import Path

from .state import today

log = logging.getLogger("agentsorg")
KEEP_BACKUPS = 14


def backup_dir(org) -> Path:
    return org.home / "data" / "backups"


def backup(org, dest: Path | None = None) -> Path:
    dest = dest or backup_dir(org) / f"org-{datetime.now():%Y%m%d-%H%M%S}.db"
    org.s.backup(dest)
    for f in ("company.yaml",):                      # the config next to it, so a restore is complete
        src = org.home / f
        if src.exists():
            shutil.copy2(src, dest.with_suffix(".company.yaml"))
    olds = sorted(backup_dir(org).glob("org-*.db"))
    for old in olds[:-KEEP_BACKUPS]:
        old.unlink(missing_ok=True)
        old.with_suffix(".company.yaml").unlink(missing_ok=True)
    return dest


def restore(org_home: Path, src: Path) -> Path:
    """Replace the database with a backup (the server must be stopped). The current db is kept aside first."""
    db = org_home / "data" / "org.db"
    aside = db.with_name(f"org.before-restore-{datetime.now():%Y%m%d-%H%M%S}.db")
    if db.exists():
        shutil.copy2(db, aside)
    for ext in ("-wal", "-shm"):
        Path(str(db) + ext).unlink(missing_ok=True)
    shutil.copy2(src, db)
    return aside


def prune(org) -> dict:
    """Drop bulky text nobody needs anymore. Final deliverables and anything newer than the retention stay."""
    days = int(org.c.budget.get("keep_days", 90))
    cut = time.time() - days * 86400
    n = org.s.xc("UPDATE tasks SET result=NULL WHERE updated<? AND COALESCE(final,0)=0 AND result IS NOT NULL "
                 "AND status IN ('done','cached','failed','cancelled','archived')", (cut,))
    keep = {r["output_path"] for r in org.s.q("SELECT output_path FROM tasks WHERE final=1 AND output_path IS NOT NULL")}
    files = 0
    for d in org.c.depts:
        for f in (org.c.dept_dir(d) / "outputs").glob("*.md"):
            try:
                if f.stat().st_mtime < cut and str(f) not in keep:
                    f.unlink()
                    files += 1
            except OSError:
                pass
    return {"results": n, "files": files}


def once(org, key: str, period: str = "day"):
    """True the first time it's asked in this period (day / ISO week); records it immediately."""
    stamp = today() if period == "day" else datetime.now().strftime("%G-W%V")
    if org.s.get(key) == stamp:
        return False
    org.s.set(key, stamp)
    return True


def daily_jobs(org) -> list:
    jobs = []
    if once(org, "last_backup_day"):
        jobs.append(("backup", lambda: (backup(org), prune(org))))
    return jobs
