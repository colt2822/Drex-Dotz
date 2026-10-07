"""Integration tests for Hermes plugin and optional NOVA adapter."""
from __future__ import annotations

import json
from pathlib import Path
from dotz.runtime import Runtime
from dotz.integrations.hermes.plugin import register
from dotz.integrations.nova.adapter import NovaAdapter


class MockHermesContext:
    def __init__(self, runtime_dir: Path):
        self.config = {"dotz_runtime_dir": str(runtime_dir)}
        self.tools = {}

    def get_config(self, key, default=None):
        return self.config.get(key, default)

    def register_tool(self, name, toolset, handler, description="", schema=None):
        self.tools[name] = handler


def test_hermes_integration_plugin(tmp_path: Path):
    ctx = MockHermesContext(tmp_path / "hermes_dotz")
    register(ctx)

    assert "dots_list" in ctx.tools
    assert "dots_create" in ctx.tools
    assert "dots_assign" in ctx.tools
    assert "dots_status" in ctx.tools

    # Create dot via plugin tool
    res = ctx.tools["dots_create"]({
        "payload": {
            "name": "hermes_worker",
            "mission": "Handle hermes tasks",
            "capabilities": {"allow": ["files.read"], "deny": ["shell.execute"]},
        }
    })
    data = json.loads(res)
    assert data["status"] == "created"
    assert data["dot"] == "hermes_worker"

    # Assign task
    assign_res = json.loads(ctx.tools["dots_assign"]({
        "dot": "hermes_worker",
        "task": "Quick inspection",
    }))
    assert assign_res["status"] == "assigned"
    assert "task_id" in assign_res


def test_nova_adapter_fallback():
    adapter = NovaAdapter("http://127.0.0.1:59999/nonexistent")
    assert adapter.is_available() is False
    res = adapter.delegate_engineering_task(
        task="refactor",
        files=["a.py"],
        verifier_command="pytest",
        dot_name="dev",
    )
    assert res["success"] is False
    assert "unavailable" in res["error"]
