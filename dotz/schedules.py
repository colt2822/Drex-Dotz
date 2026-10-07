"""Scheduling primitives for Drex Dotz.

Supports once, interval, and cron expressions.
Compiles cleanly and can be triggered via Hermes cron (--no-agent --script) or dotz tick.
"""
from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional

try:
    import croniter
    HAS_CRONITER = True
except ImportError:
    HAS_CRONITER = False


def parse_duration_seconds(expr: str) -> float:
    """Parse '30s', '5m', '2h', '1d' into seconds."""
    expr = expr.strip().lower()
    if expr.startswith("every "):
        expr = expr[6:].strip()
    m = re.match(r"^(\d+(?:\.\d+)?)\s*([smhd])?$", expr)
    if not m:
        raise ValueError(f"Invalid duration format: '{expr}'")
    val = float(m.group(1))
    unit = m.group(2) or "s"
    multipliers = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}
    return val * multipliers[unit]


def compute_next_run(schedule_entry: Dict[str, Any], now: Optional[float] = None) -> Optional[float]:
    """Compute the next Unix epoch timestamp for a schedule entry."""
    now = now or time.time()
    if "every" in schedule_entry:
        interval = parse_duration_seconds(str(schedule_entry["every"]))
        return now + interval
    if "cron" in schedule_entry:
        cron_expr = str(schedule_entry["cron"]).strip()
        if not HAS_CRONITER:
            raise RuntimeError("croniter package required for cron schedules")
        itr = croniter.croniter(cron_expr, now)
        return float(itr.get_next())
    if "at" in schedule_entry:
        return float(schedule_entry["at"])
    return None
