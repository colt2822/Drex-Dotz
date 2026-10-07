"""Command Line Interface for Drex Dotz."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotz import __version__
from dotz.models import DotConfig, DotState
from dotz.runtime import Runtime


def get_runtime(args) -> Runtime:
    root = Path(args.dir) if hasattr(args, "dir") and args.dir else None
    return Runtime(root)


def cmd_init(args):
    rt = get_runtime(args)
    # Seed standard example dots if requested or directory empty
    examples_dir = Path(__file__).parent / "examples"
    count = 0
    if examples_dir.exists():
        for ex in ["researcher", "developer", "operator"]:
            p = examples_dir / ex / "config.yaml"
            if p.is_file():
                cfg = DotConfig.from_yaml_file(p)
                rt.create_dot(cfg)
                count += 1
    print(f"Initialized Drex Dotz runtime at {rt.root_dir} with {count} example Dotz.")


def cmd_list(args):
    rt = get_runtime(args)
    dots = rt.list_dots()
    if not dots:
        print("No Dots configured. Run 'dotz init' to install examples.")
        return
    print(f"{'DOT':<20} {'STATE':<12} {'UPDATED':<20}")
    print("-" * 55)
    for d in dots:
        print(f"{d['name']:<20} {d['state']:<12} {d.get('updated_at', ''):<20}")


def cmd_show(args):
    rt = get_runtime(args)
    dot = rt.load_dot(args.name)
    cfg = dot.config

    print(f"{cfg.name.capitalize()} status: {dot.state.value}\n")
    print(f"Workspace:\n  {dot.workspace_dir}\n")
    print("Model policy:")
    for k, v in cfg.models.items():
        print(f"  {k}: {v}")
    print("\nCapabilities:")
    for a in cfg.capabilities.allow:
        print(f"  ✓ {a}")
    for d in cfg.capabilities.deny:
        print(f"  ✗ {d}")
    print(f"  ✗ filesystem outside workspace")
    print("\nDrex:\n  enforcement active (pack: " + cfg.drex.get("pack", "safe-local-coding") + ")")


def cmd_run(args):
    rt = get_runtime(args)
    dot = rt.load_dot(args.name)
    print(f"{args.name.capitalize()} started\n")
    cmd_show(args)


def cmd_assign(args):
    rt = get_runtime(args)
    task_id = rt.assign_task(dot_name=args.name, title=args.task)
    print(f"Assigned task '{args.task}' to Dot '{args.name}' -> Task ID: {task_id}")
    if args.exec:
        print("Executing task...")
        res = rt.execute_task(task_id)
        print(f"Result: {res['result']}")


def cmd_status(args):
    rt = get_runtime(args)
    dot = rt.load_dot(args.name)
    tasks = rt.store.all("SELECT * FROM tasks WHERE dot = ? ORDER BY created_at DESC LIMIT 5", (args.name,))
    print(f"Dot: {args.name}")
    print(f"State: {dot.state.value}")
    print(f"Workspace: {dot.workspace_dir}")
    print("\nRecent Tasks:")
    for t in tasks:
        print(f"  - [{t['status']}] {t['task_id']}: {t['title']}")


def cmd_stop(args):
    rt = get_runtime(args)
    dot = rt.load_dot(args.name)
    dot.transition_to(DotState.STOPPED, reason="CLI stop")
    print(f"Dot '{args.name}' stopped.")


def cmd_history(args):
    rt = get_runtime(args)
    receipts = rt.store.all(
        "SELECT * FROM receipts WHERE dot = ? ORDER BY ts DESC LIMIT ?",
        (args.name, args.limit),
    )
    if not receipts:
        print(f"No receipts found for Dot '{args.name}'.")
        return
    print(f"{'ACTION':<15} {'DECISION':<10} {'CAPABILITY':<15} {'TARGET':<25} {'RECEIPT_ID'}")
    print("-" * 80)
    for r in receipts:
        target = (r['target'] or '')[:24]
        print(f"{r['action']:<15} {r['decision']:<10} {r['capability']:<15} {target:<25} {r['receipt_id']}")


def cmd_events(args):
    rt = get_runtime(args)
    evs = rt.events.list_events(limit=args.limit)
    if not evs:
        print("No events recorded.")
        return
    print(f"{'EVENT_ID':<20} {'TYPE':<25} {'SOURCE':<15} {'SUBJECT'}")
    print("-" * 75)
    for e in evs:
        print(f"{e['event_id']:<20} {e['event_type']:<25} {e['source']:<15} {e.get('subject') or ''}")


def cmd_emit(args):
    rt = get_runtime(args)
    from dotz.events import EventEnvelope
    payload = {}
    if args.payload:
        try:
            payload = json.loads(args.payload)
        except Exception:
            payload = {"raw": args.payload}
    ev = EventEnvelope.create(
        event_type=args.type,
        source=args.source,
        subject=args.subject,
        payload=payload,
    )
    inserted = rt.events.emit(ev)
    status = "emitted" if inserted else "duplicate (ignored)"
    print(f"Event {ev.event_id} {status}.")


def cmd_tick(args):
    rt = get_runtime(args)
    triggered = rt.tick()
    print(f"Scheduler tick: {len(triggered)} tasks triggered: {triggered}")


def main():
    parser = argparse.ArgumentParser(prog="dotz", description="Drex Dotz: Persistent autonomous workers with bounded Drex capabilities")
    parser.add_argument("--dir", default=None, help="Root runtime directory (default: ./dotz_runtime)")
    parser.add_argument("--version", action="version", version=f"drex-dotz {__version__}")
    sub = parser.add_subparsers(dest="cmd")

    p_init = sub.add_parser("init", help="Initialize Dotz runtime environment")
    p_init.set_defaults(func=cmd_init)

    p_list = sub.add_parser("list", help="List registered Dots")
    p_list.set_defaults(func=cmd_list)

    p_show = sub.add_parser("show", help="Show Dot details and capabilities")
    p_show.add_argument("name", help="Name of Dot")
    p_show.set_defaults(func=cmd_show)

    p_run = sub.add_parser("run", help="Start and run a Dot")
    p_run.add_argument("name", help="Name of Dot")
    p_run.set_defaults(func=cmd_run)

    p_assign = sub.add_parser("assign", help="Assign a task to a Dot")
    p_assign.add_argument("name", help="Name of Dot")
    p_assign.add_argument("task", help="Task description")
    p_assign.add_argument("--exec", action="store_true", help="Execute immediately")
    p_assign.set_defaults(func=cmd_assign)

    p_status = sub.add_parser("status", help="Get Dot status")
    p_status.add_argument("name", help="Name of Dot")
    p_status.set_defaults(func=cmd_status)

    p_stop = sub.add_parser("stop", help="Stop a Dot")
    p_stop.add_argument("name", help="Name of Dot")
    p_stop.set_defaults(func=cmd_stop)

    p_history = sub.add_parser("history", help="Show Drex capability receipts")
    p_history.add_argument("name", help="Name of Dot")
    p_history.add_argument("--limit", type=int, default=20, help="Max records")
    p_history.set_defaults(func=cmd_history)

    p_events = sub.add_parser("events", help="List recent event records")
    p_events.add_argument("--limit", type=int, default=20, help="Max records")
    p_events.set_defaults(func=cmd_events)

    p_emit = sub.add_parser("emit", help="Emit an event")
    p_emit.add_argument("type", help="Event type")
    p_emit.add_argument("--source", default="cli", help="Source identifier")
    p_emit.add_argument("--subject", default=None, help="Subject identifier")
    p_emit.add_argument("--payload", default=None, help="JSON payload")
    p_emit.set_defaults(func=cmd_emit)

    p_tick = sub.add_parser("tick", help="Run scheduler tick")
    p_tick.set_defaults(func=cmd_tick)

    args = parser.parse_args()
    if not args.cmd:
        parser.print_help()
        sys.exit(1)

    args.func(args)


if __name__ == "__main__":
    main()
