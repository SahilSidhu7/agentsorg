"""SQLite state: tasks, usage, kb (+FTS5), reports, outbox, settings."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks(
  id INTEGER PRIMARY KEY, created REAL, updated REAL,
  dept TEXT, member TEXT, title TEXT, body TEXT,
  source TEXT,            -- ceo | dept | auto | next
  priority INTEGER DEFAULT 5,
  status TEXT,            -- queued | backlog | waiting | running | done | failed | cached | escalated
  tier INTEGER, tier_forced INTEGER DEFAULT 0, model TEXT,
  result TEXT, done_line TEXT, next_line TEXT,
  parent_id INTEGER, depends_on INTEGER, depth INTEGER DEFAULT 0,
  hash TEXT, attempts INTEGER DEFAULT 0, tokens INTEGER DEFAULT 0, error TEXT
);
CREATE INDEX IF NOT EXISTS tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS tasks_hash ON tasks(hash);
CREATE TABLE IF NOT EXISTS usage(
  id INTEGER PRIMARY KEY, ts REAL, day TEXT, task_id INTEGER, dept TEXT, model TEXT,
  input INTEGER, output INTEGER, cache_read INTEGER, cache_create INTEGER,
  cost_usd REAL, duration_ms INTEGER, auto INTEGER, ok INTEGER
);
CREATE INDEX IF NOT EXISTS usage_day ON usage(day);
CREATE TABLE IF NOT EXISTS kb(
  id INTEGER PRIMARY KEY, dept TEXT, title TEXT, body TEXT, tags TEXT, updated REAL, source_task INTEGER
);
CREATE VIRTUAL TABLE IF NOT EXISTS kb_fts USING fts5(title, body, tags, content='kb', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS kb_ai AFTER INSERT ON kb BEGIN
  INSERT INTO kb_fts(rowid, title, body, tags) VALUES (new.id, new.title, new.body, new.tags); END;
CREATE TRIGGER IF NOT EXISTS kb_ad AFTER DELETE ON kb BEGIN
  INSERT INTO kb_fts(kb_fts, rowid, title, body, tags) VALUES ('delete', old.id, old.title, old.body, old.tags); END;
CREATE TRIGGER IF NOT EXISTS kb_au AFTER UPDATE ON kb BEGIN
  INSERT INTO kb_fts(kb_fts, rowid, title, body, tags) VALUES ('delete', old.id, old.title, old.body, old.tags);
  INSERT INTO kb_fts(rowid, title, body, tags) VALUES (new.id, new.title, new.body, new.tags); END;
CREATE TABLE IF NOT EXISTS reports(id INTEGER PRIMARY KEY, day TEXT, dept TEXT, body TEXT, created REAL, llm INTEGER);
CREATE UNIQUE INDEX IF NOT EXISTS reports_day_dept ON reports(day, dept);
CREATE TABLE IF NOT EXISTS metrics(name TEXT PRIMARY KEY, value REAL, target REAL, unit TEXT, updated REAL, source TEXT);
CREATE TABLE IF NOT EXISTS metric_log(id INTEGER PRIMARY KEY, name TEXT, value REAL, at REAL, source TEXT);
CREATE TABLE IF NOT EXISTS outbox(id INTEGER PRIMARY KEY, created REAL, task_id INTEGER, dept TEXT, body TEXT, status TEXT);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
"""

# Numbered migrations (PRAGMA user_version). Each step is idempotent: databases from 0.4.x already have some of it.
MIGRATIONS: list[list[str]] = [
    [   # 1 -> 0.5.0
        "ALTER TABLE outbox ADD COLUMN note TEXT",
        "ALTER TABLE outbox ADD COLUMN project_id INTEGER",
        "ALTER TABLE tasks ADD COLUMN worker TEXT",
        "ALTER TABLE tasks ADD COLUMN lease_until REAL",
        "ALTER TABLE tasks ADD COLUMN project_id INTEGER",
        "ALTER TABLE tasks ADD COLUMN summary TEXT",
        "ALTER TABLE tasks ADD COLUMN output_path TEXT",
        "ALTER TABLE tasks ADD COLUMN final INTEGER DEFAULT 0",
        "ALTER TABLE tasks ADD COLUMN kind TEXT",
        "ALTER TABLE kb ADD COLUMN trust TEXT",
        "CREATE INDEX IF NOT EXISTS tasks_project ON tasks(project_id)",
        "CREATE INDEX IF NOT EXISTS tasks_depends ON tasks(depends_on)",
        """CREATE TABLE IF NOT EXISTS projects(id INTEGER PRIMARY KEY, created REAL, updated REAL, title TEXT,
             command TEXT, status TEXT, plan TEXT, answer TEXT, result TEXT, final INTEGER DEFAULT 0,
             critical INTEGER DEFAULT 0, conference_id INTEGER, tokens INTEGER DEFAULT 0, error TEXT)""",
        """CREATE TABLE IF NOT EXISTS decisions(id INTEGER PRIMARY KEY, at REAL, kind TEXT, subject TEXT,
             choice TEXT, why TEXT, ref TEXT)""",
        """CREATE TABLE IF NOT EXISTS conferences(id INTEGER PRIMARY KEY, created REAL, updated REAL,
             project_id INTEGER, question TEXT, seats TEXT, rounds TEXT, memo TEXT, options TEXT, status TEXT,
             choice TEXT, tokens INTEGER DEFAULT 0, calls INTEGER DEFAULT 0, error TEXT)""",
        """CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY, created REAL, project_id INTEGER,
             from_task INTEGER, from_dept TEXT, to_dept TEXT, question TEXT, answer_task INTEGER, status TEXT)""",
    ],
    [   # 2 -> 0.6.0
        "ALTER TABLE decisions ADD COLUMN superseded_by INTEGER",
        "ALTER TABLE tasks ADD COLUMN files TEXT",
        "ALTER TABLE projects ADD COLUMN review TEXT",
        "ALTER TABLE projects ADD COLUMN suggestions TEXT",
        "ALTER TABLE projects ADD COLUMN fix_round INTEGER DEFAULT 0",
        """CREATE TABLE IF NOT EXISTS changes(id INTEGER PRIMARY KEY, created REAL, updated REAL, project_id INTEGER,
             source TEXT, repo TEXT, branch TEXT, worktree TEXT, base TEXT, status TEXT, files INTEGER DEFAULT 0,
             note TEXT)""",
    ],
    [   # 3 -> 0.7.0: the company's records of the people it sells to and the money that comes in
        """CREATE TABLE IF NOT EXISTS contacts(id INTEGER PRIMARY KEY, created REAL, updated REAL, name TEXT,
             org TEXT, email TEXT, source TEXT, stage TEXT, value REAL, currency TEXT, dept TEXT, next_step TEXT,
             next_date TEXT, notes TEXT, trust TEXT)""",
        "CREATE UNIQUE INDEX IF NOT EXISTS contacts_email ON contacts(email)",
        """CREATE TABLE IF NOT EXISTS interactions(id INTEGER PRIMARY KEY, contact_id INTEGER, at REAL, kind TEXT,
             summary TEXT, outbox_id INTEGER, task_id INTEGER)""",
        "CREATE INDEX IF NOT EXISTS interactions_contact ON interactions(contact_id)",
        """CREATE TABLE IF NOT EXISTS revenue(id INTEGER PRIMARY KEY, at REAL, contact_id INTEGER, amount REAL,
             currency TEXT, kind TEXT, source TEXT, ref TEXT)""",
    ],
]


def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


class State:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(path.parent, 0o700)            # keys, token and db live here
        except OSError:
            pass
        self.path = path
        self._lock = threading.RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=10000")
        self.db.executescript(SCHEMA)
        self.migrate()

    def migrate(self) -> int:
        with self._lock:
            v = self.db.execute("PRAGMA user_version").fetchone()[0]
            for i, steps in enumerate(MIGRATIONS[v:], start=v + 1):
                for sql in steps:
                    try:
                        self.db.execute(sql)
                    except sqlite3.OperationalError as e:
                        if "duplicate column" not in str(e) and "already exists" not in str(e):
                            raise
                self.db.execute(f"PRAGMA user_version={i}")
            return self.db.execute("PRAGMA user_version").fetchone()[0]

    def xc(self, sql: str, args: tuple = ()) -> int:
        """Execute and return the number of rows changed."""
        with self._lock:
            return self.db.execute(sql, args).rowcount

    def claim(self, tid: int, from_statuses: tuple[str, ...], worker: str, lease: float, **extra) -> bool:
        """Atomically move a task to running for one worker. False if someone else got it first."""
        sets = "".join(f",{k}=?" for k in extra)
        n = self.xc(f"UPDATE tasks SET status='running', worker=?, lease_until=?, updated=?{sets} "
                    f"WHERE id=? AND status IN ({','.join('?' * len(from_statuses))})",
                    (worker, time.time() + lease, time.time(), *extra.values(), tid, *from_statuses))
        return n == 1

    def backup(self, dest: Path) -> Path:
        """Consistent copy of a live WAL database."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            dest.unlink()
        with self._lock:
            self.db.execute("VACUUM INTO ?", (str(dest),))
        return dest

    # generic
    def q(self, sql: str, args: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def one(self, sql: str, args: tuple = ()) -> dict | None:
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def x(self, sql: str, args: tuple = ()) -> int:
        with self._lock:
            return self.db.execute(sql, args).lastrowid

    # settings
    def get(self, key: str, default=None):
        r = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(r["value"]) if r else default

    def set(self, key: str, value) -> None:
        self.x("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (key, json.dumps(value)))

    # tasks
    def add_task(self, **t) -> int:
        now = time.time()
        t.setdefault("status", "queued")
        t.setdefault("title", (t.get("body") or "")[:80])
        cols = ["created", "updated", *t.keys()]
        vals = [now, now, *t.values()]
        return self.x(f"INSERT INTO tasks({','.join(cols)}) VALUES({','.join('?' * len(cols))})", tuple(vals))

    def update_task(self, tid: int, **f) -> None:
        f["updated"] = time.time()
        sets = ",".join(f"{k}=?" for k in f)
        self.x(f"UPDATE tasks SET {sets} WHERE id=?", (*f.values(), tid))

    def task(self, tid: int) -> dict | None:
        return self.one("SELECT * FROM tasks WHERE id=?", (tid,))

    # usage
    def record_usage(self, **u) -> None:
        u.setdefault("ts", time.time())
        u.setdefault("day", today())
        cols = list(u.keys())
        self.x(f"INSERT INTO usage({','.join(cols)}) VALUES({','.join('?' * len(cols))})", tuple(u.values()))

    def calls_today(self, auto: bool | None = None) -> int:
        if auto is None:
            return self.one("SELECT COUNT(*) n FROM usage WHERE day=?", (today(),))["n"]
        return self.one("SELECT COUNT(*) n FROM usage WHERE day=? AND auto=?", (today(), int(auto)))["n"]
