"""Fleet state: durable queue with atomic leases, worker heartbeats, append-only attempt log.

Own database (never the CRM): sender and discovery already write there.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path

STATE_DIR = Path(os.environ.get("DOTZ_FLEET_STATE", Path.home() / ".local" / "state" / "dotz"))
DB_PATH = STATE_DIR / "fleet.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS fleet_dots(
  name TEXT PRIMARY KEY, role TEXT NOT NULL, operational INTEGER NOT NULL, not_operational_reason TEXT,
  mission TEXT NOT NULL, capabilities TEXT NOT NULL, budget TEXT NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS fleet_tasks(
  task_id TEXT PRIMARY KEY, dot TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '{}',
  idem_key TEXT NOT NULL UNIQUE, status TEXT NOT NULL CHECK(status IN ('QUEUED','LEASED','SUCCEEDED','FAILED','DEAD')),
  priority INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 3,
  run_after REAL NOT NULL DEFAULT 0, lease_owner TEXT, lease_expires_at REAL,
  result TEXT, verification TEXT, last_error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL, finished_at REAL);
CREATE INDEX IF NOT EXISTS ix_fleet_tasks_claim ON fleet_tasks(dot, status, run_after);
CREATE TABLE IF NOT EXISTS fleet_workers(
  worker_id TEXT PRIMARY KEY, dot TEXT NOT NULL, pid INTEGER NOT NULL, started_at REAL NOT NULL,
  heartbeat_at REAL NOT NULL, current_task TEXT, status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS fleet_attempts(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, dot TEXT NOT NULL, kind TEXT NOT NULL, attempt INTEGER NOT NULL,
  worker_id TEXT, outcome TEXT NOT NULL, detail TEXT, receipt_id TEXT, started_at REAL, at REAL NOT NULL);
CREATE TRIGGER IF NOT EXISTS fleet_attempts_no_update BEFORE UPDATE ON fleet_attempts BEGIN SELECT RAISE(ABORT,'attempts are append-only'); END;
CREATE TRIGGER IF NOT EXISTS fleet_attempts_no_delete BEFORE DELETE ON fleet_attempts BEGIN SELECT RAISE(ABORT,'attempts are append-only'); END;
CREATE TABLE IF NOT EXISTS fleet_effects(
  effect_key TEXT PRIMARY KEY, task_id TEXT, detail TEXT, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS fleet_snapshots(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, ts REAL NOT NULL, data TEXT NOT NULL, task_id TEXT);
CREATE INDEX IF NOT EXISTS ix_fleet_snap ON fleet_snapshots(kind, ts);
CREATE TABLE IF NOT EXISTS fleet_schedules(
  dot TEXT NOT NULL, kind TEXT NOT NULL, every_s REAL NOT NULL, payload TEXT NOT NULL DEFAULT '{}', PRIMARY KEY(dot, kind));
"""


class FleetDB:
    def __init__(self, path: Path | str = DB_PATH, now=time.time):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path, self.now = path, now
        self.lock = threading.RLock()
        self.c = sqlite3.connect(str(path), timeout=30, isolation_level=None, check_same_thread=False)
        self.c.row_factory = sqlite3.Row
        self.c.execute("PRAGMA journal_mode=WAL")
        self.c.execute("PRAGMA busy_timeout=15000")
        self.c.executescript(SCHEMA)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def close(self):
        self.c.close()

    def one(self, sql: str, args: tuple = ()):
        with self.lock:
            return self.c.execute(sql, args).fetchone()

    def all(self, sql: str, args: tuple = ()):
        with self.lock:
            return self.c.execute(sql, args).fetchall()

    # ---------------------------------------------------------------- registry
    def upsert_dot(self, d: dict) -> None:
        with self.lock:
            self.c.execute(
                "INSERT INTO fleet_dots VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET role=excluded.role,operational=excluded.operational,"
                "not_operational_reason=excluded.not_operational_reason,mission=excluded.mission,capabilities=excluded.capabilities,budget=excluded.budget,updated_at=excluded.updated_at",
                (d["name"], d["role"], int(d["operational"]), d.get("reason"), d["mission"],
                 json.dumps(d["capabilities"]), json.dumps(d["budget"]), self.now()))

    # ------------------------------------------------------------------- queue
    def enqueue(self, dot: str, kind: str, idem_key: str, payload: dict | None = None, run_after: float = 0,
                max_attempts: int = 3, priority: int = 0) -> tuple[str, bool]:
        """Idempotent: a second enqueue with the same idem_key returns the existing task (created=False)."""
        t = self.now()
        tid = "ft_" + os.urandom(6).hex()
        with self.lock:
            cur = self.c.execute(
                "INSERT OR IGNORE INTO fleet_tasks(task_id,dot,kind,payload,idem_key,status,priority,max_attempts,run_after,created_at,updated_at) "
                "VALUES(?,?,?,?,?,'QUEUED',?,?,?,?,?)",
                (tid, dot, kind, json.dumps(payload or {}), idem_key, priority, max_attempts, run_after, t, t))
            if cur.rowcount == 1:
                return tid, True
            return self.c.execute("SELECT task_id FROM fleet_tasks WHERE idem_key=?", (idem_key,)).fetchone()[0], False

    def claim(self, worker_id: str, dot: str, lease_s: float) -> dict | None:
        """Atomic: one UPDATE..RETURNING picks and leases a single due task. Two workers can never get the same task."""
        t = self.now()
        with self.lock:
            r = self.c.execute(
                "UPDATE fleet_tasks SET status='LEASED', lease_owner=?, lease_expires_at=?, attempts=attempts+1, updated_at=? "
                "WHERE task_id=(SELECT task_id FROM fleet_tasks WHERE dot=? AND status='QUEUED' AND run_after<=? "
                "ORDER BY priority DESC, created_at LIMIT 1) AND status='QUEUED' RETURNING *",
                (worker_id, t + lease_s, t, dot, t)).fetchone()
            return dict(r) if r else None

    def extend_lease(self, task_id: str, worker_id: str, attempt: int, lease_s: float) -> bool:
        with self.lock:
            return self.c.execute(
                "UPDATE fleet_tasks SET lease_expires_at=?, updated_at=? WHERE task_id=? AND status='LEASED' AND lease_owner=? AND attempts=?",
                (self.now() + lease_s, self.now(), task_id, worker_id, attempt)).rowcount == 1

    def _log(self, task: dict, outcome: str, detail: str = "", worker_id: str | None = None, receipt_id: str | None = None, started: float | None = None):
        self.c.execute("INSERT INTO fleet_attempts(task_id,dot,kind,attempt,worker_id,outcome,detail,receipt_id,started_at,at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (task["task_id"], task["dot"], task["kind"], task["attempts"], worker_id or task.get("lease_owner"), outcome, detail[:2000], receipt_id, started, self.now()))

    def complete(self, task: dict, worker_id: str, result: dict, verification: dict, receipt_id: str | None = None, started: float | None = None) -> bool:
        """Fenced on (owner, attempt): a worker whose lease was reaped cannot complete the task."""
        with self.lock:
            ok = self.c.execute(
                "UPDATE fleet_tasks SET status='SUCCEEDED', result=?, verification=?, last_error=NULL, lease_owner=NULL, lease_expires_at=NULL, finished_at=?, updated_at=? "
                "WHERE task_id=? AND status='LEASED' AND lease_owner=? AND attempts=?",
                (json.dumps(result, default=str)[:20000], json.dumps(verification, default=str)[:5000], self.now(), self.now(), task["task_id"], worker_id, task["attempts"])).rowcount == 1
            if ok:
                self._log(task, "SUCCEEDED", "verified", worker_id, receipt_id, started)
            return ok

    def fail(self, task: dict, worker_id: str, error: str, backoff_s: float = 60, receipt_id: str | None = None,
             started: float | None = None, terminal: bool = False) -> str | None:
        """Bounded retries with exponential backoff; at max_attempts (or terminal) the task goes to DEAD (dead letter)."""
        with self.lock:
            dead = terminal or task["attempts"] >= task["max_attempts"]
            status = "DEAD" if dead else "QUEUED"
            delay = backoff_s * (2 ** (task["attempts"] - 1))
            ok = self.c.execute(
                "UPDATE fleet_tasks SET status=?, last_error=?, lease_owner=NULL, lease_expires_at=NULL, run_after=?, finished_at=?, updated_at=? "
                "WHERE task_id=? AND status='LEASED' AND lease_owner=? AND attempts=?",
                (status, error[:2000], self.now() + (0 if dead else delay), self.now() if dead else None, self.now(), task["task_id"], worker_id, task["attempts"])).rowcount == 1
            if not ok:
                return None
            self._log(task, "DEAD" if dead else "RETRY", error, worker_id, receipt_id, started)
            return status

    def reap_expired(self) -> list[dict]:
        """Crash recovery: leases past expiry go back to QUEUED (or DEAD when out of attempts)."""
        t, out = self.now(), []
        with self.lock:
            for r in self.c.execute("SELECT * FROM fleet_tasks WHERE status='LEASED' AND lease_expires_at<?", (t,)).fetchall():
                task = dict(r)
                dead = task["attempts"] >= task["max_attempts"]
                if self.c.execute("UPDATE fleet_tasks SET status=?, lease_owner=NULL, lease_expires_at=NULL, last_error='lease expired (worker crashed or hung)', "
                                  "run_after=?, finished_at=?, updated_at=? WHERE task_id=? AND status='LEASED' AND attempts=?",
                                  ("DEAD" if dead else "QUEUED", t, t if dead else None, t, task["task_id"], task["attempts"])).rowcount == 1:
                    self._log(task, "LEASE_EXPIRED", f"owner {task['lease_owner']}", task["lease_owner"])
                    out.append(task)
        return out

    # ----------------------------------------------------------------- workers
    def worker_beat(self, worker_id: str, dot: str, current: str | None, status: str = "UP") -> None:
        with self.lock:
            self.c.execute(
                "INSERT INTO fleet_workers VALUES(?,?,?,?,?,?,?) ON CONFLICT(worker_id) DO UPDATE SET heartbeat_at=excluded.heartbeat_at,"
                "current_task=excluded.current_task,status=excluded.status,pid=excluded.pid",
                (worker_id, dot, os.getpid(), self.now(), self.now(), current, status))

    def worker_stop(self, worker_id: str) -> None:
        with self.lock:
            self.c.execute("UPDATE fleet_workers SET status='STOPPED', current_task=NULL, heartbeat_at=? WHERE worker_id=?", (self.now(), worker_id))

    # ---------------------------------------------------------------- effects
    def effect_done(self, key: str) -> bool:
        return self.one("SELECT 1 FROM fleet_effects WHERE effect_key=?", (key,)) is not None

    def record_effect(self, key: str, task_id: str, detail: dict) -> bool:
        with self.lock:
            return self.c.execute("INSERT OR IGNORE INTO fleet_effects VALUES(?,?,?,?)", (key, task_id, json.dumps(detail, default=str), self.now())).rowcount == 1

    # --------------------------------------------------------------- snapshots
    def snapshot(self, kind: str, data: dict, task_id: str | None = None) -> int:
        with self.lock:
            return self.c.execute("INSERT INTO fleet_snapshots(kind,ts,data,task_id) VALUES(?,?,?,?)", (kind, self.now(), json.dumps(data, default=str), task_id)).lastrowid

    def latest(self, kind: str) -> dict | None:
        r = self.one("SELECT ts,data,task_id FROM fleet_snapshots WHERE kind=? ORDER BY seq DESC LIMIT 1", (kind,))
        return {"ts": r["ts"], "data": json.loads(r["data"]), "task_id": r["task_id"]} if r else None

    def prune_snapshots(self, keep_days: float = 14) -> int:
        with self.lock:
            return self.c.execute("DELETE FROM fleet_snapshots WHERE ts<?", (self.now() - keep_days * 86400,)).rowcount

    # --------------------------------------------------------------- schedules
    def set_schedule(self, dot: str, kind: str, every_s: float, payload: dict | None = None) -> None:
        with self.lock:
            self.c.execute("INSERT INTO fleet_schedules VALUES(?,?,?,?) ON CONFLICT(dot,kind) DO UPDATE SET every_s=excluded.every_s,payload=excluded.payload",
                           (dot, kind, every_s, json.dumps(payload or {})))

    def run_schedules(self) -> list[str]:
        """Enqueue one task per (kind, time bucket). The idem key makes restarts and double ticks harmless."""
        t, made = self.now(), []
        for s in self.all("SELECT * FROM fleet_schedules"):
            bucket = int(t // s["every_s"])
            tid, created = self.enqueue(s["dot"], s["kind"], f"sched:{s['dot']}:{s['kind']}:{bucket}", json.loads(s["payload"]))
            if created:
                made.append(tid)
        return made

    def counts(self) -> dict:
        return {r[0]: r[1] for r in self.all("SELECT status, count(*) FROM fleet_tasks GROUP BY 1")}
