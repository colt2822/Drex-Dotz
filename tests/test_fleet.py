import json
import threading
import time
from pathlib import Path

import pytest

from dotz.fleet import adapters
from dotz.fleet.adapters import Adapter, Ctx, TaskFailed
from dotz.fleet.db import FleetDB
from dotz.fleet.dots import DOTS
from dotz.fleet.gate import FleetGate
from dotz.fleet.supervisor import register, run_task


class Clock:
    def __init__(self): self.t = 1_000_000.0
    def __call__(self): return self.t


@pytest.fixture
def env(tmp_path):
    clk = Clock()
    db = FleetDB(tmp_path / "fleet.db", now=clk)
    register(db)
    gate = FleetGate(DOTS, tmp_path / "rt")
    ctx = Ctx(db, now=clk)
    return db, gate, ctx, clk


def adapter(kind, ex, vf=lambda c, p, r: [{"check": "x", "ok": True}], cap="crm.read"):
    adapters.ADAPTERS[kind] = Adapter(kind, cap, ex, vf, 5)
    return kind


def test_enqueue_idempotent(env):
    db, *_ = env
    a, c1 = db.enqueue("analyst", "k", "same")
    b, c2 = db.enqueue("analyst", "k", "same")
    assert a == b and c1 and not c2
    assert db.one("select count(*) from fleet_tasks")[0] == 1


def test_claim_is_exclusive_under_concurrency(env):
    db, *_ = env
    for i in range(20):
        db.enqueue("analyst", "k", f"i{i}")
    got, lock = [], threading.Lock()
    def w(n):
        while True:
            t = db.claim(f"w{n}", "analyst", 60)
            if not t: return
            with lock: got.append(t["task_id"])
    ts = [threading.Thread(target=w, args=(n,)) for n in range(6)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert len(got) == 20 and len(set(got)) == 20


def test_lease_expiry_reclaims_and_fences_stale_worker(env):
    db, gate, ctx, clk = env
    db.enqueue("analyst", "k", "a")
    t1 = db.claim("w1", "analyst", 30)
    assert db.claim("w2", "analyst", 30) is None            # leased: invisible
    clk.t += 31
    assert [r["task_id"] for r in db.reap_expired()] == [t1["task_id"]]
    t2 = db.claim("w2", "analyst", 30)
    assert t2["task_id"] == t1["task_id"] and t2["attempts"] == 2
    # the crashed/hung first worker wakes up and tries to finish: fenced out
    assert not db.complete(t1, "w1", {"r": 1}, {"passed": True})
    assert db.complete(t2, "w2", {"r": 2}, {"passed": True})
    assert db.one("select status from fleet_tasks")[0] == "SUCCEEDED"


def test_bounded_retries_then_dead_letter(env):
    db, *_ = env
    db.enqueue("analyst", "k", "a", max_attempts=2)
    t = db.claim("w", "analyst", 30)
    assert db.fail(t, "w", "boom", backoff_s=10) == "QUEUED"
    assert db.claim("w", "analyst", 30) is None             # backoff not elapsed
    env[3].t += 11
    t = db.claim("w", "analyst", 30)
    assert db.fail(t, "w", "boom", backoff_s=10) == "DEAD"
    assert db.claim("w", "analyst", 30) is None
    assert db.counts() == {"DEAD": 1}


def test_attempt_log_is_append_only(env):
    db, *_ = env
    db.enqueue("analyst", "k", "a")
    t = db.claim("w", "analyst", 30); db.fail(t, "w", "x", terminal=True)
    with pytest.raises(Exception):
        db.c.execute("update fleet_attempts set outcome='SUCCEEDED'")
    with pytest.raises(Exception):
        db.c.execute("delete from fleet_attempts")


def test_schedule_bucket_dedupes_restarts(env):
    db, _, _, clk = env
    n1 = db.run_schedules(); n2 = db.run_schedules()
    assert len(n1) == 6 and n2 == []
    clk.t += 61
    assert len(db.run_schedules()) >= 1                      # next 60s bucket only for the fast schedule


def test_verified_completion_only(env):
    db, gate, ctx, _ = env
    k = adapter("t.ok", lambda c, p: {"v": 1})
    db.enqueue("analyst", k, "ok"); t = db.claim("w", "analyst", 30)
    assert run_task(db, gate, ctx, t, "w") == "SUCCEEDED"
    assert db.latest("t.ok")["data"] == {"v": 1}
    k = adapter("t.unverified", lambda c, p: {"v": 1}, lambda c, p, r: [{"check": "state matches", "ok": False}])
    db.enqueue("analyst", k, "bad", max_attempts=1); t = db.claim("w", "analyst", 30)
    assert run_task(db, gate, ctx, t, "w") == "DEAD"          # execute() succeeded but verify() failed: NOT completed
    row = db.one("select status,last_error from fleet_tasks where idem_key='bad'")
    assert row[0] == "DEAD" and "verification failed" in row[1]


def test_drex_capability_denied_is_blocked_and_receipted(env):
    db, gate, ctx, _ = env
    ran = []
    k = adapter("t.cap", lambda c, p: ran.append(1) or {}, cap="gmail.send")
    db.enqueue("analyst", k, "cap"); t = db.claim("w", "analyst", 30)
    assert run_task(db, gate, ctx, t, "w") == "DEAD" and not ran
    r = gate.rt.store.one("select decision,rule,capability from receipts where task_id=?", (t["task_id"],))
    assert (r["decision"], r["rule"], r["capability"]) == ("BLOCK", "DOT_POLICY_DENIED", "gmail.send")


def test_not_operational_dot_refuses_instead_of_faking(env):
    db, gate, ctx, _ = env
    k = adapter("t.fake", lambda c, p: {"fake": True})
    db.enqueue("growth", k, "g"); t = db.claim("w", "growth", 30)
    assert run_task(db, gate, ctx, t, "w") == "DEAD"
    assert db.one("select status from fleet_tasks")[0] == "DEAD"
    assert gate.rt.store.one("select decision from receipts where task_id=?", (t["task_id"],))["decision"] == "BLOCK"


def test_success_writes_persistent_receipt_with_verification(env):
    db, gate, ctx, _ = env
    k = adapter("t.rcpt", lambda c, p: {"n": 3})
    db.enqueue("analyst", k, "r"); t = db.claim("w", "analyst", 30)
    run_task(db, gate, ctx, t, "w")
    r = gate.rt.store.one("select decision,verification,output_sha256 from receipts where task_id=?", (t["task_id"],))
    assert r["decision"] == "ALLOW" and json.loads(r["verification"])["passed"] is True and r["output_sha256"]


def test_effect_ledger_makes_side_effects_idempotent(env):
    db, *_ = env
    assert db.record_effect("promote:a.com", "t1", {}) is True
    assert db.record_effect("promote:a.com", "t2", {}) is False
    assert db.effect_done("promote:a.com")


def test_crash_recovery_does_not_double_execute_effect(env):
    """Worker records its effect then dies before completing; the retry sees the effect and does not repeat it."""
    db, gate, ctx, clk = env
    calls = []
    def ex(c, p):
        key = "fx:1"
        if not c.db.effect_done(key):
            calls.append(1); c.db.record_effect(key, p["_task_id"], {})
        return {"done": True}
    k = adapter("t.fx", ex)
    db.enqueue("analyst", k, "fx"); t1 = db.claim("w1", "analyst", 30)
    ex(ctx, {"_task_id": t1["task_id"]})                     # w1 performs the effect, then "crashes"
    clk.t += 31; db.reap_expired()
    t2 = db.claim("w2", "analyst", 30)
    assert run_task(db, gate, ctx, t2, "w2") == "SUCCEEDED"
    assert len(calls) == 1


def test_sender_adapter_never_touches_gmail():
    import inspect
    src = inspect.getsource(adapters)
    assert "Gmail(" not in src and ".tick(" not in src and "_send_loop" not in src and "send_one" not in src


def test_dashboard_is_get_only_and_localhost(env, monkeypatch):
    import http.client
    from http.server import ThreadingHTTPServer
    from dotz.fleet import dashboard
    db, *_ = env
    monkeypatch.setattr(dashboard, "DB_PATH", db.path)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.H)
    assert srv.server_address[0] == "127.0.0.1"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    def call(method, path, host=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request(method, path, headers={"Host": host or f"127.0.0.1:{port}"})
        r = c.getresponse(); return r.status, r.read()
    assert call("GET", "/healthz")[0] == 200
    assert call("POST", "/api/overview")[0] == 405 and call("DELETE", "/api/overview")[0] == 405
    assert call("GET", "/api/overview", host="evil.example")[0] == 403
    st, body = call("GET", "/api/dots")
    assert st == 200 and len(json.loads(body)["dots"]) == 9
    st, body = call("GET", "/api/overview")
    d = json.loads(body)
    assert d["unknown"]["revenue_usd"] and d["funnel"]["data"] is None   # no snapshot yet -> UNKNOWN, never a fabricated number
    srv.shutdown()


def test_scout_verify_allows_pending_enrichment_to_shrink():
    r = {"engine_heartbeat_age_s": 5, "delta_since_last": {"raw": 3, "candidates": 2, "pending_enrichment": -40, "qualified": 0}}
    assert all(c["ok"] for c in adapters.scout_verify(None, {}, r))
    r["delta_since_last"]["raw"] = -1
    assert not all(c["ok"] for c in adapters.scout_verify(None, {}, r))
