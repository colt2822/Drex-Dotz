"""python -m dotz.sales.disco {run|once|status|report|promote|seed}"""
from __future__ import annotations

import argparse
import fcntl
import json
import signal
import time

from dotz.sales.crm_store import CrmStore
from dotz.sales.disco import config
from dotz.sales.disco.engine import Engine
from dotz.sales.policy import Policy
from dotz.sales.worker import STATE_DIR, SalesWorker


def engine() -> Engine:
    return Engine(CrmStore().path, config.load())


def cmd_run(a):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock = open(STATE_DIR / "disco.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("another discovery worker holds the lock")
    e = engine()
    signal.signal(signal.SIGTERM, lambda *_: e.stop_ev.set())
    signal.signal(signal.SIGINT, lambda *_: e.stop_ev.set())
    e.seed_jobs()
    e.run_forever()


def cmd_once(a):
    e = engine()
    t0 = time.time()
    print("seeded", e.seed_jobs(a.regions))
    srcs = [s for s in a.sources.split(",") if e.cfg["sources"][s]["enabled"]]
    for s in srcs:
        for _ in range(a.jobs):
            print(json.dumps(e.run_source_job(s), default=str))
    if a.enrich:
        while True:
            r = e.enrich_batch(a.enrich)
            print(json.dumps(r))
            if not r.get("enriched") or e.store.c.execute("SELECT count(*) FROM disco_candidates WHERE stage='RAW'").fetchone()[0] == 0 or time.time() - t0 > a.max_seconds:
                break
    print(json.dumps({"elapsed_s": round(time.time() - t0, 1), **e.metrics()}, indent=1, default=str))


def cmd_status(a):
    print(json.dumps(engine().metrics(), indent=1, default=str))


def cmd_promote(a):
    e = engine()
    from dotz.sales.luna import Luna
    w = SalesWorker(CrmStore(), Policy(), None, None if a.dry_run else Luna())
    print(json.dumps(e.promote(w, a.limit, a.domains.split(",") if a.domains else None, a.dry_run), indent=1, default=str))


def main():
    ap = argparse.ArgumentParser(prog="dotz.sales.disco")
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("run").set_defaults(f=cmd_run)
    x = sp.add_parser("once"); x.add_argument("--sources", default="osm,search,directory"); x.add_argument("--jobs", type=int, default=1)
    x.add_argument("--regions", type=int, default=1); x.add_argument("--enrich", type=int, default=40); x.add_argument("--max-seconds", type=int, default=600); x.set_defaults(f=cmd_once)
    sp.add_parser("status").set_defaults(f=cmd_status)
    x = sp.add_parser("promote"); x.add_argument("--limit", type=int); x.add_argument("--domains"); x.add_argument("--dry-run", action="store_true"); x.set_defaults(f=cmd_promote)
    a = ap.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
