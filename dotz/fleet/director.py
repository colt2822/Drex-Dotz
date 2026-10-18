"""Director: owner goal -> NOVA general-task API (Codex1 via FETT-Lite + Drex). No planner in between.

The objective is stored and sent verbatim. State lives in fleet.db table `director_goals`; execution state is
whatever NOVA reports plus the sealed job artifacts on disk. Nothing is marked COMPLETED without Drex ALLOW,
sandbox cleanup, independently re-run passing tests and a sealed diff. NOVA_CONTROL_API_TOKEN stays server-side.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from dotz.fleet.db import DB_PATH

NOVA_URL = os.environ.get("DIRECTOR_NOVA_URL", "http://127.0.0.1:4100/api/v1")
TOKEN_FILE = Path(os.environ.get("DIRECTOR_NOVA_ENV", Path.home() / ".config" / "nova" / "control-api.env"))
PROJECTS_DB = Path(os.environ.get("DIRECTOR_PROJECTS_DB", Path.home() / ".hermes" / "profiles" / "nova" / "projects.db"))
JOBS_DIR = Path(os.environ.get("DIRECTOR_JOBS_DIR", Path.home() / "general-task-runner" / "fett" / "state" / "jobs"))
NOVA_CONTROL_REPO = Path.home() / "nova"
WORKER = "codex1"
MAX_OBJECTIVE = 2400          # NOVA/Drex limit
MAX_ATTEMPTS = 3              # hard dispatch failures before the goal parks as BLOCKED
SUBMIT_LEASE_S = 120          # a SUBMITTING claim older than this is a crashed dispatch; replay is idempotent
BACKOFF_S = 30
HTTP_TIMEOUT_S = 90
_JOB_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_lock = threading.Lock()      # one reconcile/dispatch at a time in this process

SCHEMA = """
CREATE TABLE IF NOT EXISTS director_goals(
  id TEXT PRIMARY KEY, client_key TEXT UNIQUE, objective TEXT NOT NULL, objective_sha TEXT NOT NULL,
  project_slug TEXT NOT NULL, project_path TEXT NOT NULL, worker TEXT NOT NULL, status TEXT NOT NULL,
  idem_key TEXT NOT NULL UNIQUE, task_id TEXT, nova_goal_id TEXT, job_id TEXT, attempts INTEGER NOT NULL DEFAULT 0,
  next_try_at REAL NOT NULL DEFAULT 0, blocker TEXT, activity TEXT, view TEXT, evidence TEXT, poll_error TEXT,
  created_at REAL NOT NULL, updated_at REAL NOT NULL);
"""
COLUMN = {"QUEUED": "QUEUED", "PAUSED": "QUEUED", "SUBMITTING": "QUEUED", "RUNNING": "RUNNING",
          "BLOCKED": "BLOCKED", "FAILED": "BLOCKED", "CANCELLED": "BLOCKED", "COMPLETED": "COMPLETED"}
OPEN = ("QUEUED", "PAUSED", "SUBMITTING", "RUNNING")


class DirectorError(Exception):
    def __init__(self, message: str, http: int = 400):
        super().__init__(message)
        self.http = http


def _db() -> sqlite3.Connection:
    c = sqlite3.connect(str(DB_PATH), timeout=30, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=15000")
    c.executescript(SCHEMA)
    return c


# ------------------------------------------------------------------ projects
def projects() -> list[dict]:
    """Registered Hermes projects that are git work-tree roots. NOVA's allowlist is still enforced at admission."""
    try:
        c = sqlite3.connect(f"file:{PROJECTS_DB}?mode=ro", uri=True, timeout=5)
        rows = c.execute("SELECT slug,name,primary_path FROM projects WHERE archived=0 AND slug NOT LIKE 'nova-wt-%'").fetchall()
        c.close()
    except sqlite3.Error:
        return []
    out = []
    for slug, name, path in rows:
        p = Path(path or "")
        if p.is_dir() and (p / ".git").exists() and p.resolve() != NOVA_CONTROL_REPO.resolve():
            out.append({"slug": slug, "name": name, "path": str(p)})
    return out


# ------------------------------------------------------------------ NOVA client (token never leaves here)
def _token() -> str:
    try:
        for line in TOKEN_FILE.read_text().splitlines():
            k, _, v = line.partition("=")
            if k.strip() == "NOVA_CONTROL_API_TOKEN":
                return v.strip().strip("\"'")
    except OSError:
        pass
    return ""


def _nova(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    """(http status, json). status 0 = backend unreachable."""
    token = _token()
    if not token:
        return 0, {"error": "NOVA control token unavailable"}
    req = urllib.request.Request(NOVA_URL + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        return 0, {"error": type(e).__name__}


def _problem(data: dict) -> str:
    d = data.get("detail", data)
    if isinstance(d, dict):
        return f"{d.get('code') or d.get('error') or 'ERROR'}: {str(d.get('message') or d.get('detail') or '')[:300]}"
    return str(d)[:300]


# ------------------------------------------------------------------ persistence helpers
def _set(c, gid: str, **f) -> None:
    f["updated_at"] = time.time()
    c.execute(f"UPDATE director_goals SET {','.join(k + '=?' for k in f)} WHERE id=?", (*f.values(), gid))


def create_goal(objective: str, project_slug: str, client_key: str | None = None) -> dict:
    if not isinstance(objective, str) or not objective.strip():
        raise DirectorError("goal text is empty")
    if len(objective) > MAX_OBJECTIVE:
        raise DirectorError(f"goal is {len(objective)} chars; the execution path binds at most {MAX_OBJECTIVE} verbatim")
    proj = next((p for p in projects() if p["slug"] == project_slug), None)
    if not proj:
        raise DirectorError(f"project {project_slug!r} is not a registered, authorized project")
    sha = hashlib.sha256(objective.strip().encode()).hexdigest()
    now = time.time()
    with _lock:
        c = _db()
        try:
            c.execute("BEGIN IMMEDIATE")
            dup = None
            if client_key:
                dup = c.execute("SELECT id FROM director_goals WHERE client_key=?", (client_key,)).fetchone()
            dup = dup or c.execute(
                f"SELECT id FROM director_goals WHERE objective_sha=? AND project_slug=? AND status IN ({','.join('?' * len(OPEN))})",
                (sha, project_slug, *OPEN)).fetchone()
            if dup:
                c.execute("COMMIT")
                return {**get_goal(dup["id"]), "duplicate": True}
            gid = "DIR-" + uuid.uuid4().hex[:8].upper()
            c.execute("INSERT INTO director_goals(id,client_key,objective,objective_sha,project_slug,project_path,worker,status,idem_key,"
                      "activity,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                      (gid, client_key or None, objective, sha, project_slug, proj["path"], WORKER, "QUEUED", f"director-{gid}",
                       "Queued for dispatch", now, now))
            c.execute("COMMIT")
        finally:
            c.close()
    _dispatch_next()
    return get_goal(gid)


# ------------------------------------------------------------------ dispatch
def _dispatch_next() -> None:
    """Dispatch at most one eligible QUEUED goal, and only if no Director goal is in flight (NOVA runs one at a time)."""
    with _lock:
        c = _db()
        try:
            c.execute("BEGIN IMMEDIATE")
            if c.execute("SELECT 1 FROM director_goals WHERE status IN ('SUBMITTING','RUNNING')").fetchone():
                c.execute("COMMIT")
                return
            g = c.execute("SELECT * FROM director_goals WHERE status='QUEUED' AND task_id IS NULL AND next_try_at<=? "
                          "ORDER BY created_at LIMIT 1", (time.time(),)).fetchone()
            if not g:
                c.execute("COMMIT")
                return
            _set(c, g["id"], status="SUBMITTING", attempts=g["attempts"] + 1, activity="Submitting to NOVA general-task API")
            c.execute("COMMIT")
        finally:
            c.close()
        _submit({**dict(g), "attempts": g["attempts"] + 1})


def _submit(g: dict) -> None:
    code, data = _nova("POST", "/general-tasks", {"objective": g["objective"], "repository": g["project_path"],
                                                     "idempotency_key": g["idem_key"]})
    c = _db()
    try:
        if code in (200, 201) and data.get("task_id"):
            _apply_view(c, g["id"], data)
        elif code == 429:   # worker busy: wait, this is not a failure
            _set(c, g["id"], status="QUEUED", attempts=max(0, g["attempts"] - 1), next_try_at=time.time() + BACKOFF_S,
                 activity="Waiting: the execution worker is busy")
        elif code == 0 or code >= 500:
            if g["attempts"] >= MAX_ATTEMPTS:
                _set(c, g["id"], status="BLOCKED", blocker=f"Backend unavailable after {MAX_ATTEMPTS} attempts ({_problem(data)}). Resume to retry.",
                     activity="Dispatch stopped")
            else:
                _set(c, g["id"], status="QUEUED", next_try_at=time.time() + BACKOFF_S * g["attempts"],
                     activity=f"Backend unavailable; retry {g['attempts']}/{MAX_ATTEMPTS} scheduled", blocker=_problem(data))
        else:               # 4xx: policy, validation, conflict. Not retriable without a change.
            _set(c, g["id"], status="BLOCKED", blocker=_problem(data), activity="Admission refused")
    finally:
        c.close()


# ------------------------------------------------------------------ reconcile
def _apply_view(c, gid: str, view: dict) -> None:
    """Map a NOVA general-task view onto the Director goal. Only real, evidenced states."""
    state, job = view.get("state"), view.get("fett_lite_job_id")
    f = dict(task_id=view["task_id"], nova_goal_id=view.get("goal_id"), job_id=job, view=json.dumps(view), poll_error=None)
    if state in ("ADMITTED", "RUNNING"):
        _set(c, gid, status="RUNNING", blocker=None, activity=_activity(job) or "Dispatched; waiting for the worker", **f)
        return
    ev = _evidence(job, view)
    f["evidence"] = json.dumps(ev)
    if state == "PASS":
        missing = [k for k, v in ev["gates"].items() if not v]
        if missing:
            _set(c, gid, status="BLOCKED", blocker="NOVA reported PASS but completion evidence is missing: " + ", ".join(missing),
                 activity="Not marked complete", **f)
        else:
            _set(c, gid, status="COMPLETED", blocker=None, activity="Verified complete", **f)
    elif state == "BLOCKED":
        _set(c, gid, status="BLOCKED", blocker=view.get("reason") or (view.get("job_status") or "blocked by NOVA/Drex"), activity="Blocked", **f)
    elif state == "FAILED":
        _set(c, gid, status="FAILED", blocker=_failure(view, ev), activity="Execution failed", **f)
    elif state == "CANCELLED":
        _set(c, gid, status="CANCELLED", blocker="Cancelled in NOVA", activity="Cancelled", **f)
    else:
        _set(c, gid, status="BLOCKED", blocker=f"Unrecognized NOVA state {state!r}", **f)


def _failure(view: dict, ev: dict) -> str:
    r, v = view.get("receipt") or {}, view.get("verification") or {}
    why = []
    if view.get("job_status") != "DONE":
        why.append(f"job {view.get('job_status')}")
    if not r.get("drex_allow"):
        why.append("no Drex ALLOW")
    if not r.get("cleanup_complete"):
        why.append("sandbox cleanup not confirmed")
    if not v.get("passed"):
        why.append("independent tests did not pass" + (f" ({v.get('reason')})" if v.get("reason") else ""))
    return "; ".join(why) or "failed"


def _job_dir(job: str | None) -> Path | None:
    if not job or not _JOB_ID.fullmatch(job):
        return None
    d = JOBS_DIR / job
    return d if d.is_dir() and not d.is_symlink() else None


def _activity(job: str | None) -> str | None:
    d = _job_dir(job)
    try:
        last = (d / "events.jsonl").read_text().strip().splitlines()[-1] if d else None
        e = json.loads(last) if last else None
    except (OSError, ValueError, IndexError):
        return None
    return f"{e.get('t')} · {e.get('ev')}" if e else None


def _evidence(job: str | None, view: dict) -> dict:
    """Artifacts straight from disk: sealed diff (re-hashed), repository base, job paths."""
    d = _job_dir(job)
    r, v = view.get("receipt") or {}, view.get("verification") or {}
    ev = {"job_dir": str(d) if d else None, "diff_path": None, "diff_sha256": None, "diff_bytes": None, "changed_files": [],
          "base_commit": None, "source_repo": None, "receipt_path": None}
    if d:
        diff = d / "changes.diff"
        if diff.is_file() and not diff.is_symlink():
            raw = diff.read_bytes()
            ev.update(diff_path=str(diff), diff_sha256=hashlib.sha256(raw).hexdigest(), diff_bytes=len(raw))
            ev["changed_files"] = sorted({m.decode(errors="replace") for m in re.findall(rb"^diff --git a/(.+?) b/", raw, re.M)})[:200]
        try:
            rep = json.loads((d / "repository.json").read_text())
            ev["base_commit"], ev["source_repo"] = rep.get("base_commit"), rep.get("source")
        except (OSError, ValueError):
            pass
        ev["receipt_path"] = str(d / "job.json") if (d / "job.json").is_file() else None
    ev["gates"] = {"drex_allow": bool(r.get("drex_allow")), "sandbox_cleanup": bool(r.get("cleanup_complete")),
                   "independent_tests_passed": bool(v.get("passed")), "sealed_diff": bool(ev["diff_sha256"] and ev["changed_files"])}
    return ev


def tick() -> None:
    """Lazy recovery/reconcile, driven by dashboard polling (no daemon): stale claims, due retries, running goals."""
    if not _lock.acquire(blocking=False):
        return
    try:
        c = _db()
        try:
            c.execute("UPDATE director_goals SET status='QUEUED',activity='Recovered a dispatch interrupted by restart; replay is idempotent',"
                      "updated_at=? WHERE status='SUBMITTING' AND updated_at<?", (time.time(), time.time() - SUBMIT_LEASE_S))
            running = c.execute("SELECT id,task_id FROM director_goals WHERE status='RUNNING' AND task_id IS NOT NULL").fetchall()
            for g in running:
                code, data = _nova("GET", f"/general-tasks/{g['task_id']}")
                if code == 200 and data.get("task_id"):
                    _apply_view(c, g["id"], data)
                else:
                    _set(c, g["id"], poll_error=f"status unknown: {_problem(data) if code else 'backend unreachable'}")
        finally:
            c.close()
    finally:
        _lock.release()
    _dispatch_next()


# ------------------------------------------------------------------ controls
def _supported(g: dict) -> list[str]:
    s, nothing_sent = g["status"], g["task_id"] is None
    if s == "QUEUED":
        return ["pause", "cancel"]
    if s == "PAUSED":
        return ["resume", "cancel"]
    if s == "BLOCKED" and nothing_sent:
        return ["resume", "cancel"]
    return []   # SUBMITTING/RUNNING: NOVA has no pause/cancel for an admitted general task


def control(gid: str, action: str) -> dict:
    with _lock:
        c = _db()
        try:
            c.execute("BEGIN IMMEDIATE")
            g = c.execute("SELECT * FROM director_goals WHERE id=?", (gid,)).fetchone()
            if not g:
                raise DirectorError("goal not found", 404)
            if action not in _supported(dict(g)):
                why = ("already dispatched: the NOVA general-task API has no safe " + action + " for admitted work"
                       if g["status"] in ("SUBMITTING", "RUNNING") else f"not applicable in state {g['status']}")
                raise DirectorError(f"{action} unsupported: {why}", 409)
            if action == "pause":
                _set(c, gid, status="PAUSED", activity="Paused: no dispatch until resumed")
            elif action == "resume":
                _set(c, gid, status="QUEUED", attempts=0, next_try_at=0, blocker=None, activity="Resumed; queued for dispatch")
            else:
                _set(c, gid, status="CANCELLED", blocker="Cancelled by owner before dispatch", activity="Cancelled before any work started")
            c.execute("COMMIT")
        finally:
            c.close()
    if action == "resume":
        _dispatch_next()
    return get_goal(gid)


# ------------------------------------------------------------------ read model
def _card(g: dict) -> dict:
    view = json.loads(g["view"]) if g["view"] else None
    ev = json.loads(g["evidence"]) if g["evidence"] else None
    r, v = (view or {}).get("receipt") or {}, (view or {}).get("verification") or {}
    tests = None
    if v:
        tests = {"ran": v.get("ran"), "passed": v.get("passed"), "reason": v.get("reason"),
                 "runs": [{"command": x.get("command"), "returncode": x.get("returncode"), "tests": x.get("tests")} for x in v.get("runs", [])]}
    obj = g["objective"]
    final = None
    if g["status"] == "COMPLETED" and ev:
        final = (f"Verified. {len(ev['changed_files'])} file(s) changed in an isolated clone; sealed diff {ev['diff_sha256'][:12]}… "
                 f"NOT applied to {g['project_slug']}.")
    return {"id": g["id"], "title": (obj.strip().splitlines() or [""])[0][:80], "description": obj[:200],
            "original_goal": obj, "objective_sha256": g["objective_sha"], "project": g["project_slug"], "project_path": g["project_path"],
            "worker": g["worker"], "status": COLUMN[g["status"]], "state": g["status"], "task_id": g["task_id"],
            "nova_goal_id": g["nova_goal_id"], "job_id": g["job_id"], "attempts": g["attempts"],
            "blocker": g["blocker"], "poll_error": g["poll_error"], "activity": g["activity"],
            "created_at": g["created_at"], "updated_at": g["updated_at"],
            "tests": tests, "drex": {k: r.get(k) for k in ("drex_allow", "drex_action_id", "account_fp", "policy_digest", "workspace_mode",
                                                           "cleanup_complete", "authorization_consumed", "returncode")} if r else None,
            "evidence": ev, "files": (view or {}).get("files"), "final_report": final, "supported_actions": _supported(g)}


def get_goal(gid: str) -> dict:
    c = _db()
    try:
        g = c.execute("SELECT * FROM director_goals WHERE id=?", (gid,)).fetchone()
    finally:
        c.close()
    if not g:
        raise DirectorError("goal not found", 404)
    return _card(dict(g))


def snapshot() -> dict:
    tick()
    c = _db()
    try:
        goals = [_card(dict(r)) for r in c.execute("SELECT * FROM director_goals ORDER BY created_at DESC LIMIT 200")]
    finally:
        c.close()
    return {"goals": goals, "projects": projects(), "worker": WORKER, "now": time.time(),
            "controls": {"pause": "queued/paused goals only", "resume": "paused or retriable-blocked goals",
                         "cancel": "before dispatch only; running work cannot be cancelled through the NOVA general-task API"}}
