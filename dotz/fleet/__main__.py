"""python -m dotz.fleet {run|dashboard|status|enqueue|promote|dots}"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from dotz.fleet.db import FleetDB


def cmd_status(a):
    db = FleetDB()
    print(json.dumps({"tasks": db.counts(),
                      "workers": [dict(r) for r in db.c.execute("SELECT worker_id,dot,pid,status,current_task,round(?-heartbeat_at,1) hb_age_s FROM fleet_workers ORDER BY dot", (time.time(),))],
                      "dots": [dict(r) | {"capabilities": None} for r in db.c.execute("SELECT name,role,operational,not_operational_reason FROM fleet_dots")]}, indent=1, default=str))


def cmd_promote(a):
    from dotz.fleet.supervisor import register
    db = FleetDB()
    register(db)
    stamp = time.strftime("%Y%m%d%H%M", time.gmtime())
    payload = {"dry_run": not a.execute, "limit": a.limit, "domains": a.domains.split(",") if a.domains else None}
    tid, created = db.enqueue("sales", "sales.promote", a.idem or f"promote:{'exec' if a.execute else 'dry'}:{stamp}", payload, max_attempts=1, priority=5)
    print(json.dumps({"task_id": tid, "created": created, "mode": "EXECUTE" if a.execute else "DRY_RUN"}))


def cmd_enqueue(a):
    from dotz.fleet.supervisor import register
    db = FleetDB()
    register(db)
    print(db.enqueue(a.dot, a.kind, a.idem or f"manual:{a.kind}:{int(time.time())}", json.loads(a.payload)))


def cmd_hunter_run(a):
    from dotz.fleet.contributions import GitHubClient, HunterConfig, run_once
    from dotz.fleet.db import DB_PATH
    config = HunterConfig.load(a.config) if a.config else HunterConfig.load()
    result = run_once(GitHubClient(api=a.api), config, a.db or DB_PATH)
    print(json.dumps(result or {"status": "NOT_STARTED", "reason": "disabled or quota/concurrency limit"}, indent=1))


def cmd_hunter_status(a):
    from dotz.fleet.contributions import HunterConfig, _db
    from dotz.fleet.db import DB_PATH
    config = HunterConfig.load(a.config) if a.config else HunterConfig.load()
    c = _db(a.db or DB_PATH)
    try:
        jobs = [dict(r) for r in c.execute("SELECT job_id,issue_url,repository,number,status,attempts,created_at,updated_at FROM contribution_jobs ORDER BY created_at DESC")]
        runs = [dict(r) for r in c.execute("SELECT run_id,status,detail,created_at FROM contribution_runs ORDER BY created_at DESC LIMIT 20")]
        for row in runs:
            row["detail"] = json.loads(row["detail"]) if row["detail"] else None
        active = sum(row["status"] in {"QUEUED", "RUNNING", "VERIFYING", "DRAFT"} for row in jobs)
        print(json.dumps({"config": {k: getattr(config, k) for k in config.ENV_KEYS},
                          "active_jobs": active, "jobs": jobs, "runs": runs}, indent=1, default=str))
    finally:
        c.close()


def cmd_hunter_advance(a):
    from dotz.fleet import hunter_pipeline
    from dotz.fleet.contributions import HunterConfig
    from dotz.fleet.db import DB_PATH
    config = HunterConfig.load(a.config) if a.config else HunterConfig.load()
    print(json.dumps(hunter_pipeline.tick(config, a.db or DB_PATH), indent=1, default=str))


def cmd_hunter_plan(a):
    from dotz.fleet import hunter_pipeline
    from dotz.fleet.db import DB_PATH
    print(json.dumps(hunter_pipeline.plan(a.job, a.db or DB_PATH), indent=1, default=str))


def main():
    ap = argparse.ArgumentParser(prog="dotz.fleet")
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("run").set_defaults(f=lambda a: __import__("dotz.fleet.supervisor", fromlist=["main"]).main())
    x = sp.add_parser("dashboard"); x.add_argument("--port", type=int, default=8795); x.set_defaults(f=lambda a: __import__("dotz.fleet.dashboard", fromlist=["main"]).main(a.port))
    sp.add_parser("status").set_defaults(f=cmd_status)
    x = sp.add_parser("promote"); x.add_argument("--limit", type=int, default=25); x.add_argument("--domains"); x.add_argument("--execute", action="store_true"); x.add_argument("--idem"); x.set_defaults(f=cmd_promote)
    x = sp.add_parser("enqueue"); x.add_argument("dot"); x.add_argument("kind"); x.add_argument("--payload", default="{}"); x.add_argument("--idem"); x.set_defaults(f=cmd_enqueue)
    x = sp.add_parser("hunter-run"); x.add_argument("--config"); x.add_argument("--db", type=Path); x.add_argument("--api", default="https://api.github.com"); x.set_defaults(f=cmd_hunter_run)
    x = sp.add_parser("hunter-status"); x.add_argument("--config"); x.add_argument("--db", type=Path); x.set_defaults(f=cmd_hunter_status)
    x = sp.add_parser("hunter-advance"); x.add_argument("--config"); x.add_argument("--db", type=Path); x.set_defaults(f=cmd_hunter_advance)
    x = sp.add_parser("hunter-plan"); x.add_argument("job"); x.add_argument("--db", type=Path); x.set_defaults(f=cmd_hunter_plan)
    a = ap.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
