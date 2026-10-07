"""Hermes plugin adapter exposing Dotz management to Hermes sessions."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

from dotz.runtime import Runtime


def register(ctx):
    """Hermes plugin entrypoint."""
    runtime_dir = Path(ctx.get_config("dotz_runtime_dir", Path.home() / ".dotz"))
    runtime = Runtime(runtime_dir)

    def dots_list(args, **kwargs):
        dots = runtime.list_dots()
        return json.dumps({"dots": dots})

    def dots_create(args, **kwargs):
        payload = args.get("payload", {})
        from dotz.models import DotConfig
        cfg = DotConfig.from_dict(payload)
        dot = runtime.create_dot(cfg)
        return json.dumps({"status": "created", "dot": dot.config.name, "state": dot.state.value})

    def dots_assign(args, **kwargs):
        dot_name = args.get("dot")
        task = args.get("task", "")
        task_id = runtime.assign_task(dot_name=dot_name, title=task, source="hermes")
        return json.dumps({"status": "assigned", "task_id": task_id, "dot": dot_name})

    def dots_status(args, **kwargs):
        dot_name = args.get("dot")
        try:
            dot = runtime.load_dot(dot_name)
            return json.dumps({"dot": dot.config.name, "state": dot.state.value, "workspace": str(dot.workspace_dir)})
        except Exception as e:
            return json.dumps({"error": str(e)})

    def dots_result(args, **kwargs):
        task_id = args.get("task_id")
        task = runtime.store.one("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
        if not task:
            return json.dumps({"error": "Task not found"})
        return json.dumps(task)

    def dots_stop(args, **kwargs):
        dot_name = args.get("dot")
        from dotz.models import DotState
        try:
            dot = runtime.load_dot(dot_name)
            dot.transition_to(DotState.STOPPED, reason="Stopped via Hermes")
            return json.dumps({"dot": dot_name, "state": DotState.STOPPED.value})
        except Exception as e:
            return json.dumps({"error": str(e)})

    def dots_history(args, **kwargs):
        dot_name = args.get("dot")
        receipts = runtime.store.all("SELECT * FROM receipts WHERE dot = ? ORDER BY ts DESC LIMIT 50", (dot_name,))
        return json.dumps({"dot": dot_name, "receipts": receipts})

    tools = [
        ("dots_list", dots_list, "List all active persistent Dot workers"),
        ("dots_create", dots_create, "Create a new Dot worker with configuration"),
        ("dots_assign", dots_assign, "Assign a task obligation to a Dot worker"),
        ("dots_status", dots_status, "Check status and lifecycle state of a Dot worker"),
        ("dots_result", dots_result, "Get result of an assigned Dot task"),
        ("dots_stop", dots_stop, "Stop a Dot worker"),
        ("dots_history", dots_history, "Retrieve Drex capability receipts and audit history"),
    ]

    for name, handler, desc in tools:
        ctx.register_tool(
            name=name,
            toolset="dotz",
            handler=handler,
            description=desc,
            schema={
                "name": name,
                "description": desc,
                "parameters": {"type": "object", "properties": {"dot": {"type": "string"}, "task": {"type": "string"}, "task_id": {"type": "string"}, "payload": {"type": "object"}}},
            },
        )
