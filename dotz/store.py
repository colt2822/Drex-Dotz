"""Single-file SQLite persistence for Dotz.

Pattern extracted from drex-pr-shepherd (Apache-2.0, same author): append-only events with
idempotent ingest (UNIQUE event_id) enforced by immutability triggers, explicit transition log,
derived aggregates that can be rebuilt. Nothing here is process-local state.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS dots (
  name TEXT PRIMARY KEY, state TEXT NOT NULL, created_at REAL, updated_at REAL,
  last_result TEXT, project_id TEXT
);
CREATE TABLE IF NOT EXISTS transitions (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, dot TEXT NOT NULL, from_state TEXT, to_state TEXT,
  reason TEXT, at REAL
);
CREATE TABLE IF NOT EXISTS tasks (
  task_id TEXT PRIMARY KEY, dot TEXT NOT NULL, title TEXT NOT NULL, status TEXT NOT NULL,
  source TEXT NOT NULL, event_id TEXT, responsibility TEXT, parent_task_id TEXT, parent_dot TEXT,
  expected_output TEXT, deadline REAL, ceiling TEXT, session_id TEXT NOT NULL, steps INTEGER DEFAULT 0,
  obligation TEXT, next_action TEXT, result TEXT, created_at REAL, updated_at REAL
);
CREATE INDEX IF NOT EXISTS tasks_dot ON tasks(dot, status);
CREATE TABLE IF NOT EXISTS steps (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, kind TEXT NOT NULL, summary TEXT, at REAL
);
CREATE TABLE IF NOT EXISTS events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE, event_type TEXT NOT NULL,
  timestamp REAL NOT NULL, source TEXT NOT NULL, subject TEXT, payload_ref TEXT
);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events are immutable'); END;
CREATE TABLE IF NOT EXISTS deliveries (
  delivery_key TEXT PRIMARY KEY, dot TEXT NOT NULL, task_id TEXT NOT NULL, at REAL
);
CREATE TABLE IF NOT EXISTS responsibilities (
  dot TEXT NOT NULL, name TEXT NOT NULL, trigger TEXT NOT NULL, next_due_at REAL,
  current_task TEXT, last_result TEXT, last_run_at REAL, PRIMARY KEY (dot, name)
);
CREATE TABLE IF NOT EXISTS grants (
  grant_id TEXT PRIMARY KEY, dot TEXT NOT NULL, task_id TEXT NOT NULL, session_id TEXT NOT NULL,
  workspace TEXT NOT NULL, capability TEXT NOT NULL, resources TEXT NOT NULL,
  issued_at REAL NOT NULL, expires_at REAL NOT NULL, max_uses INTEGER NOT NULL, uses INTEGER NOT NULL DEFAULT 0,
  approved_by TEXT, revoked INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS receipts (
  receipt_id TEXT PRIMARY KEY, ts REAL NOT NULL, who TEXT, dot TEXT, task_id TEXT, session_id TEXT,
  why TEXT, model TEXT, capability TEXT, grant_id TEXT, target TEXT, action TEXT, decision TEXT,
  reason TEXT, rule TEXT, drex_action_id TEXT, cost_usd REAL, input_tokens INTEGER, output_tokens INTEGER,
  result TEXT, verification TEXT, upstream_executed INTEGER, output_sha256 TEXT
);
CREATE INDEX IF NOT EXISTS receipts_dot ON receipts(dot, ts);
CREATE TRIGGER IF NOT EXISTS receipts_no_update BEFORE UPDATE ON receipts
BEGIN SELECT RAISE(ABORT, 'receipts are append-only'); END;
CREATE TABLE IF NOT EXISTS model_calls (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, dot TEXT, task_id TEXT, tier TEXT,
  provider TEXT, model TEXT, input_tokens INTEGER, output_tokens INTEGER, cache_read INTEGER,
  cost_usd REAL, ok INTEGER
);
CREATE TABLE IF NOT EXISTS delegations (
  child_task_id TEXT PRIMARY KEY, parent_dot TEXT NOT NULL, parent_task_id TEXT, child_dot TEXT NOT NULL,
  ceiling TEXT NOT NULL, at REAL
);
"""


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = path
        self.conn = sqlite3.connect(str(path), timeout=30, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self.lock = threading.RLock()
        try:
            path.chmod(0o600)
        except OSError:
            pass

    def x(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self.lock:
            return self.conn.execute(sql, tuple(params))

    def one(self, sql: str, params: Iterable[Any] = ()) -> dict | None:
        row = self.x(sql, params).fetchone()
        return dict(row) if row else None

    def all(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        return [dict(r) for r in self.x(sql, params).fetchall()]

    def insert(self, table: str, row: dict, *, ignore: bool = False) -> bool:
        cols = ",".join(row)
        marks = ",".join("?" for _ in row)
        verb = "INSERT OR IGNORE" if ignore else "INSERT"
        cur = self.x(f"{verb} INTO {table} ({cols}) VALUES ({marks})", row.values())
        return cur.rowcount == 1

    def transaction(self):
        return _Tx(self)


class _Tx:
    def __init__(self, store: Store):
        self.store = store

    def __enter__(self):
        self.store.lock.acquire()
        self.store.conn.execute("BEGIN IMMEDIATE")
        return self.store

    def __exit__(self, exc_type, *_):
        try:
            self.store.conn.execute("ROLLBACK" if exc_type else "COMMIT")
        finally:
            self.store.lock.release()
        return False


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
