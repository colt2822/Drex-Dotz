"""Phase 19 Runtime Tests for Drex Dotz.

Verifies:
- Create Dot
- Persist Dot to SQLite
- Restart runtime (simulate new process) & verify Dot survives
- Assign & run task
- Lifecycle transitions (CREATED -> IDLE -> RUNNING -> IDLE / STOPPED)
- Scheduled wake via tick
- Event wake & idempotent event emission
- Delegation & child result collection
- Stopping Dot
"""
from __future__ import annotations

import time
from pathlib import Path
import pytest

from dotz.events import EventEnvelope
from dotz.models import DotConfig, DotState
from dotz.runtime import Runtime


def test_create_persist_and_restart(tmp_path: Path):
    root = tmp_path / "rt_persist"
    rt1 = Runtime(root_dir=root)

    cfg = DotConfig(
        name="worker_alpha",
        mission="Persist across restarts",
        schedule=[{"every": "30m"}],
    )
    dot1 = rt1.create_dot(cfg)
    assert dot1.state == DotState.IDLE

    # Assign a task
    t_id = rt1.assign_task("worker_alpha", "Task 1")
    assert t_id.startswith("task_")

    # Simulate restart: create brand new Runtime instance pointing to same root
    del rt1
    rt2 = Runtime(root_dir=root)
    dots = rt2.list_dots()
    assert len(dots) == 1
    assert dots[0]["name"] == "worker_alpha"
    assert dots[0]["state"] == "IDLE"

    dot2 = rt2.load_dot("worker_alpha")
    assert dot2.state == DotState.IDLE
    assert dot2.config.mission == "Persist across restarts"


def test_lifecycle_and_task_execution(tmp_path: Path):
    rt = Runtime(root_dir=tmp_path / "rt_exec")
    cfg = DotConfig(name="worker_beta", mission="Execute tasks")
    dot = rt.create_dot(cfg)

    task_id = rt.assign_task("worker_beta", "Analyze metrics")
    task_row = rt.store.one("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
    assert task_row["status"] == "QUEUED"

    exec_res = rt.execute_task(task_id)
    assert exec_res["status"] == "COMPLETED"
    assert dot.state == DotState.IDLE

    task_after = rt.store.one("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
    assert task_after["status"] == "COMPLETED"
    assert task_after["result"] is not None


def test_scheduled_wake(tmp_path: Path):
    rt = Runtime(root_dir=tmp_path / "rt_sched")
    cfg = DotConfig(
        name="sched_worker",
        mission="Run on interval",
        schedule=[{"every": "1s"}],
    )
    dot = rt.create_dot(cfg)

    # Fast forward / sleep slightly over 1s
    time.sleep(1.05)
    triggered = rt.tick()
    assert len(triggered) >= 1
    t_id = triggered[0]

    # Verify task was created in store
    task = rt.store.one("SELECT * FROM tasks WHERE task_id = ?", (t_id,))
    assert task["dot"] == "sched_worker"
    assert task["source"] == "schedule"


def test_idempotent_event_wake(tmp_path: Path):
    rt = Runtime(root_dir=tmp_path / "rt_events")
    cfg = DotConfig(name="event_worker", mission="React to events")
    rt.create_dot(cfg)

    ev1 = EventEnvelope.create(
        event_type="github.issue_opened",
        source="github_webhook",
        subject="issue_101",
        payload={"title": "Bug in parser"},
        event_id="ev_fixed_123",
    )

    # First emit -> newly inserted
    assert rt.events.emit(ev1) is True

    # Duplicate emit -> ignored
    assert rt.events.emit(ev1) is False

    events = rt.events.list_events()
    assert len(events) == 1
    assert events[0]["event_id"] == "ev_fixed_123"


def test_delegation_and_child_task(tmp_path: Path):
    rt = Runtime(root_dir=tmp_path / "rt_deleg")
    parent_cfg = DotConfig(
        name="parent_dot",
        mission="Coordinate",
        delegate_capabilities=["files.read", "notes.write"],
    )
    child_cfg = DotConfig(
        name="child_dot",
        mission="Assist",
        capabilities=parent_cfg.capabilities,
    )
    rt.create_dot(parent_cfg)
    rt.create_dot(child_cfg)

    parent_task_id = rt.assign_task("parent_dot", "Top level obligation")
    child_task_id = rt.delegate_task(
        parent_dot_name="parent_dot",
        parent_task_id=parent_task_id,
        child_dot_name="child_dot",
        task_title="Sub-investigation",
        delegated_capabilities=["files.read"],
    )

    child_task = rt.store.one("SELECT * FROM tasks WHERE task_id = ?", (child_task_id,))
    assert child_task["parent_task_id"] == parent_task_id
    assert child_task["parent_dot"] == "parent_dot"

    # Execute child task
    res = rt.execute_task(child_task_id)
    assert res["status"] == "COMPLETED"


def test_stop_dot(tmp_path: Path):
    rt = Runtime(root_dir=tmp_path / "rt_stop")
    cfg = DotConfig(name="retiring_dot", mission="Stop cleanly")
    dot = rt.create_dot(cfg)
    assert dot.state == DotState.IDLE

    dot.transition_to(DotState.STOPPED, reason="Manual shutdown")
    assert dot.state == DotState.STOPPED
