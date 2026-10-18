"""One fleet process: scheduler + one worker thread per operational dot. Single instance enforced by flock."""
from __future__ import annotations

import fcntl
import json
import os
import threading
import time
import traceback

from dotz.fleet.adapters import ADAPTERS, SNAPSHOT_KIND, Ctx, TaskFailed
from dotz.fleet.db import STATE_DIR, FleetDB
from dotz.fleet.dots import DOTS, SCHEDULES
from dotz.fleet.gate import FleetGate

LEASE_S = 120
BEAT_S = 10


def register(db: FleetDB) -> None:
    for d in DOTS:
        db.upsert_dot(d)
    for dot, kind, every in SCHEDULES:
        db.set_schedule(dot, kind, every)


def run_task(db: FleetDB, gate: FleetGate, ctx: Ctx, task: dict, worker_id: str) -> str:
    """Capability check -> execute -> verify -> fenced complete. Returns final status string."""
    started = ctx.now()
    ad = ADAPTERS.get(task["kind"])
    dot = db.one("SELECT * FROM fleet_dots WHERE name=?", (task["dot"],))
    if not dot or not dot["operational"]:
        rid = gate.receipt(task, "fleet.run", "BLOCK", f"dot {task['dot']} is NOT_OPERATIONAL") if dot else None
        db.fail(task, worker_id, "dot not operational: refusing instead of faking completion", receipt_id=rid, started=started, terminal=True)
        return "DEAD"
    if not ad:
        db.fail(task, worker_id, f"no adapter for kind {task['kind']}", started=started, terminal=True)
        return "DEAD"
    ok, reason = gate.check(task["dot"], ad.capability)
    if not ok:
        rid = gate.receipt(task, ad.capability, "BLOCK", reason)
        db.fail(task, worker_id, f"Drex capability denied: {reason}", receipt_id=rid, started=started, terminal=True)
        return "DEAD"
    payload = json.loads(task["payload"] or "{}")
    payload["_task_id"] = task["task_id"]
    try:
        result = ad.execute(ctx, payload)
        checks = ad.verify(ctx, payload, result)
        verification = {"passed": all(c["ok"] for c in checks), "checks": checks, "verified_at": ctx.now()}
    except TaskFailed as e:
        rid = gate.receipt(task, ad.capability, "ALLOW", "executed; failed", verification={"error": str(e)})
        return db.fail(task, worker_id, str(e), receipt_id=rid, started=started, terminal=e.terminal) or "LOST"
    except Exception as e:
        rid = gate.receipt(task, ad.capability, "ALLOW", "executed; raised", verification={"error": f"{type(e).__name__}: {e}"})
        return db.fail(task, worker_id, f"{type(e).__name__}: {str(e)[:300]}", receipt_id=rid, started=started) or "LOST"
    if not verification["passed"]:
        bad = [c["check"] for c in checks if not c["ok"]]
        rid = gate.receipt(task, ad.capability, "ALLOW", "executed; verification failed", result=result, verification=verification)
        db.snapshot(SNAPSHOT_KIND.get(task["kind"], task["kind"]) + ".unverified", result, task["task_id"])
        return db.fail(task, worker_id, "verification failed: " + "; ".join(bad), receipt_id=rid, started=started) or "LOST"
    rid = gate.receipt(task, ad.capability, "ALLOW", "executed and verified", result=result, verification=verification, upstream=task["kind"] == "sales.promote")
    db.snapshot(SNAPSHOT_KIND.get(task["kind"], task["kind"]), result, task["task_id"])
    return "SUCCEEDED" if db.complete(task, worker_id, result, verification, rid, started) else "LOST"


class Supervisor:
    def __init__(self, db: FleetDB | None = None, ctx: Ctx | None = None, gate: FleetGate | None = None, poll_s: float = 2.0):
        self.db = db or FleetDB()
        self.ctx = ctx or Ctx(self.db)
        register(self.db)
        self.gate = gate or FleetGate(DOTS)
        self.stop = threading.Event()
        self.poll_s = poll_s
        self.threads: list[threading.Thread] = []
        self.current: dict[str, tuple[str, int, float] | None] = {}
        self.pid = os.getpid()

    def worker(self, dot: str) -> None:
        wid = f"{dot}@{self.pid}"
        while not self.stop.is_set():
            self.db.worker_beat(wid, dot, None)
            task = self.db.claim(wid, dot, LEASE_S)
            if not task:
                self.stop.wait(self.poll_s)
                continue
            ad = ADAPTERS.get(task["kind"])
            self.current[wid] = (task["task_id"], task["attempts"], time.time() + (ad.timeout_s if ad else 60))
            self.db.worker_beat(wid, dot, task["task_id"], "BUSY")
            try:
                run_task(self.db, self.gate, self.ctx, task, wid)
            except Exception:
                traceback.print_exc()
            finally:
                self.current[wid] = None
        self.db.worker_stop(wid)

    def beat_loop(self) -> None:
        """Heartbeats + lease extension for in-flight tasks; also scheduler and reaper. A hung worker thread stops
        extending nothing new, but a dead process stops everything and leases expire."""
        last_prune = 0.0
        while not self.stop.is_set():
            for wid, cur in list(self.current.items()):
                dot = wid.split("@")[0]
                self.db.worker_beat(wid, dot, cur[0] if cur else None, "BUSY" if cur else "UP")
                if cur and time.time() < cur[2]:  # a hung adapter stops getting lease extensions after its own timeout
                    self.db.extend_lease(cur[0], wid, cur[1], LEASE_S)
            self.db.run_schedules()
            for t in self.db.reap_expired():
                print(f"reaped expired lease {t['task_id']} ({t['kind']}) owner={t['lease_owner']}", flush=True)
            if time.time() - last_prune > 3600:
                self.db.prune_snapshots()
                last_prune = time.time()
            self.stop.wait(BEAT_S / 2)

    def hunter_loop(self) -> None:
        """PR hunter: advance the active contribution job one stage (or claim the next issue), paced by next_delay.
        Replaces the hourly systemd timer; external writes stay gated by auto_external_submission."""
        from dotz.fleet import hunter_pipeline
        from dotz.fleet.contributions import HunterConfig
        self.stop.wait(30)
        while not self.stop.is_set():
            delay = 3600.0
            try:
                config = HunterConfig.load()
                if config.enabled:
                    result = hunter_pipeline.tick(config)
                    delay = hunter_pipeline.next_delay(result)
                    if result.get("status") != "IDLE" or result.get("claim"):
                        print(f"hunter: {json.dumps(result, default=str)[:300]}", flush=True)
            except Exception:
                traceback.print_exc()
                delay = hunter_pipeline.next_delay({}, errored=True)
            self.stop.wait(delay)

    def start(self) -> None:
        # stale workers of a previous process incarnation are not alive: mark them
        with self.db.lock:
            self.db.c.execute("UPDATE fleet_workers SET status='STOPPED', current_task=NULL WHERE pid!=? AND status!='STOPPED'", (self.pid,))
        ops = [d["name"] for d in DOTS if d["operational"]]
        self.threads = [threading.Thread(target=self.worker, args=(n,), daemon=True, name=f"w-{n}") for n in ops]
        self.threads.append(threading.Thread(target=self.beat_loop, daemon=True, name="beat"))
        if os.environ.get("FETT_HUNTER_THREAD", "1") != "0":
            self.threads.append(threading.Thread(target=self.hunter_loop, daemon=True, name="hunter"))
        for t in self.threads:
            t.start()

    def shutdown(self) -> None:
        self.stop.set()
        for t in self.threads:
            t.join(timeout=30)


def main() -> None:
    import signal
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock = open(STATE_DIR / "fleet.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("another fleet supervisor holds the lock")
    sup = Supervisor()
    signal.signal(signal.SIGTERM, lambda *_: sup.stop.set())
    signal.signal(signal.SIGINT, lambda *_: sup.stop.set())
    sup.start()
    print(f"fleet supervisor up pid={sup.pid} dots={[d['name'] for d in DOTS if d['operational']]}", flush=True)
    sup.stop.wait()
    sup.shutdown()
