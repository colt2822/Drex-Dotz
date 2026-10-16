"""Drex capability enforcement for fleet tasks, reusing dotz.runtime's per-Dot gate and append-only receipts."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from dotz.fleet.db import STATE_DIR
from dotz.models import DotConfig
from dotz.runtime import Runtime


class FleetGate:
    def __init__(self, dots: list[dict], root: Path | None = None):
        self.rt = Runtime(root or STATE_DIR / "fleet-runtime")
        self.cfg = {d["name"]: d for d in dots}
        for d in dots:
            if not (self.rt.dots_dir / d["name"] / "config.yaml").is_file():
                self.rt.create_dot(DotConfig.from_dict({
                    "name": d["name"], "mission": d["mission"], "capabilities": d["capabilities"], "budget": d["budget"],
                    "models": {"default": "cheap", "escalate": None}, "drex": {"pack": "safe-local-coding"}}))

    def check(self, dot: str, capability: str) -> tuple[bool, str]:
        return self.rt.load_dot(dot).gate.check_capability(capability)

    def receipt(self, task: dict, capability: str, decision: str, reason: str, result: dict | None = None,
                verification: dict | None = None, upstream: bool = False) -> str:
        dot = self.rt.load_dot(task["dot"])
        return dot.gate.record_receipt(
            task_id=task["task_id"], session_id=f"fleet_{task['task_id']}", capability=capability, action=task["kind"],
            target=task["idem_key"], decision=decision, reason=reason, rule=None if decision == "ALLOW" else "DOT_POLICY_DENIED",
            result=json.dumps(result, default=str)[:1500] if result is not None else None,
            verification=json.dumps(verification, default=str)[:1500] if verification is not None else None,
            upstream_executed=upstream)
