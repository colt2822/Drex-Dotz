"""Generic event envelope and dispatcher for Drex Dotz.

Events are immutable, idempotent (deduplicated by event_id), and trigger Dot runs.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional

from dotz.store import Store


@dataclass
class EventEnvelope:
    event_id: str
    event_type: str
    timestamp: float
    source: str
    subject: Optional[str] = None
    payload: Optional[Dict[str, Any]] = None

    @classmethod
    def create(
        cls,
        event_type: str,
        source: str,
        subject: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
        event_id: Optional[str] = None,
    ) -> EventEnvelope:
        if not event_id:
            # Deterministic hash if possible or unique id
            raw = f"{event_type}:{source}:{subject}:{json.dumps(payload, sort_keys=True) if payload else ''}"
            event_id = f"ev_{hashlib.sha256(raw.encode()).hexdigest()[:16]}"
        return cls(
            event_id=event_id,
            event_type=event_type,
            timestamp=time.time(),
            source=source,
            subject=subject,
            payload=payload or {},
        )


class EventDispatcher:
    def __init__(self, store: Store):
        self.store = store

    def emit(self, event: EventEnvelope) -> bool:
        """Emit an event into the store. Returns True if newly inserted, False if duplicate."""
        payload_ref = json.dumps(event.payload or {}, ensure_ascii=False)
        inserted = self.store.insert(
            "events",
            {
                "event_id": event.event_id,
                "event_type": event.event_type,
                "timestamp": event.timestamp,
                "source": event.source,
                "subject": event.subject,
                "payload_ref": payload_ref,
            },
            ignore=True,
        )
        return inserted

    def list_events(self, limit: int = 50) -> list[dict]:
        return self.store.all(
            "SELECT * FROM events ORDER BY seq DESC LIMIT ?",
            (limit,),
        )
