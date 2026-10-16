"""python -m dotz.fleet {run|dashboard|status|enqueue|promote|dots}"""
from __future__ import annotations

import argparse
import json
import time

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


def main():
    ap = argparse.ArgumentParser(prog="dotz.fleet")
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("run").set_defaults(f=lambda a: __import__("dotz.fleet.supervisor", fromlist=["main"]).main())
    x = sp.add_parser("dashboard"); x.add_argument("--port", type=int, default=8795); x.set_defaults(f=lambda a: __import__("dotz.fleet.dashboard", fromlist=["main"]).main(a.port))
    sp.add_parser("status").set_defaults(f=cmd_status)
    x = sp.add_parser("promote"); x.add_argument("--limit", type=int, default=25); x.add_argument("--domains"); x.add_argument("--execute", action="store_true"); x.add_argument("--idem"); x.set_defaults(f=cmd_promote)
    x = sp.add_parser("enqueue"); x.add_argument("dot"); x.add_argument("kind"); x.add_argument("--payload", default="{}"); x.add_argument("--idem"); x.set_defaults(f=cmd_enqueue)
    a = ap.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
