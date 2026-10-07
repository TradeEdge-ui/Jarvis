"""SQLite persistence. One connection per thread, WAL mode, FTS5 for memory search."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY, value TEXT NOT NULL, updated TEXT
);
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY, title TEXT, device TEXT, created TEXT NOT NULL, updated TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    role TEXT NOT NULL, content TEXT NOT NULL, ts TEXT NOT NULL, device TEXT, meta TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, id);

CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL, title TEXT, content TEXT NOT NULL, tags TEXT DEFAULT '',
    project TEXT, source TEXT, importance REAL DEFAULT 0.5,
    created TEXT NOT NULL, updated TEXT NOT NULL, deleted INTEGER DEFAULT 0
);
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    title, content, tags, content='memories', content_rowid='id', tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, title, content, tags) VALUES (new.id, new.title, new.content, new.tags);
END;
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, title, content, tags)
    VALUES ('delete', old.id, old.title, old.content, old.tags);
END;
CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, title, content, tags)
    VALUES ('delete', old.id, old.title, old.content, old.tags);
    INSERT INTO memories_fts(rowid, title, content, tags) VALUES (new.id, new.title, new.content, new.tags);
END;

CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL,
    parent TEXT, description TEXT, status TEXT DEFAULT 'active', created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL, description TEXT DEFAULT '', priority INTEGER DEFAULT 3,
    deadline TEXT, project TEXT, status TEXT DEFAULT 'todo', depends_on TEXT DEFAULT '[]',
    owner TEXT DEFAULT 'user', automation_level INTEGER, notes TEXT DEFAULT '',
    remind_at TEXT, reminded_at TEXT, created TEXT NOT NULL, updated TEXT NOT NULL,
    completed_at TEXT, source_device TEXT
);
CREATE TABLE IF NOT EXISTS task_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER NOT NULL, ts TEXT NOT NULL,
    event TEXT NOT NULL, detail TEXT
);
CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tool TEXT NOT NULL, args TEXT NOT NULL, fingerprint TEXT NOT NULL, scope TEXT, risk TEXT,
    reason TEXT, why_needed TEXT, user_command TEXT, conversation_id TEXT, device TEXT,
    status TEXT NOT NULL DEFAULT 'pending', created TEXT NOT NULL, expires TEXT NOT NULL,
    decided_at TEXT, decided_by TEXT, result TEXT
);
CREATE TABLE IF NOT EXISTS standing_approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT, tool TEXT NOT NULL, fingerprint TEXT NOT NULL,
    created TEXT NOT NULL, expires TEXT, UNIQUE(tool, fingerprint)
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL, run_id TEXT, device TEXT, user_command TEXT, reason TEXT,
    tool TEXT NOT NULL, scope TEXT, action TEXT, args TEXT, status TEXT NOT NULL,
    summary TEXT, verified INTEGER, verification TEXT, success INTEGER NOT NULL,
    permission TEXT, risk TEXT, autonomy INTEGER, prev_hash TEXT NOT NULL, hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, priority TEXT NOT NULL,
    title TEXT NOT NULL, body TEXT DEFAULT '', source TEXT, dedupe_key TEXT UNIQUE,
    read_at TEXT, ref TEXT
);
CREATE TABLE IF NOT EXISTS devices (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, kind TEXT DEFAULT 'other',
    role TEXT NOT NULL, token_hash TEXT UNIQUE NOT NULL, can_approve INTEGER DEFAULT 1,
    created TEXT NOT NULL, last_seen TEXT, revoked INTEGER DEFAULT 0
);
"""


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self._local = threading.local()
        self._init_lock = threading.Lock()
        with self._init_lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    @property
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=10, isolation_level=None)  # autocommit; explicit tx below
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("PRAGMA busy_timeout=10000")
            self._local.conn = c
        return c

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    @contextmanager
    def transaction(self, immediate: bool = False):
        c = self.conn
        if c.in_transaction:  # nested: join the outer transaction
            yield c
            return
        c.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield c
        except BaseException:
            c.execute("ROLLBACK")
            raise
        else:
            c.execute("COMMIT")

    # ---- settings (JSON values) ---------------------------------------
    def get_setting(self, key: str, default=None):
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    def set_setting(self, key: str, value, now: str = "") -> None:
        self.execute(
            "INSERT INTO settings(key,value,updated) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated=excluded.updated",
            (key, json.dumps(value), now),
        )

    def close(self) -> None:
        c = getattr(self._local, "conn", None)
        if c is not None:
            c.close()
            self._local.conn = None
