"""Central Dot Runtime orchestrating Dot lifecycle, tasks, events, and execution."""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from dotz.dot import Dot
from dotz.events import EventDispatcher, EventEnvelope
from dotz.models import DotConfig, DotState
from dotz.models_router import ModelRouter
from dotz.schedules import compute_next_run
from dotz.store import Store


class Runtime:
    """The Drex Dotz Runtime environment."""

    def __init__(self, root_dir: Optional[Path] = None):
        self.root_dir = (root_dir or Path.cwd() / "dotz_runtime").resolve()
        self.root_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db_path = self.root_dir / "dotz.db"
        self.store = Store(self.db_path)
        self.events = EventDispatcher(self.store)
        self.models = ModelRouter(self.store)
        self.dots_dir = self.root_dir / "dots"
        self.dots_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._dots_cache: Dict[str, Dot] = {}

    def load_dot(self, name: str) -> Dot:
        if name in self._dots_cache:
            return self._dots_cache[name]

        dot_path = self.dots_dir / name / "config.yaml"
        if not dot_path.is_file():
            raise FileNotFoundError(f"Dot config not found at {dot_path}")

        cfg = DotConfig.from_yaml_file(dot_path)
        dot = Dot(cfg, self.dots_dir, self.store)
        self._dots_cache[name] = dot
        return dot

    def create_dot(self, config: DotConfig) -> Dot:
        dot_dir = self.dots_dir / config.name
        dot_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        config_path = dot_dir / "config.yaml"
        dot_md_path = dot_dir / "DOT.md"

        # Serialize config
        import yaml
        raw = {
            "name": config.name,
            "version": config.version,
            "mission": config.mission,
            "models": config.models,
            "capabilities": {
                "allow": config.capabilities.allow,
                "deny": config.capabilities.deny,
            },
            "workspace": config.workspace,
            "budget": config.budget,
            "schedule": config.schedule,
            "escalate": config.escalate,
            "drex": config.drex,
        }
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(raw, f, sort_keys=False)

        if not dot_md_path.exists():
            dot_md_path.write_text(f"# {config.name}\n\n{config.mission}\n", encoding="utf-8")

        dot = Dot(config, self.dots_dir, self.store)
        self._dots_cache[config.name] = dot

        # Register any schedules as responsibilities
        for s in config.schedule:
            nxt = compute_next_run(s)
            dot.register_responsibility(
                name=f"sched_{s.get('every') or s.get('cron')}",
                trigger=f"schedule:{json.dumps(s)}",
                next_due_at=nxt,
            )

        return dot

    def list_dots(self) -> List[dict]:
        return self.store.all("SELECT * FROM dots ORDER BY name ASC")

    def assign_task(
        self,
        dot_name: str,
        title: str,
        expected_output: Optional[str] = None,
        source: str = "manual",
        event_id: Optional[str] = None,
        parent_task_id: Optional[str] = None,
        parent_dot: Optional[str] = None,
        ceiling: Optional[List[str]] = None,
    ) -> str:
        dot = self.load_dot(dot_name)
        task_id = f"task_{uuid.uuid4().hex[:12]}"
        now = time.time()
        session_id = f"sess_{dot_name}_{uuid.uuid4().hex[:8]}"

        self.store.insert(
            "tasks",
            {
                "task_id": task_id,
                "dot": dot_name,
                "title": title,
                "status": "QUEUED",
                "source": source,
                "event_id": event_id,
                "responsibility": None,
                "parent_task_id": parent_task_id,
                "parent_dot": parent_dot,
                "expected_output": expected_output,
                "deadline": None,
                "ceiling": json.dumps(ceiling) if ceiling else None,
                "session_id": session_id,
                "steps": 0,
                "obligation": title,
                "next_action": "start_execution",
                "result": None,
                "created_at": now,
                "updated_at": now,
            },
        )
        return task_id

    def execute_task(self, task_id: str) -> dict:
        """Run a task to completion within bounded Drex capabilities."""
        row = self.store.one("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
        if not row:
            raise ValueError(f"Task {task_id} not found")

        dot = self.load_dot(row["dot"])
        dot.transition_to(DotState.RUNNING, reason=f"Running task {task_id}")

        self.store.x("UPDATE tasks SET status = 'RUNNING', updated_at = ? WHERE task_id = ?", (time.time(), task_id))

        # Check if dot requires model reasoning
        tier = dot.config.models.get("default", "cheap")
        comp = self.models.complete(
            prompt=f"Task: {row['title']}\nExpected output: {row['expected_output'] or 'Completed task'}",
            tier=tier,
            dot=dot.config.name,
            task_id=task_id,
            system=f"You are Dot '{dot.config.name}'. Mission: {dot.config.mission}",
        )

        res_text = comp.text if comp.ok else f"Failed: {comp.error}"
        now = time.time()
        status = "COMPLETED" if comp.ok else "FAILED"

        self.store.x(
            "UPDATE tasks SET status = ?, result = ?, updated_at = ? WHERE task_id = ?",
            (status, res_text, now, task_id),
        )

        dot.transition_to(DotState.IDLE, reason=f"Finished task {task_id} with status {status}")
        return {
            "task_id": task_id,
            "dot": dot.config.name,
            "status": status,
            "result": res_text,
            "tokens": comp.usage.input_tokens + comp.usage.output_tokens,
        }

    # Bounded Action Execution via Drex Gate
    def execute_dot_action(
        self,
        dot_name: str,
        task_id: str,
        capability: str,
        action: str,
        args: dict,
    ) -> dict:
        """Execute a guarded action requested by a Dot.

        Dot -> capability request -> Drex -> ALLOW / BLOCK / bounded grant -> execution -> receipt
        """
        dot = self.load_dot(dot_name)
        task = self.store.one("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
        session_id = task["session_id"] if task else "default"

        # Check delegation ceiling if present
        if task and task.get("ceiling"):
            allowed_ceiling = json.loads(task["ceiling"])
            if capability not in allowed_ceiling:
                # Block immediately
                rcpt = dot.gate.record_receipt(
                    task_id=task_id,
                    session_id=session_id,
                    capability=capability,
                    action=action,
                    target=str(args.get("path") or args.get("command") or args.get("url") or ""),
                    decision="BLOCK",
                    reason=f"Action exceeds delegated capability ceiling: {capability}",
                    rule="DELEGATION_CEILING_EXCEEDED",
                )
                return {"allowed": False, "error": f"Delegation ceiling exceeded for {capability}", "receipt_id": rcpt}

        # Check Dot policy capabilities
        allowed, reason = dot.gate.check_capability(capability)
        if not allowed:
            rcpt = dot.gate.record_receipt(
                task_id=task_id,
                session_id=session_id,
                capability=capability,
                action=action,
                target=str(args.get("path") or args.get("command") or args.get("url") or ""),
                decision="BLOCK",
                reason=reason,
                rule="DOT_POLICY_DENIED",
            )
            return {"allowed": False, "error": reason, "receipt_id": rcpt}

        # Check Grant Requirements for mutation operations
        grant_id = args.get("grant_id")
        if action in {"file_write", "repo_modify", "service_restart"}:
            if not grant_id:
                rcpt = dot.gate.record_receipt(
                    task_id=task_id,
                    session_id=session_id,
                    capability=capability,
                    action=action,
                    target=str(args.get("path") or ""),
                    decision="BLOCK",
                    reason="Consequential mutation requires an active bounded grant_id",
                    rule="MUTATION_GRANT_REQUIRED",
                )
                return {"allowed": False, "error": "Mutation requires an active bounded grant_id", "receipt_id": rcpt}

            grant = dot.gate.get_grant(grant_id)
            if not grant:
                rcpt = dot.gate.record_receipt(
                    task_id=task_id,
                    session_id=session_id,
                    capability=capability,
                    action=action,
                    target=str(args.get("path") or ""),
                    decision="BLOCK",
                    reason="Grant not found",
                    rule="GRANT_NOT_FOUND",
                )
                return {"allowed": False, "error": "Invalid grant_id", "receipt_id": rcpt}

            valid, v_reason = grant.is_valid(
                dot=dot.config.name,
                task_id=task_id,
                workspace=str(dot.workspace_dir),
                resource=args.get("path"),
            )
            if not valid:
                rcpt = dot.gate.record_receipt(
                    task_id=task_id,
                    session_id=session_id,
                    capability=capability,
                    action=action,
                    target=str(args.get("path") or ""),
                    decision="BLOCK",
                    reason=f"Grant check failed: {v_reason}",
                    rule=v_reason,
                    grant_id=grant_id,
                )
                return {"allowed": False, "error": f"Grant invalid: {v_reason}", "receipt_id": rcpt}

            dot.gate.consume_grant_use(grant_id)

        # Dispatch via Drex Firewall Guarded Adapters
        result = None
        drex_action_id = None
        err = None
        upstream = False

        if action == "file_read":
            path = args.get("path")
            res = dot.gate.firewall.filesystem.read_file(path, cwd=str(dot.workspace_dir))
            drex_action_id = res.firewall_decision.action_id if res.firewall_decision else None
            if not res.allowed:
                rcpt = dot.gate.record_receipt(
                    task_id=task_id,
                    session_id=session_id,
                    capability=capability,
                    action=action,
                    target=path,
                    decision="BLOCK",
                    reason=res.error or "Blocked by firewall",
                    drex_action_id=drex_action_id,
                )
                return {"allowed": False, "error": res.error, "receipt_id": rcpt}
            result = res.content
            upstream = True

        elif action == "file_write":
            path = args.get("path")
            content = args.get("content", "")
            res = dot.gate.firewall.filesystem.modify_file(path, content, cwd=str(dot.workspace_dir))
            drex_action_id = res.firewall_decision.action_id if res.firewall_decision else None
            if not res.allowed:
                rcpt = dot.gate.record_receipt(
                    task_id=task_id,
                    session_id=session_id,
                    capability=capability,
                    action=action,
                    target=path,
                    decision="BLOCK",
                    reason=res.error or "Blocked by firewall",
                    drex_action_id=drex_action_id,
                    grant_id=grant_id,
                )
                return {"allowed": False, "error": res.error, "receipt_id": rcpt}
            result = "Write successful"
            upstream = True

        elif action == "shell_execute":
            cmd = args.get("command")
            res = dot.gate.firewall.shell_adapter.execute(cmd, cwd=str(dot.workspace_dir))
            drex_action_id = res.firewall_decision.action_id if res.firewall_decision else None
            if not res.allowed:
                rcpt = dot.gate.record_receipt(
                    task_id=task_id,
                    session_id=session_id,
                    capability=capability,
                    action=action,
                    target=cmd,
                    decision="BLOCK",
                    reason=res.error or "Blocked by firewall",
                    drex_action_id=drex_action_id,
                )
                return {"allowed": False, "error": res.error, "receipt_id": rcpt}
            result = res.stdout or res.stderr
            upstream = True

        elif action == "http_get":
            url = args.get("url")
            res = dot.gate.firewall.http.request("GET", url)
            drex_action_id = res.firewall_decision.action_id if res.firewall_decision else None
            if not res.allowed:
                rcpt = dot.gate.record_receipt(
                    task_id=task_id,
                    session_id=session_id,
                    capability=capability,
                    action=action,
                    target=url,
                    decision="BLOCK",
                    reason=res.error or "Blocked by firewall",
                    drex_action_id=drex_action_id,
                )
                return {"allowed": False, "error": res.error, "receipt_id": rcpt}
            result = res.text
            upstream = True

        else:
            return {"allowed": False, "error": f"Unknown action: {action}"}

        rcpt = dot.gate.record_receipt(
            task_id=task_id,
            session_id=session_id,
            capability=capability,
            action=action,
            target=str(args.get("path") or args.get("command") or args.get("url") or ""),
            decision="ALLOW",
            reason="Guarded execution permitted",
            grant_id=grant_id,
            result=result,
            upstream_executed=upstream,
            drex_action_id=drex_action_id,
        )
        return {"allowed": True, "result": result, "receipt_id": rcpt}

    # Delegation (child_caps <= parent_caps)
    def delegate_task(
        self,
        parent_dot_name: str,
        parent_task_id: str,
        child_dot_name: str,
        task_title: str,
        delegated_capabilities: List[str],
        expected_output: Optional[str] = None,
    ) -> str:
        parent = self.load_dot(parent_dot_name)
        child = self.load_dot(child_dot_name)

        # Enforce capability ceiling: child_capabilities <= parent_delegatable_capabilities
        parent_caps = set(parent.config.delegate_capabilities)
        for cap in delegated_capabilities:
            if cap not in parent_caps and "*" not in parent_caps:
                raise PermissionError(
                    f"Parent {parent_dot_name} cannot delegate undelegated capability: {cap}"
                )

        child_task_id = self.assign_task(
            dot_name=child_dot_name,
            title=task_title,
            expected_output=expected_output,
            source="delegation",
            parent_task_id=parent_task_id,
            parent_dot=parent_dot_name,
            ceiling=delegated_capabilities,
        )

        self.store.insert(
            "delegations",
            {
                "child_task_id": child_task_id,
                "parent_dot": parent_dot_name,
                "parent_task_id": parent_task_id,
                "child_dot": child_dot_name,
                "ceiling": json.dumps(delegated_capabilities),
                "at": time.time(),
            },
        )
        return child_task_id

    # Periodic Scheduler Tick
    def tick(self) -> List[str]:
        """Check all scheduled responsibilities and wake due Dots."""
        now = time.time()
        due = self.store.all(
            "SELECT * FROM responsibilities WHERE next_due_at IS NOT NULL AND next_due_at <= ?",
            (now,),
        )
        triggered_tasks = []
        for r in due:
            dot_name = r["dot"]
            resp_name = r["name"]
            trigger = r["trigger"]
            task_id = self.assign_task(
                dot_name=dot_name,
                title=f"Scheduled trigger: {resp_name}",
                source="schedule",
            )
            # Recompute next due
            if trigger.startswith("schedule:"):
                s_data = json.loads(trigger[9:])
                nxt = compute_next_run(s_data, now=now)
            else:
                nxt = None
            self.store.x(
                "UPDATE responsibilities SET next_due_at = ?, last_run_at = ? WHERE dot = ? AND name = ?",
                (nxt, now, dot_name, resp_name),
            )
            triggered_tasks.append(task_id)
        return triggered_tasks
