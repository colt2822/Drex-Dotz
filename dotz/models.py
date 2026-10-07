"""Core definitions, Dot schema parsing, lifecycle states, and capability models."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional
import yaml


class DotState(str, Enum):
    CREATED = "CREATED"
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    STOPPED = "STOPPED"


VALID_TRANSITIONS: dict[DotState, set[DotState]] = {
    DotState.CREATED: {DotState.IDLE, DotState.STOPPED},
    DotState.IDLE: {DotState.RUNNING, DotState.STOPPED, DotState.BLOCKED},
    DotState.RUNNING: {DotState.IDLE, DotState.WAITING, DotState.BLOCKED, DotState.FAILED, DotState.STOPPED},
    DotState.WAITING: {DotState.RUNNING, DotState.BLOCKED, DotState.FAILED, DotState.STOPPED},
    DotState.BLOCKED: {DotState.IDLE, DotState.RUNNING, DotState.STOPPED},
    DotState.FAILED: {DotState.IDLE, DotState.STOPPED},
    DotState.STOPPED: {DotState.IDLE},  # Can be restarted
}


@dataclass
class DotCapabilities:
    allow: List[str] = field(default_factory=list)
    deny: List[str] = field(default_factory=list)

    def is_explicitly_denied(self, cap: str) -> bool:
        for d in self.deny:
            if cap == d or cap.startswith(d.rstrip("*")):
                return True
        return False

    def is_allowed(self, cap: str) -> bool:
        if self.is_explicitly_denied(cap):
            return False
        for a in self.allow:
            if a == cap or a == "*" or (a.endswith("*") and cap.startswith(a[:-1])):
                return True
        return False


@dataclass
class DotConfig:
    name: str
    version: int = 1
    mission: str = ""
    models: Dict[str, str] = field(default_factory=lambda: {"default": "cheap", "escalate": "frontier"})
    capabilities: DotCapabilities = field(default_factory=DotCapabilities)
    workspace: Dict[str, Any] = field(default_factory=lambda: {"mode": "isolated"})
    budget: Dict[str, Any] = field(default_factory=lambda: {"compute_daily_usd": 2.00})
    schedule: List[Dict[str, Any]] = field(default_factory=list)
    events: List[str] = field(default_factory=list)
    escalate: List[str] = field(default_factory=list)
    delegate_capabilities: List[str] = field(default_factory=list)
    drex: Dict[str, Any] = field(default_factory=lambda: {"pack": "safe-local-coding"})

    @classmethod
    def from_dict(cls, data: dict) -> DotConfig:
        caps_raw = data.get("capabilities", {})
        caps = DotCapabilities(
            allow=list(caps_raw.get("allow", [])),
            deny=list(caps_raw.get("deny", [])),
        )
        return cls(
            name=data["name"],
            version=data.get("version", 1),
            mission=data.get("mission", "").strip(),
            models=data.get("models", {"default": "cheap", "escalate": "frontier"}),
            capabilities=caps,
            workspace=data.get("workspace", {"mode": "isolated"}),
            budget=data.get("budget", {"compute_daily_usd": 2.00}),
            schedule=data.get("schedule", []),
            events=data.get("events", []),
            escalate=data.get("escalate", []),
            delegate_capabilities=data.get("delegate_capabilities", caps.allow),
            drex=data.get("drex", {}),
        )

    @classmethod
    def from_yaml_file(cls, path: Path) -> DotConfig:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return cls.from_dict(data)
