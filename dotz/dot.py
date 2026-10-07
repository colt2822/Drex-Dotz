"""Persistent Dot state, lifecycle transitions, and memory management."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotz.drex_gate import DrexCapabilityGate
from dotz.models import DotConfig, DotState, VALID_TRANSITIONS
from dotz.store import Store


class Dot:
    """A persistent autonomous worker."""

    def __init__(self, config: DotConfig, base_dir: Path, store: Store):
        self.config = config
        self.base_dir = base_dir.resolve()
        self.store = store

        # Layout:
        # dotz/
        #   <name>/
        #     DOT.md
        #     config.yaml
        #     memory/
        #     workspace/
        self.dot_dir = self.base_dir / config.name
        self.memory_dir = self.dot_dir / "memory"
        self.workspace_dir = self.dot_dir / "workspace"
        self.state_dir = self.dot_dir / "state"

        self.dot_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.memory_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.workspace_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

        # Initialize Drex capability gate
        self.gate = DrexCapabilityGate(self.config, self.workspace_dir, self.store, self.state_dir)

        # Ensure Dot record exists in store
        self._ensure_record()

    def _ensure_record(self) -> None:
        row = self.store.one("SELECT * FROM dots WHERE name = ?", (self.config.name,))
        if not row:
            now = time.time()
            self.store.insert(
                "dots",
                {
                    "name": self.config.name,
                    "state": DotState.CREATED.value,
                    "created_at": now,
                    "updated_at": now,
                    "last_result": None,
                    "project_id": None,
                },
            )
            self._transition_to(DotState.IDLE, reason="Initial activation")

    @property
    def state(self) -> DotState:
        row = self.store.one("SELECT state FROM dots WHERE name = ?", (self.config.name,))
        if row:
            return DotState(row["state"])
        return DotState.CREATED

    def transition_to(self, new_state: DotState, reason: str = "") -> None:
        self._transition_to(new_state, reason)

    def _transition_to(self, new_state: DotState, reason: str = "") -> None:
        curr = self.state
        if curr != new_state:
            allowed = VALID_TRANSITIONS.get(curr, set())
            if new_state not in allowed:
                raise ValueError(f"Illegal Dot state transition from {curr} to {new_state}")

        now = time.time()
        self.store.x(
            "UPDATE dots SET state = ?, updated_at = ? WHERE name = ?",
            (new_state.value, now, self.config.name),
        )
        self.store.insert(
            "transitions",
            {
                "dot": self.config.name,
                "from_state": curr.value,
                "to_state": new_state.value,
                "reason": reason,
                "at": now,
            },
        )

    # Memory Primitives
    def read_note(self, name: str) -> Optional[str]:
        path = self.memory_dir / name
        if path.is_file():
            return path.read_text(encoding="utf-8")
        return None

    def write_note(self, name: str, content: str) -> Path:
        path = self.memory_dir / name
        path.write_text(content, encoding="utf-8")
        return path

    def list_notes(self) -> List[str]:
        return sorted([p.name for p in self.memory_dir.glob("*.md") if p.is_file()])

    # Responsibilities
    def register_responsibility(self, name: str, trigger: str, next_due_at: Optional[float] = None) -> None:
        self.store.x(
            """INSERT OR REPLACE INTO responsibilities
               (dot, name, trigger, next_due_at, current_task, last_result, last_run_at)
               VALUES (?, ?, ?, ?, NULL, NULL, NULL)""",
            (self.config.name, name, trigger, next_due_at),
        )

    def get_responsibilities(self) -> List[dict]:
        return self.store.all("SELECT * FROM responsibilities WHERE dot = ?", (self.config.name,))
