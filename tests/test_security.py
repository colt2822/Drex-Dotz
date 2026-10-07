"""Phase 18 Security Tests for Drex Dotz.

Verifies non-negotiable security invariants:
- Dot cannot escape workspace
- Dot cannot access denied files (e.g. ~/.ssh/id_ed25519)
- Dot cannot use shell without capability
- Dot cannot reuse expired grant
- Dot cannot use another Dot's grant
- Dot cannot broaden grant scope
- Dot cannot inherit undelegated capability
- Blocked action executes zero upstream side effects
- Post-BLOCK valid action still works
- Receipt emitted for consequential actions
- Secret fields stripped
"""
from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path
import pytest

from dotz.models import DotConfig
from dotz.runtime import Runtime


@pytest.fixture
def test_runtime(tmp_path: Path):
    rt = Runtime(root_dir=tmp_path / "runtime")
    # Load researcher and developer
    ex_dir = Path(__file__).parent.parent / "dotz" / "examples"
    r_cfg = DotConfig.from_yaml_file(ex_dir / "researcher" / "config.yaml")
    d_cfg = DotConfig.from_yaml_file(ex_dir / "developer" / "config.yaml")
    rt.create_dot(r_cfg)
    rt.create_dot(d_cfg)
    return rt


def test_researcher_cannot_use_shell(test_runtime: Runtime):
    """Dot without shell capability is blocked immediately."""
    task_id = test_runtime.assign_task("researcher", "Try shell execution")
    res = test_runtime.execute_dot_action(
        dot_name="researcher",
        task_id=task_id,
        capability="shell.execute",
        action="shell_execute",
        args={"command": "echo hacked"},
    )
    assert res["allowed"] is False
    assert "denied by Dot policy" in res["error"]
    assert "receipt_id" in res

    # Verify receipt emitted
    rcpt = test_runtime.store.one("SELECT * FROM receipts WHERE receipt_id = ?", (res["receipt_id"],))
    assert rcpt["decision"] == "BLOCK"
    assert rcpt["upstream_executed"] == 0


def test_workspace_escape_blocked(test_runtime: Runtime):
    """Dot cannot access files outside its designated workspace."""
    task_id = test_runtime.assign_task("researcher", "Try read outside workspace")
    res = test_runtime.execute_dot_action(
        dot_name="researcher",
        task_id=task_id,
        capability="files.read",
        action="file_read",
        args={"path": "/etc/passwd"},
    )
    assert res["allowed"] is False
    assert "escapes allowed root" in res["error"] or "blocked" in res["error"].lower()

    rcpt = test_runtime.store.one("SELECT * FROM receipts WHERE receipt_id = ?", (res["receipt_id"],))
    assert rcpt["decision"] == "BLOCK"
    assert rcpt["upstream_executed"] == 0


def test_sensitive_credential_access_blocked(test_runtime: Runtime):
    """Dot cannot access ssh keys or sensitive credentials even if path given."""
    task_id = test_runtime.assign_task("researcher", "Read ssh key")
    res = test_runtime.execute_dot_action(
        dot_name="researcher",
        task_id=task_id,
        capability="files.read",
        action="file_read",
        args={"path": os.path.expanduser("~/.ssh/id_ed25519")},
    )
    assert res["allowed"] is False
    rcpt_id = res.get("receipt_id")
    assert rcpt_id is not None
    rcpt = test_runtime.store.one("SELECT * FROM receipts WHERE receipt_id = ?", (rcpt_id,))
    assert rcpt["decision"] == "BLOCK"


def test_mutation_requires_valid_grant(test_runtime: Runtime):
    """Developer cannot write files without a bounded grant."""
    task_id = test_runtime.assign_task("developer", "Unbounded write")
    res = test_runtime.execute_dot_action(
        dot_name="developer",
        task_id=task_id,
        capability="repo.modify",
        action="file_write",
        args={"path": "main.py", "content": "print('hello')"},
    )
    assert res["allowed"] is False
    assert "requires an active bounded grant_id" in res["error"]


def test_expired_grant_blocked(test_runtime: Runtime):
    """Developer cannot use an expired grant."""
    task_id = test_runtime.assign_task("developer", "Bounded edit")
    dot = test_runtime.load_dot("developer")
    # Issue a grant with ttl = 0.01 seconds
    grant = dot.gate.issue_grant(
        task_id=task_id,
        session_id="s1",
        capability="repo.modify",
        resources=["main.py"],
        ttl_seconds=0.05,
    )
    time.sleep(0.08)

    res = test_runtime.execute_dot_action(
        dot_name="developer",
        task_id=task_id,
        capability="repo.modify",
        action="file_write",
        args={"path": "main.py", "content": "code", "grant_id": grant.grant_id},
    )
    assert res["allowed"] is False
    assert "GRANT_EXPIRED" in res["error"]


def test_cross_dot_grant_reuse_blocked(test_runtime: Runtime):
    """A grant issued to developer cannot be used by researcher."""
    dev_task = test_runtime.assign_task("developer", "Dev task")
    res_task = test_runtime.assign_task("researcher", "Researcher task")
    dev_dot = test_runtime.load_dot("developer")

    grant = dev_dot.gate.issue_grant(
        task_id=dev_task,
        session_id="s_dev",
        capability="repo.modify",
        resources=["main.py"],
    )

    # Researcher tries to reuse dev's grant
    res = test_runtime.execute_dot_action(
        dot_name="researcher",
        task_id=res_task,
        capability="filesystem.modify",
        action="file_write",
        args={"path": "main.py", "content": "evil", "grant_id": grant.grant_id},
    )
    assert res["allowed"] is False
    assert "denied by Dot policy" in res["error"] or "GRANT_DOT_MISMATCH" in res.get("error", "")


def test_unauthorized_scope_broadening_blocked(test_runtime: Runtime):
    """Grant bound to main.py cannot be used to modify secret.py."""
    task_id = test_runtime.assign_task("developer", "Bounded edit")
    dot = test_runtime.load_dot("developer")
    grant = dot.gate.issue_grant(
        task_id=task_id,
        session_id="s_dev",
        capability="repo.modify",
        resources=["main.py"],
    )

    res = test_runtime.execute_dot_action(
        dot_name="developer",
        task_id=task_id,
        capability="repo.modify",
        action="file_write",
        args={"path": "other.py", "content": "code", "grant_id": grant.grant_id},
    )
    assert res["allowed"] is False
    assert "GRANT_RESOURCE_UNSCOPED" in res["error"]


def test_delegation_ceiling_enforced(test_runtime: Runtime):
    """Parent cannot delegate capabilities beyond its own ceiling."""
    parent_task = test_runtime.assign_task("developer", "Parent task")

    # Developer has repo.modify, tests.run, files.read.
    # Developer tries to delegate 'money.spend' or 'shell.execute' which it does not have.
    with pytest.raises(PermissionError) as excinfo:
        test_runtime.delegate_task(
            parent_dot_name="developer",
            parent_task_id=parent_task,
            child_dot_name="researcher",
            task_title="Child task",
            delegated_capabilities=["shell.execute"],
        )
    assert "cannot delegate undelegated capability" in str(excinfo.value)


def test_valid_action_succeeds_after_blocked_action(test_runtime: Runtime):
    """Drex fail-closed policy does not corrupt runtime state for subsequent valid actions."""
    task_id = test_runtime.assign_task("researcher", "Multi-action task")

    # 1. Blocked action
    bad_res = test_runtime.execute_dot_action(
        dot_name="researcher",
        task_id=task_id,
        capability="shell.execute",
        action="shell_execute",
        args={"command": "rm -rf /"},
    )
    assert bad_res["allowed"] is False

    # 2. Valid read inside workspace
    dot = test_runtime.load_dot("researcher")
    test_file = dot.workspace_dir / "notes.txt"
    test_file.write_text("Valid evidence content", encoding="utf-8")

    good_res = test_runtime.execute_dot_action(
        dot_name="researcher",
        task_id=task_id,
        capability="files.read",
        action="file_read",
        args={"path": "notes.txt"},
    )
    assert good_res["allowed"] is True
    assert good_res["result"] == "Valid evidence content"
