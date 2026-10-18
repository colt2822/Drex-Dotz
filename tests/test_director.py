import hashlib
import json
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from dotz.fleet import director

TOKEN = "tok-secret-123"


class Nova:
    """Stand-in for the NOVA general-task API: idempotent on key, bearer auth, scriptable outcomes."""
    def __init__(self):
        self.tasks, self.by_key, self.posts, self.mode, self.state = {}, {}, [], "ok", "RUNNING"
        self.view_extra = {}
        nova = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a): pass

            def _j(self, code, body):
                raw = json.dumps(body).encode()
                self.send_response(code); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)

            def do_POST(self):
                if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                    return self._j(401, {"detail": "no"})
                b = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                nova.posts.append(b)
                if nova.mode == "busy":
                    return self._j(429, {"detail": {"code": "RESOURCE_BUSY", "message": "busy"}})
                if nova.mode == "down":
                    return self._j(503, {"detail": {"code": "FETT_LITE_UNAVAILABLE", "message": "down"}})
                if nova.mode == "policy":
                    return self._j(400, {"detail": {"code": "POLICY_BLOCKED", "message": "repository is not an allowed workspace"}})
                if b["idempotency_key"] in nova.by_key:
                    return self._j(201, {**nova.by_key[b["idempotency_key"]], "idempotent_replay": True})
                tid = f"task_{len(nova.tasks):04d}"
                v = {"task_id": tid, "goal_id": "g_" + tid, "state": "RUNNING", "fett_lite_job_id": "job-" + tid, "objective": b["objective"]}
                nova.tasks[tid] = v; nova.by_key[b["idempotency_key"]] = v
                self._j(201, v)

            def do_GET(self):
                tid = self.path.rsplit("/", 1)[1]
                if nova.mode == "down":
                    return self._j(503, {})
                v = nova.tasks[tid]
                self._j(200, {**v, "state": nova.state, **nova.view_extra})

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_port}/api/v1"


@pytest.fixture
def env(tmp_path, monkeypatch):
    nova = Nova()
    repo = tmp_path / "proj"; (repo / ".git").mkdir(parents=True)
    pdb = tmp_path / "projects.db"
    c = sqlite3.connect(pdb)
    c.execute("CREATE TABLE projects(id,slug,name,description,icon,color,board_slug,primary_path,created_at,archived)")
    c.execute("INSERT INTO projects VALUES('p1','demo','Demo',NULL,NULL,NULL,NULL,?,0,0)", (str(repo),))
    c.commit(); c.close()
    tok = tmp_path / "control-api.env"; tok.write_text(f"NOVA_CONTROL_API_TOKEN={TOKEN}\n")
    jobs = tmp_path / "jobs"; jobs.mkdir()
    for k, v in dict(NOVA_URL=nova.url, PROJECTS_DB=pdb, TOKEN_FILE=tok, JOBS_DIR=jobs, DB_PATH=tmp_path / "fleet.db").items():
        monkeypatch.setattr(director, k, v)
    monkeypatch.setattr(director, "BACKOFF_S", 0)
    return nova, jobs


def seal(jobs, job, files=("a.py",)):
    d = jobs / job; d.mkdir()
    (d / "changes.diff").write_text("".join(f"diff --git a/{f} b/{f}\n+x\n" for f in files))
    (d / "repository.json").write_text(json.dumps({"source": "/x", "base_commit": "abc"}))
    (d / "job.json").write_text("{}")


PASS = {"job_status": "DONE", "receipt": {"drex_allow": True, "cleanup_complete": True, "drex_action_id": "act1"},
        "verification": {"ran": True, "passed": True, "runs": [{"command": "pytest", "returncode": 0, "tests": 2}]}}


def test_submit_verbatim_and_persisted(env):
    nova, _ = env
    obj = "Line one\n\nLine  two  with   spacing"
    g = director.create_goal(obj, "demo", "k1")
    assert g["state"] == "RUNNING" and g["task_id"] == "task_0000" and g["status"] == "RUNNING"
    assert nova.posts[0]["objective"] == obj and nova.posts[0]["idempotency_key"] == "director-" + g["id"]
    assert director.get_goal(g["id"])["original_goal"] == obj
    assert TOKEN not in json.dumps(director.snapshot())


def test_invalid_project_and_empty_and_oversize(env):
    nova, _ = env
    for args in (("x", "nope"), ("  ", "demo"), ("a" * 2401, "demo")):
        with pytest.raises(director.DirectorError):
            director.create_goal(*args)
    assert nova.posts == []


def test_duplicate_protection(env):
    nova, _ = env
    a = director.create_goal("do it", "demo", "k1")
    assert director.create_goal("do it", "demo", "k1")["id"] == a["id"]          # same client key
    b = director.create_goal("do it", "demo", "k2")                                # same open objective
    assert b["id"] == a["id"] and b["duplicate"] and len(nova.posts) == 1


def test_one_in_flight_then_next_dispatches(env):
    nova, jobs = env
    a = director.create_goal("first", "demo")
    b = director.create_goal("second", "demo")
    assert b["state"] == "QUEUED" and len(nova.posts) == 1
    seal(jobs, "job-task_0000"); nova.state, nova.view_extra = "PASS", PASS
    director.tick()
    assert director.get_goal(a["id"])["state"] == "COMPLETED"
    assert director.get_goal(b["id"])["state"] == "RUNNING" and len(nova.posts) == 2


def test_completion_requires_evidence(env):
    nova, jobs = env
    g = director.create_goal("task", "demo")
    nova.state, nova.view_extra = "PASS", PASS            # PASS but no sealed diff on disk
    director.tick()
    r = director.get_goal(g["id"])
    assert r["state"] == "BLOCKED" and "sealed_diff" in r["blocker"]
    g2 = director.create_goal("task2", "demo"); seal(jobs, "job-task_0001")
    nova.view_extra = {**PASS, "verification": {"ran": True, "passed": False}}
    director.tick()
    assert director.get_goal(g2["id"])["state"] == "BLOCKED"


def test_completed_has_evidence(env):
    nova, jobs = env
    g = director.create_goal("task", "demo"); seal(jobs, "job-task_0000", ("a.py", "b.py"))
    nova.state, nova.view_extra = "PASS", PASS
    director.tick()
    r = director.get_goal(g["id"])
    assert r["state"] == "COMPLETED" and r["evidence"]["changed_files"] == ["a.py", "b.py"]
    assert r["evidence"]["diff_sha256"] == hashlib.sha256((jobs / "job-task_0000" / "changes.diff").read_bytes()).hexdigest()
    assert r["controls" if False else "supported_actions"] == []


def test_failed_and_blocked_states(env):
    nova, _ = env
    g = director.create_goal("task", "demo")
    nova.state, nova.view_extra = "FAILED", {"job_status": "FAILED", "receipt": {}, "verification": None}
    director.tick()
    r = director.get_goal(g["id"])
    assert r["state"] == "FAILED" and r["status"] == "BLOCKED" and "no Drex ALLOW" in r["blocker"]


def test_backend_down_bounded_retries_then_resume(env):
    nova, _ = env
    nova.mode = "down"
    g = director.create_goal("task", "demo")
    for _ in range(5):
        director.tick()
    r = director.get_goal(g["id"])
    assert r["state"] == "BLOCKED" and r["attempts"] == director.MAX_ATTEMPTS and len(nova.posts) == director.MAX_ATTEMPTS
    assert r["supported_actions"] == ["resume", "cancel"]
    nova.mode = "ok"
    assert director.control(g["id"], "resume")["state"] == "RUNNING" and len({p["idempotency_key"] for p in nova.posts}) == 1
    assert len(nova.tasks) == 1


def test_busy_waits_without_burning_attempts(env):
    nova, _ = env
    nova.mode = "busy"
    g = director.create_goal("task", "demo")
    r = director.get_goal(g["id"])
    assert r["state"] == "QUEUED" and r["attempts"] == 0
    nova.mode = "ok"; director.tick()
    assert director.get_goal(g["id"])["state"] == "RUNNING"


def test_admission_refused_is_blocked_not_retried(env):
    nova, _ = env
    nova.mode = "policy"
    g = director.create_goal("task", "demo")
    r = director.get_goal(g["id"])
    assert r["state"] == "BLOCKED" and "POLICY_BLOCKED" in r["blocker"] and len(nova.posts) == 1


def test_restart_recovery_replays_idempotently(env, monkeypatch):
    nova, _ = env
    g = director.create_goal("task", "demo")
    c = director._db()   # simulate crash after NOVA admitted but before the result was stored
    c.execute("UPDATE director_goals SET status='SUBMITTING',task_id=NULL,job_id=NULL,view=NULL,updated_at=? WHERE id=?",
              (time.time() - 1000, g["id"]))
    c.close()
    director.tick()
    r = director.get_goal(g["id"])
    assert r["state"] == "RUNNING" and r["task_id"] == "task_0000" and len(nova.tasks) == 1 and len(nova.posts) == 2


def test_poll_failure_keeps_running_and_flags_unknown(env):
    nova, _ = env
    g = director.create_goal("task", "demo")
    nova.mode = "down"; director.tick()
    r = director.get_goal(g["id"])
    assert r["state"] == "RUNNING" and "status unknown" in r["poll_error"]


def test_controls(env):
    nova, _ = env
    a = director.create_goal("first", "demo"); b = director.create_goal("second", "demo")
    with pytest.raises(director.DirectorError) as e:
        director.control(a["id"], "cancel")                    # running: unsupported, not faked
    assert e.value.http == 409
    assert director.control(b["id"], "pause")["state"] == "PAUSED"
    director.tick(); assert director.get_goal(b["id"])["state"] == "PAUSED" and len(nova.posts) == 1
    assert director.control(b["id"], "resume")["state"] == "QUEUED"
    assert director.control(b["id"], "cancel")["state"] == "CANCELLED"
    with pytest.raises(director.DirectorError):
        director.control(b["id"], "resume")
