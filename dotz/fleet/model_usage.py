"""DREX FLEET COMMAND: Model Usage & Rate Limit Visibility telemetry provider.

Real data only. Read-only adapters with least privilege.
No credentials, emails, or secret identifiers are ever logged, cached, or returned.
Distinguishes MEASURED vs ESTIMATED vs UNKNOWN data.
Handles missing, corrupt, or stale sources gracefully.
"""
from __future__ import annotations

import glob
import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Default system paths (overrideable for synthetic testing)
HOME = Path.home()
DEFAULT_HERMES_DIR = HOME / ".hermes"
DEFAULT_HERMES_PROFILE = "colt2822"
DEFAULT_FETT_DIR = DEFAULT_HERMES_DIR / "profiles" / DEFAULT_HERMES_PROFILE / "fett-lite"
DEFAULT_CLAUDE_DIR = HOME / ".claude"
DEFAULT_CODEX_DIR_1 = HOME / ".codex"
DEFAULT_CODEX_DIR_2 = HOME / ".codex-this"
DEFAULT_KIMI_DIR = HOME / ".kimi-code"
DEFAULT_GROK_DIR = HOME / ".grok"
DEFAULT_DOTZ_STATE_DIR = HOME / ".local" / "state" / "dotz"
DEFAULT_ROUTER_STATUS = HOME / ".local" / "state" / "elyreon" / "model-router" / "status.json"

# Status constants
ST_AVAILABLE = "AVAILABLE"
ST_RATE_LIMITED = "RATE_LIMITED"
ST_AUTH_FAILED = "AUTH_FAILED"
ST_DISCONNECTED = "DISCONNECTED"
ST_UNKNOWN = "UNKNOWN"
ST_OWNER_RESERVED = "OWNER_RESERVED"
ST_BLOCKED = "BLOCKED_BY_POLICY"

# Data classification constants
SRC_MEASURED = "MEASURED"
SRC_ESTIMATED = "ESTIMATED"
SRC_UNKNOWN = "UNKNOWN"
TAG_UNKNOWN_DATA = "UNKNOWN_DATA"
TAG_STALE_DATA = "STALE_DATA"
TAG_PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"

# Known model-to-provider mappings
FETT_AGENT_MODELS = {
    "claude": {"model": "claude-sonnet-5-5", "provider": "anthropic", "role": "BUILD"},
    "codex1": {"model": "gpt-5.6-luna", "provider": "openai", "role": "CODE_QA_AND_FIXES"},
    "codex2": {"model": "gpt-5.6-luna", "provider": "openai", "role": "CODE_QA_AND_FIXES"},
    "kimi": {"model": "k3", "provider": "openai", "role": "OPTIONAL_SPECIALIST"},
    "grok": {"model": "grok-4.7", "provider": "xai", "role": "X_RESEARCH_AND_CONTENT"},
    "agy": {"model": "gemini-3.8-flash", "provider": "google", "role": "RESEARCH_ISOLATED"},
}


def _ro_sqlite(path: Path) -> sqlite3.Connection | None:
    if not path.exists():
        return None
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        c.row_factory = sqlite3.Row
        return c
    except (sqlite3.Error, OSError):
        return None


def _clean_str(s: Any, max_len: int = 140) -> str:
    if s is None:
        return ""
    text = str(s).strip()
    return text[:max_len]


def _mask_secrets(obj: Any) -> Any:
    """Recursively redact any potential credential keys or values."""
    secret_terms = ("password", "passwd", "secret", "bearer", "cookie", "credential", "private_key")
    token_auth_terms = ("api_key", "api_token", "auth_token", "access_token", "refresh_token", "session_token")
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            k_lower = str(k).lower()
            is_secret = (
                any(t in k_lower for t in secret_terms) or
                any(t in k_lower for t in token_auth_terms) or
                (k_lower in ("token", "key", "auth") and not isinstance(v, (int, float, list)))
            )
            is_token_metric = any(ok in k_lower for ok in ("tokens", "token_status", "token_count", "token_usage", "token_tracking", "outputtokens", "inputtokens"))
            if is_secret and not is_token_metric:
                out[k] = "[REDACTED_CREDENTIAL]"
            elif isinstance(v, (dict, list)):
                out[k] = _mask_secrets(v)
            elif isinstance(v, str) and ("@" in v and "." in v and not v.startswith("@")):
                out[k] = "[REDACTED_EMAIL]"
            else:
                out[k] = v
        return out
    elif isinstance(obj, list):
        return [_mask_secrets(item) for item in obj]
    return obj


@dataclass
class ModelUsageEnv:
    hermes_dir: Path = DEFAULT_HERMES_DIR
    hermes_profile: str = DEFAULT_HERMES_PROFILE
    fett_dir: Path = DEFAULT_FETT_DIR
    claude_dir: Path = DEFAULT_CLAUDE_DIR
    codex_dir_1: Path = DEFAULT_CODEX_DIR_1
    codex_dir_2: Path = DEFAULT_CODEX_DIR_2
    kimi_dir: Path = DEFAULT_KIMI_DIR
    grok_dir: Path = DEFAULT_GROK_DIR
    dotz_state_dir: Path = DEFAULT_DOTZ_STATE_DIR
    router_status_path: Path = DEFAULT_ROUTER_STATUS
    now: float = field(default_factory=time.time)


def parse_hermes_rate_limits(log_file: Path, now: float) -> list[dict]:
    """Parse genuine Hermes 429 rate limit events and retry-after intervals."""
    if not log_file.exists():
        return []
    events = []
    try:
        size = log_file.stat().st_size
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            if size > 250_000:
                f.seek(size - 250_000)
            text = f.read()
        
        # Match lines like:
        # 2026-10-16 20:32:37,016 WARNING [...] agent.conversation_loop: Retrying API call in 443.0s (attempt 2/3) thread=... provider=anthropic base_url=... model=claude-sonnet-4-6 ... error=...
        pattern = re.compile(
            r"(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})\s+(?P<lvl>\w+)\s+(?:\[\w+\]\s+)?agent\.conversation_loop:\s+"
            r"Retrying API call in (?P<retry>[\d\.]+)s.*?provider=(?P<provider>\S+).*?model=(?P<model>\S+).*?error=(?P<err>.*)",
            re.MULTILINE
        )
        for m in pattern.finditer(text):
            ts_str = m.group("ts")
            retry_s = float(m.group("retry"))
            provider = m.group("provider")
            model = m.group("model")
            raw_err = m.group("err").strip()
            
            # Extract request ID if present
            m_req = re.search(r"['\"]request_id['\"]:\s*['\"]([^'\"]+)['\"]", raw_err)
            req_id = m_req.group(1) if m_req else None
            
            # Keep provider payload text out of the dashboard. The status and
            # retry metadata are enough to report the event safely.
            clean_msg = "HTTP 429 rate limit response"

            try:
                dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S,%f")
                evt_epoch = dt.timestamp()
            except ValueError:
                evt_epoch = now
            
            expires_at = evt_epoch + retry_s
            remaining_s = max(0.0, expires_at - now)
            is_active = remaining_s > 0

            events.append({
                "ts": evt_epoch,
                "ts_str": ts_str,
                "provider": provider,
                "model": model,
                "account_alias": f"hermes-{provider}",
                "retry_after_s": retry_s,
                "reset_at": datetime.fromtimestamp(expires_at, timezone.utc).isoformat(),
                "remaining_s": round(remaining_s, 1),
                "is_active": is_active,
                "error_type": "RateLimitError (HTTP 429)",
                "error_detail": clean_msg,
                "request_id": req_id,
                "initiating_workflow": "UNKNOWN",
                "initiating_service": "UNKNOWN",
                "source": "hermes.errors.log",
            })
    except Exception as e:
        events.append({
            "ts": now,
            "ts_str": datetime.now(timezone.utc).isoformat(),
            "provider": "unknown",
            "model": "unknown",
            "account_alias": "hermes",
            "retry_after_s": 0,
            "remaining_s": 0,
            "is_active": False,
            "error_type": f"LogParseError: {type(e).__name__}",
            "error_detail": str(e)[:100],
            "source": "hermes.errors.log",
        })
    return sorted(events, key=lambda x: x["ts"], reverse=True)


def parse_claude_session_limits(fett_jobs_dir: Path, now: float) -> list[dict]:
    """Parse Claude session rate limits reported in CLI stdout (e.g. resets 8:40pm)."""
    if not fett_jobs_dir.exists():
        return []
    limits = []
    try:
        for stdout_path in glob.glob(str(fett_jobs_dir / "*" / "*.stdout")):
            p = Path(stdout_path)
            try:
                # Tail last 16KB
                size = p.stat().st_size
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    if size > 16_000:
                        f.seek(size - 16_000)
                    content = f.read()
                
                # You've hit your session limit · resets 8:40pm (America/Chicago)
                m = re.search(r"You've hit your session limit\s*·\s*resets\s*([0-9:]+[ap]m)\s*(?:\(([^)]+)\))?", content)
                if m:
                    reset_time_str = m.group(1)
                    tz_str = m.group(2) or "Local"
                    mtime = p.stat().st_mtime
                    job_id = p.parent.name
                    reset_epoch = None
                    try:
                        zone = ZoneInfo(tz_str) if tz_str != "Local" else datetime.now().astimezone().tzinfo
                        event_local = datetime.fromtimestamp(mtime, zone)
                        reset_clock = datetime.strptime(reset_time_str.upper(), "%I:%M%p").time()
                        reset_local = datetime.combine(event_local.date(), reset_clock, tzinfo=zone)
                        if reset_local.timestamp() <= mtime:
                            reset_local = reset_local + timedelta(days=1)
                        reset_epoch = reset_local.timestamp()
                    except (ValueError, ZoneInfoNotFoundError, OverflowError):
                        reset_epoch = None
                    remaining = max(0.0, reset_epoch - now) if reset_epoch is not None else None
                    limits.append({
                        "ts": mtime,
                        "provider": "anthropic",
                        "model": "claude-sonnet-5-5",
                        "account_alias": "claude-code",
                        "reset_time_str": reset_time_str,
                        "reset_at": datetime.fromtimestamp(reset_epoch, timezone.utc).isoformat() if reset_epoch is not None else None,
                        "remaining_s": round(remaining, 1) if remaining is not None else None,
                        "timezone": tz_str,
                        "fett_job_id": job_id,
                        "error_type": "SessionLimitError",
                        "error_detail": f"Session limit reached · resets {reset_time_str} ({tz_str})",
                        "source": f"fett-lite/{job_id}/{p.name}",
                        "age_s": round(now - mtime, 1),
                        "is_recent": (now - mtime) < 7200,
                        "is_active": remaining is not None and remaining > 0,
                    })
            except OSError:
                continue
    except Exception:
        pass
    return sorted(limits, key=lambda x: x["ts"], reverse=True)


def parse_quota_cache(quota_path: Path, now: float) -> dict:
    """Parse live provider quota cache if present."""
    if not quota_path.exists():
        return {"fetched_at": None, "providers": {}, "status": TAG_UNKNOWN_DATA}
    try:
        with open(quota_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        fetched_at = data.get("fetched_at")
        fetched_epoch = None
        if fetched_at:
            try:
                fetched_epoch = datetime.fromisoformat(str(fetched_at).replace("Z", "+00:00")).timestamp()
            except (TypeError, ValueError):
                fetched_epoch = None
        age_s = max(0.0, now - fetched_epoch) if fetched_epoch is not None else None
        providers_out = {}
        for prov, info in data.get("providers", {}).items():
            if not isinstance(info, dict):
                continue
            windows_out = []
            for w in info.get("windows", []):
                reset_at = w.get("reset_at")
                remaining_s = None
                reset_expired = None
                if reset_at:
                    try:
                        # e.g. 2026-10-17T01:39:59.618035+00:00
                        dt = datetime.fromisoformat(reset_at)
                        reset_epoch = dt.timestamp()
                        reset_expired = reset_epoch <= now
                        remaining_s = max(0.0, reset_epoch - now)
                    except ValueError:
                        pass
                windows_out.append({
                    "label": w.get("label"),
                    "reset_at": reset_at,
                    "remaining_s": round(remaining_s, 1) if remaining_s is not None else None,
                    "reset_expired": reset_expired,
                    "used_percent": w.get("used_percent"),
                })
            providers_out[prov] = {
                "label": info.get("label", prov),
                "plan": info.get("plan"),
                "account_label": info.get("account_label"),
                "unavailable_reason": info.get("unavailable_reason"),
                "details": info.get("details", []),
                "windows": windows_out,
            }
        return {
            "fetched_at": fetched_at,
            "as_of_age_s": round(age_s, 1) if age_s is not None else None,
            "providers": providers_out,
            "status": (SRC_MEASURED if age_s is not None and age_s <= 600
                       else TAG_STALE_DATA if age_s is not None
                       else TAG_UNKNOWN_DATA),
        }
    except Exception as e:
        return {"fetched_at": None, "providers": {}, "status": TAG_UNKNOWN_DATA, "error": str(e)[:80]}


def parse_router_status(status_path: Path, now: float) -> dict:
    """Read only fresh, provider-reported Codex quota state from NOVA Router."""
    if not status_path.exists():
        return {"updated_at": None, "age_s": None, "workers": {}, "status": TAG_UNKNOWN_DATA}
    try:
        data = json.loads(status_path.read_text(encoding="utf-8"))
        updated = data.get("updated_at")
        try:
            updated_epoch = datetime.fromisoformat(str(updated).replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError):
            updated_epoch = None
        age = max(0.0, now - updated_epoch) if updated_epoch is not None else None
        workers = {}
        for worker_id in ("codex_1", "codex_2"):
            source = (data.get("workers") or {}).get(worker_id)
            if not isinstance(source, dict):
                continue
            usage = source.get("provider_usage") if isinstance(source.get("provider_usage"), dict) else {}
            observed_at = usage.get("observed_at")
            try:
                observation_age = max(0.0, now - float(observed_at)) if observed_at is not None else None
            except (TypeError, ValueError):
                observation_age = None
            workers[worker_id] = {
                "health": source.get("health", ST_UNKNOWN),
                "health_source": source.get("health_source", SRC_UNKNOWN),
                "usage_state": source.get("usage_state", ST_UNKNOWN),
                "usage_source": source.get("usage_source", SRC_UNKNOWN),
                "observation_age_s": round(observation_age, 1) if observation_age is not None else None,
                "cooldown_until": source.get("cooldown_until"),
                "windows": [
                    {
                        "label": w.get("window"),
                        "window_minutes": w.get("window_minutes"),
                        "used_percent": w.get("used_pct"),
                        "reset_at": w.get("resets_at"),
                        "remaining_s": max(0.0, float(w["resets_at"]) - now)
                        if isinstance(w.get("resets_at"), (int, float)) else None,
                        "reset_expired": isinstance(w.get("resets_at"), (int, float)) and w["resets_at"] <= now,
                    }
                    for w in usage.get("windows", []) if isinstance(w, dict)
                ],
            }
        status = (SRC_MEASURED if age is not None and age <= 600 and workers
                  else TAG_STALE_DATA if age is not None
                  else TAG_UNKNOWN_DATA)
        return {"updated_at": updated, "age_s": round(age, 1) if age is not None else None,
                "workers": workers, "status": status}
    except Exception as e:
        return {"updated_at": None, "age_s": None, "workers": {},
                "status": TAG_UNKNOWN_DATA, "error": type(e).__name__}


def parse_codex_tokens(db_path: Path, now: float) -> dict:
    """Read Codex cumulative per-thread token counters without summing snapshots."""
    c = _ro_sqlite(db_path)
    if not c:
        return {"total_tokens": None, "turn_count": None, "by_model": {}, "recent_turns": [], "status": TAG_PROVIDER_UNAVAILABLE}
    try:
        # Check table
        has_table = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='logs'").fetchone()
        if not has_table:
            return {"total_tokens": None, "turn_count": None, "by_model": {}, "recent_turns": [], "status": TAG_UNKNOWN_DATA}

        rows = c.execute(
            "SELECT ts,ts_nanos,thread_id,process_uuid,feedback_log_body FROM logs "
            "WHERE feedback_log_body LIKE '%total_usage_tokens=%' ORDER BY ts,ts_nanos"
        ).fetchall()

        threads: dict[tuple[str, str], list[tuple[float, int, str]]] = {}
        by_model = {}
        recent = []
        windows = {name: {"total_tokens": None, "turn_count": 0} for name in ("1h", "24h", "7d", "30d")}
        seen = set()
        duplicates = 0
        timestamps = []
        ungrouped = False
        models = set()
        for window, seconds in (("1h", 3600), ("24h", 86400), ("7d", 604800), ("30d", 2592000)):
            windows[window]["total_tokens"] = 0
        for r in rows:
            ts = r["ts"]
            body = r["feedback_log_body"] or ""
            try:
                ts = float(ts)
            except (TypeError, ValueError):
                continue
            m_tok = re.search(r"total_usage_tokens=(\d+)", body)
            m_mod = re.search(r"model=([^\s,}]+)", body)
            if m_tok:
                event_key = (ts, r["ts_nanos"], r["thread_id"], r["process_uuid"])
                if event_key in seen:
                    duplicates += 1
                    continue
                seen.add(event_key)
                tok = int(m_tok.group(1))
                mod = m_mod.group(1) if m_mod else "unknown"
                timestamps.append(ts)
                models.add(mod)
                for window, seconds in (("1h", 3600), ("24h", 86400), ("7d", 604800), ("30d", 2592000)):
                    if now - seconds <= ts <= now:
                        windows[window]["turn_count"] += 1
                thread_id, process_uuid = r["thread_id"], r["process_uuid"]
                if not thread_id or not process_uuid:
                    ungrouped = True
                    continue
                threads.setdefault((str(thread_id), str(process_uuid)), []).append((ts, tok, mod))
                if len(recent) < 25:
                    recent.append({
                        "ts": ts,
                        "age_s": round(now - ts, 1) if ts else None,
                        "model": mod,
                        "cumulative_total_tokens": tok,
                        "token_status": SRC_MEASURED,
                    })

        cumulative_total = 0
        total_complete = bool(threads) and not ungrouped
        for events in threads.values():
            previous = None
            for ts, value, model in events:
                if previous is not None and value < previous:
                    total_complete = False
                previous = value
            if events:
                final_value = events[-1][1]
                cumulative_total += final_value
        by_model = {model: None for model in models}

        for window, seconds in (("1h", 3600), ("24h", 86400), ("7d", 604800), ("30d", 2592000)):
            cutoff = now - seconds
            token_total = 0
            token_complete = bool(threads) and not ungrouped
            for events in threads.values():
                in_window = [(ts, value) for ts, value, _model in events if cutoff <= ts <= now]
                if not in_window:
                    continue
                prior = [(ts, value) for ts, value, _model in events if ts < cutoff]
                if not prior:
                    token_complete = False
                    continue
                previous = prior[-1][1]
                for _ts, value in in_window:
                    if value < previous:
                        token_complete = False
                        break
                    token_total += value - previous
                    previous = value
            windows[window]["total_tokens"] = token_total if token_complete else None
        return {
            "total_tokens": cumulative_total if total_complete else None,
            "turn_count": len(seen),
            "token_counter_semantics": "CUMULATIVE_PER_THREAD; window deltas require a pre-window baseline",
            "deduplicated_rows": duplicates,
            "first_event_at": min(timestamps) if timestamps else None,
            "last_event_at": max(timestamps) if timestamps else None,
            "windows": windows,
            "input_output_cache_breakdown": SRC_UNKNOWN,
            "by_model": by_model,
            "recent_turns": recent,
            "status": SRC_MEASURED if rows else TAG_UNKNOWN_DATA,
        }
    except Exception as e:
        return {"total_tokens": None, "turn_count": None, "by_model": {}, "recent_turns": [], "status": TAG_UNKNOWN_DATA, "error": str(e)[:80]}
    finally:
        c.close()


def parse_claude_stats(claude_dir: Path) -> dict:
    """Extract measured model tokens and activity from Claude stats-cache.json."""
    sc_path = claude_dir / "stats-cache.json"
    if not sc_path.exists():
        return {"model_usage": {}, "daily_tokens": [], "status": TAG_UNKNOWN_DATA}
    try:
        with open(sc_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        mu = data.get("modelUsage", {})
        cleaned_mu = {}
        for mod, usage in mu.items():
            cleaned_mu[mod] = {
                "input_tokens": usage.get("inputTokens", 0),
                "output_tokens": usage.get("outputTokens", 0),
                "cache_read_tokens": usage.get("cacheReadInputTokens", 0),
                "cache_write_tokens": usage.get("cacheCreationInputTokens", 0),
                "total_tokens": (
                    usage.get("inputTokens", 0) +
                    usage.get("outputTokens", 0) +
                    usage.get("cacheReadInputTokens", 0) +
                    usage.get("cacheCreationInputTokens", 0)
                ),
                "web_searches": usage.get("webSearchRequests", 0),
                "cost_usd": usage.get("costUSD", 0.0),
                "token_status": SRC_MEASURED,
            }
        return {
            "last_computed_date": data.get("lastComputedDate"),
            "model_usage": cleaned_mu,
            "daily_tokens": data.get("dailyModelTokens", [])[-14:],
            "total_sessions": data.get("totalSessions", 0),
            "total_messages": data.get("totalMessages", 0),
            "status": SRC_MEASURED,
        }
    except Exception as e:
        return {"model_usage": {}, "daily_tokens": [], "status": TAG_UNKNOWN_DATA, "error": str(e)[:80]}


def parse_kimi_logs(kimi_dir: Path, now: float) -> dict:
    """Extract measured output tokens and latency from Kimi CLI session logs."""
    if not kimi_dir.exists():
        return {"output_tokens": 0, "call_count": 0, "status": TAG_PROVIDER_UNAVAILABLE}
    try:
        total_tokens = 0
        call_count = 0
        total_stream_ms = 0
        total_ttft_ms = 0
        recent = []
        windows = {name: {"output_tokens": 0, "response_count": 0} for name in ("1h", "24h", "7d", "30d")}
        timestamped_count = 0
        timestamps = []
        for log_path in glob.glob(str(kimi_dir / "sessions" / "*" / "*" / "logs" / "kimi-code.log")):
            p = Path(log_path)
            try:
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        if "llm response" in line:
                            m_tok = re.search(r"outputTokens=(\d+)", line)
                            m_ts = re.match(r"(?P<ts>\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)?)", line)
                            m_stream = re.search(r"streamDurationMs=(\d+)", line)
                            m_ttft = re.search(r"ttftMs=(\d+)", line)
                            if m_tok:
                                tok = int(m_tok.group(1))
                                total_tokens += tok
                                call_count += 1
                                event_epoch = None
                                if m_ts:
                                    try:
                                        event_epoch = datetime.fromisoformat(m_ts.group("ts").replace("Z", "+00:00")).timestamp()
                                        timestamped_count += 1
                                        timestamps.append(event_epoch)
                                        for window, seconds in (("1h", 3600), ("24h", 86400), ("7d", 604800), ("30d", 2592000)):
                                            if now - seconds <= event_epoch <= now:
                                                windows[window]["output_tokens"] += tok
                                                windows[window]["response_count"] += 1
                                    except ValueError:
                                        event_epoch = None
                                dur = int(m_stream.group(1)) if m_stream else 0
                                ttft = int(m_ttft.group(1)) if m_ttft else 0
                                total_stream_ms += dur
                                total_ttft_ms += ttft
                                if len(recent) < 15:
                                    recent.append({
                                        "ts": event_epoch,
                                        "output_tokens": tok,
                                        "input_tokens": None,  # UNKNOWN
                                        "stream_duration_ms": dur,
                                        "ttft_ms": ttft,
                                        "model": "k3",
                                        "token_status": SRC_MEASURED,
                                    })
            except OSError:
                continue
        avg_ttft = round(total_ttft_ms / max(1, call_count), 1) if call_count else 0
        avg_stream = round(total_stream_ms / max(1, call_count), 1) if call_count else 0
        return {
            "output_tokens": total_tokens,
            "input_tokens": None,  # legitimately not exposed in kimi log
            "call_count": call_count,
            "windows": windows if timestamped_count == call_count else None,
            "window_event_coverage": {"timestamped": timestamped_count, "records": call_count},
            "first_event_at": min(timestamps) if timestamps else None,
            "last_event_at": max(timestamps) if timestamps else None,
            "avg_ttft_ms": avg_ttft,
            "avg_stream_duration_ms": avg_stream,
            "recent_turns": recent,
            "status": SRC_MEASURED if call_count else TAG_UNKNOWN_DATA,
            "input_token_status": SRC_UNKNOWN,
            "output_token_status": SRC_MEASURED,
        }
    except Exception as e:
        return {"output_tokens": 0, "call_count": 0, "status": TAG_UNKNOWN_DATA, "error": str(e)[:80]}


def parse_grok_telemetry(grok_dir: Path, now: float) -> dict:
    """Extract Grok SuperGrok quota & credit status from unified.jsonl."""
    log_file = grok_dir / "logs" / "unified.jsonl"
    if not log_file.exists():
        return {"status": TAG_PROVIDER_UNAVAILABLE, "subscription_tier": None, "credit_usage_percent": None}
    try:
        sub_tier = None
        credit_pct = None
        current_period = None
        call_count = 0
        windows = {name: 0 for name in ("1h", "24h", "7d", "30d")}
        timestamped_count = 0
        timestamps = []
        size = log_file.stat().st_size
        truncated = size > 150_000
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            if size > 150_000:
                f.seek(size - 150_000)
            for line in f:
                if "billing: fetched credits config" in line:
                    try:
                        d = json.loads(line)
                        cfg = d.get("ctx", {}).get("config", {})
                        if "subscriptionTier" in cfg:
                            sub_tier = cfg["subscriptionTier"]
                        if "creditUsagePercent" in cfg:
                            credit_pct = cfg["creditUsagePercent"]
                        if "currentPeriod" in cfg:
                            current_period = cfg["currentPeriod"]
                    except ValueError:
                        pass
                elif "agent response complete" in line:
                    call_count += 1
                    try:
                        event = json.loads(line)
                        event_epoch = datetime.fromisoformat(str(event.get("ts", "")).replace("Z", "+00:00")).timestamp()
                        timestamped_count += 1
                        timestamps.append(event_epoch)
                        for window, seconds in (("1h", 3600), ("24h", 86400), ("7d", 604800), ("30d", 2592000)):
                            if now - seconds <= event_epoch <= now:
                                windows[window] += 1
                    except (TypeError, ValueError):
                        pass
        
        return {
            "model": "UNKNOWN",
            "subscription_tier": sub_tier or "SuperGrok",
            "credit_usage_percent": credit_pct,
            "current_period": current_period,
            "call_count": None if truncated else call_count,
            "windows": windows if timestamped_count == call_count and not truncated else None,
            "window_event_coverage": {"timestamped": timestamped_count, "records": call_count,
                                      "source_truncated": truncated},
            "first_event_at": min(timestamps) if timestamps and not truncated else None,
            "last_event_at": max(timestamps) if timestamps and not truncated else None,
            "token_tracking_status": TAG_UNKNOWN_DATA,
            "note": "Local logs record response completions; current model, freshness, and quota eligibility are UNKNOWN.",
            "status": TAG_UNKNOWN_DATA,
        }
    except Exception as e:
        return {"status": TAG_UNKNOWN_DATA, "error": str(e)[:80]}


def parse_fett_lite_jobs(fett_dir: Path, now: float) -> dict:
    """Extract active and recent jobs from NOVA FETT Lite state."""
    jobs_dir = fett_dir / "state" / "jobs"
    if not jobs_dir.exists():
        return {"active_job": None, "jobs": [], "total_count": 0, "status": TAG_UNKNOWN_DATA}
    try:
        job_dirs = sorted(glob.glob(str(jobs_dir / "*")), key=lambda d: os.path.basename(d), reverse=True)
        active_job = None
        job_summaries = []
        rate_limit_failures = 0
        total_duration = 0.0
        subprocess_completions = {}

        for jd in job_dirs:
            p = Path(jd)
            jid = p.name
            jf = p / "job.json"
            rf = p / "result.json"
            ef = p / "events.jsonl"

            task_desc = ""
            agents_declared = []
            created_at = None
            created_epoch = p.stat().st_mtime
            if jf.exists():
                try:
                    with open(jf, "r", encoding="utf-8") as f:
                        j_data = json.load(f)
                        task_desc = _clean_str(j_data.get("task", ""), 120)
                        created_at = j_data.get("created")
                        if created_at:
                            try:
                                created_epoch = datetime.fromisoformat(str(created_at)).timestamp()
                            except ValueError:
                                pass
                except Exception:
                    pass

            events = []
            if ef.exists():
                try:
                    with open(ef, "r", encoding="utf-8", errors="replace") as f:
                        for l in f:
                            if l.strip():
                                events.append(json.loads(l))
                except Exception:
                    pass

            for ev in events:
                if ev.get("ev") == "submitted" and "agents" in ev:
                    agents_declared = ev["agents"]
                if ev.get("ev") == "step_end" and ev.get("agent"):
                    agent = str(ev["agent"])
                    subprocess_completions[agent] = subprocess_completions.get(agent, 0) + 1

            status = "UNKNOWN"
            steps = []
            dur_secs = 0.0
            is_rate_limited = False
            job_completions = []
            for ev in events:
                if ev.get("ev") != "step_end" or not ev.get("agent"):
                    continue
                try:
                    event_epoch = datetime.fromisoformat(str(ev.get("t"))).timestamp()
                except (TypeError, ValueError):
                    event_epoch = None
                job_completions.append({"agent": str(ev["agent"]), "ts": event_epoch,
                                        "status": "SUBPROCESS_COMPLETED"})

            if rf.exists():
                try:
                    with open(rf, "r", encoding="utf-8") as f:
                        r_data = json.load(f)
                        status = r_data.get("status", "DONE")
                        for s in r_data.get("steps", []):
                            ag = s.get("agent")
                            s_sec = s.get("secs") or 0.0
                            s_stat = s.get("status")
                            s_err = s.get("error")
                            mod_info = FETT_AGENT_MODELS.get(ag, {"model": "unknown", "provider": "unknown"})
                            dur_secs += s_sec
                            
                            # Check if step hit rate limit
                            if s_stat == "FAILED" or s_err:
                                err_str = str(s_err or "").lower()
                                if any(t in err_str for t in ("429", "rate_limit", "session limit", "quota")):
                                    is_rate_limited = True
                            
                            steps.append({
                                "agent": ag,
                                "model": mod_info["model"],
                                "provider": mod_info["provider"],
                                "status": s_stat,
                                "duration_s": s_sec,
                                "error": s_err,
                                "tokens": None,  # FETT steps don't record raw tokens directly
                                "token_status": SRC_UNKNOWN,
                            })
                except Exception:
                    status = "CORRUPT_RESULT"
            else:
                # In-progress, interrupted or stopped
                if any(ev.get("ev") == "reaped_interrupted" for ev in events):
                    status = "INTERRUPTED"
                elif any(ev.get("ev") == "stopped" for ev in events):
                    status = "STOPPED"
                elif any(ev.get("ev") == "run_start" for ev in events):
                    runner_pid = None
                    try:
                        runner_pid = json.loads(jf.read_text(encoding="utf-8")).get("runner_pid") if jf.exists() else None
                    except Exception:
                        pass
                    runner_alive = False
                    if isinstance(runner_pid, int) and runner_pid > 1:
                        try:
                            os.kill(runner_pid, 0)
                            runner_alive = True
                        except (ProcessLookupError, PermissionError):
                            runner_alive = False
                    if runner_alive:
                        status = "ACTIVE"
                        active_job = jid
                    else:
                        status = "STALE_PENDING"

            # Check stdout files for session limits if not already flagged
            if not is_rate_limited and status == "FAILED":
                for stdout_file in p.glob("*.stdout"):
                    try:
                        tail_txt = stdout_file.read_text(encoding="utf-8", errors="replace")[-2000:]
                        if "session limit" in tail_txt or "rate limit" in tail_txt or "429" in tail_txt:
                            is_rate_limited = True
                            break
                    except OSError:
                        pass

            if is_rate_limited:
                rate_limit_failures += 1
            total_duration += dur_secs

            job_summaries.append({
                "job_id": jid,
                "task": task_desc,
                "status": status,
                "agents": agents_declared,
                "steps": steps,
                "duration_s": round(dur_secs, 1),
                "is_rate_limited": is_rate_limited,
                "created_at": created_at,
                "created_epoch": created_epoch,
                "subprocess_completions": job_completions,
                "created_mtime": p.stat().st_mtime,
            })

        return {
            "active_job": active_job,
            "jobs": job_summaries,
            "total_count": len(job_dirs),
            "rate_limit_failures": rate_limit_failures,
            "total_duration_s": round(total_duration, 1),
            "subprocess_completions_by_agent": subprocess_completions,
            "status": SRC_MEASURED,
        }
    except Exception as e:
        return {"active_job": None, "jobs": [], "total_count": 0, "status": TAG_UNKNOWN_DATA, "error": str(e)[:80]}


def get_model_usage_snapshot(env: ModelUsageEnv | None = None, now: float | None = None) -> dict:
    """Build the complete Model Usage telemetry snapshot."""
    cur_now = now or time.time()
    e = env or ModelUsageEnv(now=cur_now)

    # 1. Parse Rate Limits & Quotas
    hermes_err_log = e.hermes_dir / "profiles" / e.hermes_profile / "logs" / "errors.log"
    rate_limits = parse_hermes_rate_limits(hermes_err_log, cur_now)
    fett_jobs_dir = e.fett_dir / "state" / "jobs"
    claude_session_limits = parse_claude_session_limits(fett_jobs_dir, cur_now)
    quota_cache_path = e.hermes_dir / "profiles" / e.hermes_profile / "quota_cache.json"
    quota_info = parse_quota_cache(quota_cache_path, cur_now)
    router_info = parse_router_status(e.router_status_path, cur_now)

    # 2. Parse Token Metrics per Provider
    codex1_stats = parse_codex_tokens(e.codex_dir_1 / "logs_2.sqlite", cur_now)
    codex2_stats = parse_codex_tokens(e.codex_dir_2 / "logs_2.sqlite", cur_now)
    claude_stats = parse_claude_stats(e.claude_dir)
    kimi_stats = parse_kimi_logs(e.kimi_dir, cur_now)
    grok_stats = parse_grok_telemetry(e.grok_dir, cur_now)
    fett_jobs = parse_fett_lite_jobs(e.fett_dir, cur_now)

    # 3. Read Fleet dot spend from dotz.db
    fleet_dot_usd = 0.0
    fleet_calls = 0
    fleet_cost_known = False
    dotz_db_path = e.dotz_state_dir / "fleet-runtime" / "dotz.db"
    c_dotz = _ro_sqlite(dotz_db_path)
    if c_dotz:
        try:
            r = c_dotz.execute("SELECT count(*), sum(cost_usd) FROM model_calls").fetchone()
            if r:
                fleet_calls = r[0] or 0
                fleet_dot_usd = r[1] or 0.0
                fleet_cost_known = True
        except sqlite3.Error:
            pass
        finally:
            c_dotz.close()

    # Determine Provider Statuses
    # Active Hermes 429
    active_hermes_rl = next((rl for rl in rate_limits if rl.get("is_active")), None)
    hermes_status = ST_RATE_LIMITED if active_hermes_rl else ST_UNKNOWN
    
    # Active Claude Session Limit
    active_claude_rl = next((cl for cl in claude_session_limits if cl.get("is_active")), None)
    claude_status = ST_OWNER_RESERVED

    def codex_status(worker_id: str) -> tuple[str, str, list[dict]]:
        worker = router_info.get("workers", {}).get(worker_id, {})
        fresh = (router_info.get("status") == SRC_MEASURED
                 and isinstance(worker.get("observation_age_s"), (int, float))
                 and worker["observation_age_s"] <= 600)
        windows = worker.get("windows", []) if fresh and worker.get("usage_source") == "PROVIDER_REPORTED" else []
        if worker.get("usage_state") in {ST_RATE_LIMITED, "COOLDOWN", "RED"} and fresh:
            state = ST_RATE_LIMITED
        elif (fresh and worker.get("health") == "HEALTHY"
              and worker.get("health_source") == "CLI_REPORTED"
              and worker.get("usage_state") in {"GREEN", "YELLOW"} and windows):
            state = ST_AVAILABLE
        elif worker.get("health") == "UNAVAILABLE":
            state = ST_AUTH_FAILED
        else:
            state = ST_UNKNOWN
        reason = (f"CLI auth {worker.get('health', 'UNKNOWN')}; provider quota "
                  f"{worker.get('usage_state', 'UNKNOWN')} ({worker.get('usage_source', 'UNKNOWN')}); "
                  f"observation age {worker.get('observation_age_s', 'UNKNOWN')}s"
                  if worker else "No current account-specific provider metadata")
        return state, reason, windows

    codex1_status, codex1_reason, codex1_windows = codex_status("codex_1")
    codex2_status, codex2_reason, codex2_windows = codex_status("codex_2")
    kimi_status = ST_UNKNOWN
    grok_status = ST_UNKNOWN

    providers = [
        {
            "id": "hermes_anthropic",
            "account_alias": f"Hermes (profile {e.hermes_profile})",
            "provider": "anthropic",
            "model": "claude-sonnet-4-6",
            "status": hermes_status,
            "status_reason": (f"Observed 429 backoff; workflow/service attribution UNKNOWN; retry in {active_hermes_rl['remaining_s']}s"
                              if active_hermes_rl else "Profile configured; current availability and workflow attribution UNKNOWN"),
            "quota_window": next(
                (f"{w['label']}: {w['used_percent']}% used (resets {w['reset_at']})"
                 for w in quota_info.get("providers", {}).get("anthropic", {}).get("windows", [])),
                "No live window"
            ),
            "measured_tokens": 0,
            "token_status": SRC_UNKNOWN,
            "rate_limit_active": bool(active_hermes_rl),
            "retry_after_s": active_hermes_rl["retry_after_s"] if active_hermes_rl else None,
            "reset_remaining_s": active_hermes_rl["remaining_s"] if active_hermes_rl else None,
        },
        {
            "id": "claude_code",
            "account_alias": "Claude Code (local CLI)",
            "provider": "anthropic",
            "model": "claude-sonnet-5-5",
            "other_models": ["claude-sonnet-4-6", "claude-opus-5-5", "claude-sonnet-5"],
            "status": claude_status,
            "status_reason": (f"OWNER_RESERVED; observed FETT session limit resets {active_claude_rl['reset_time_str']}"
                              if active_claude_rl else "OWNER_RESERVED; current quota UNKNOWN; stats do not split interactive from autonomous use"),
            "quota_window": active_claude_rl["error_detail"] if active_claude_rl else "Owner access preserved; no current quota observation",
            "measured_tokens": None,
            "token_status": SRC_UNKNOWN,
            "usage_scope": "CUMULATIVE_CLAUDE_STATS_UNATTRIBUTED",
            "rate_limit_active": bool(active_claude_rl),
            "retry_after_s": None,
            "reset_remaining_s": None,
        },
        {
            "id": "claude_autonomous",
            "account_alias": "Claude (autonomous use)",
            "provider": "anthropic",
            "model": "UNKNOWN",
            "status": ST_BLOCKED,
            "status_reason": "Explicit owner authorization required; FETT and NOVA router guards installed",
            "quota_window": "Not available for autonomous routing",
            "measured_tokens": None,
            "token_status": SRC_UNKNOWN,
            "rate_limit_active": bool(active_hermes_rl or active_claude_rl),
            "retry_after_s": active_hermes_rl["retry_after_s"] if active_hermes_rl else None,
            "reset_remaining_s": active_hermes_rl["remaining_s"] if active_hermes_rl else None,
        },
        {
            "id": "codex_1",
            "account_alias": "Codex1 (independent account)",
            "provider": "openai-codex",
            "model": "gpt-5.6-luna",
            "other_models": list(codex1_stats.get("by_model", {}).keys()),
            "status": codex1_status,
            "status_reason": codex1_reason,
            "quota_windows": codex1_windows,
            "quota_window": "Provider-reported account-specific windows" if codex1_windows else "UNKNOWN",
            "measured_tokens": codex1_stats.get("total_tokens", 0),
            "token_status": SRC_MEASURED if codex1_stats.get("total_tokens") is not None else SRC_UNKNOWN,
            "rate_limit_active": False,
            "retry_after_s": None,
            "reset_remaining_s": None,
        },
        {
            "id": "codex_2",
            "account_alias": "Codex2 (independent account)",
            "provider": "openai-codex",
            "model": "gpt-5.6-luna",
            "other_models": list(codex2_stats.get("by_model", {}).keys()),
            "status": codex2_status,
            "status_reason": codex2_reason,
            "quota_windows": codex2_windows,
            "quota_window": "Provider-reported account-specific windows" if codex2_windows else "UNKNOWN",
            "measured_tokens": codex2_stats.get("total_tokens", 0),
            "token_status": SRC_MEASURED if codex2_stats.get("total_tokens") is not None else SRC_UNKNOWN,
            "rate_limit_active": False,
            "retry_after_s": None,
            "reset_remaining_s": None,
        },
        {
            "id": "kimi_code",
            "account_alias": "Kimi Code (k3)",
            "provider": "kimi",
            "model": "k3",
            "status": kimi_status,
            "status_reason": f"{kimi_stats.get('call_count', 0)} local response records; current availability UNKNOWN",
            "quota_window": "UNKNOWN (no provider quota metadata)",
            "measured_tokens": kimi_stats.get("output_tokens", 0),
            "token_status": SRC_MEASURED,
            "rate_limit_active": False,
            "retry_after_s": None,
            "reset_remaining_s": None,
        },
        {
            "id": "grok_cli",
            "account_alias": "Grok (SuperGrok)",
            "provider": "xai",
            "model": grok_stats.get("model", "grok-4.7"),
            "status": grok_status,
            "status_reason": "Local credit record present; freshness and current availability UNKNOWN",
            "quota_window": "UNKNOWN (credit record has no verified observation time)",
            "measured_tokens": 0,
            "token_status": TAG_UNKNOWN_DATA,
            "rate_limit_active": False,
            "retry_after_s": None,
            "reset_remaining_s": None,
        },
        {
            "id": "nous",
            "account_alias": "Nous Research",
            "provider": "nous",
            "model": "Hermes-3-Llama-3.1",
            "status": ST_UNKNOWN,
            "status_reason": "No current provider metadata available",
            "quota_window": "UNKNOWN",
            "measured_tokens": None,
            "token_status": TAG_UNKNOWN_DATA,
            "rate_limit_active": False,
            "retry_after_s": None,
            "reset_remaining_s": None,
        },
        {
            "id": "gemini_assist",
            "account_alias": "Google Gemini (Code Assist)",
            "provider": "google",
            "model": "gemini-3.1-pro-preview",
            "status": ST_UNKNOWN,
            "status_reason": "No current provider metadata available",
            "quota_window": "UNKNOWN",
            "measured_tokens": None,
            "token_status": TAG_UNKNOWN_DATA,
            "rate_limit_active": False,
            "retry_after_s": None,
            "reset_remaining_s": None,
        },
        {
            "id": "antigravity_host",
            "account_alias": "Antigravity AGY (Host Keyring)",
            "provider": "google",
            "model": "gemini-3.8-flash",
            "status": ST_UNKNOWN,
            "status_reason": "AGY is host-direct only; local usage and quota logs unavailable",
            "quota_window": "UNKNOWN",
            "measured_tokens": None,
            "token_status": TAG_UNKNOWN_DATA,
            "rate_limit_active": False,
            "retry_after_s": None,
            "reset_remaining_s": None,
        },
        {
            "id": "openrouter_free",
            "account_alias": "OpenRouter Free",
            "provider": "openrouter",
            "model": "UNKNOWN",
            "status": ST_UNKNOWN,
            "status_reason": "No local request or quota telemetry; free-model eligibility UNKNOWN",
            "quota_window": "UNKNOWN",
            "measured_tokens": None,
            "token_status": SRC_UNKNOWN,
            "rate_limit_active": False,
            "retry_after_s": None,
            "reset_remaining_s": None,
        },
        {
            "id": "local_qwen",
            "account_alias": "Local Qwen",
            "provider": "local",
            "model": "Qwen (loopback service)",
            "status": ST_UNKNOWN,
            "status_reason": "Service presence does not provide request counts; no durable request log is wired",
            "quota_window": "Not applicable; usage UNKNOWN",
            "measured_tokens": None,
            "token_status": SRC_UNKNOWN,
            "rate_limit_active": False,
            "retry_after_s": None,
            "reset_remaining_s": None,
        },
    ]

    # Windowed counters are computed only from records with source timestamps.
    def calc_window(window: str, seconds_ago: int | None):
        cutoff = cur_now - seconds_ago if seconds_ago is not None else float("-inf")
        c1 = codex1_stats.get("windows", {}).get(window, {}) if window != "all" else {
            "total_tokens": codex1_stats.get("total_tokens"), "turn_count": codex1_stats.get("turn_count")}
        c2 = codex2_stats.get("windows", {}).get(window, {}) if window != "all" else {
            "total_tokens": codex2_stats.get("total_tokens"), "turn_count": codex2_stats.get("turn_count")}
        kimi_window = kimi_stats.get("windows")
        kimi = kimi_window.get(window) if window != "all" and isinstance(kimi_window, dict) else None
        if window == "all":
            kimi = {"output_tokens": kimi_stats.get("output_tokens"), "response_count": kimi_stats.get("call_count")}

        measured_parts = [x for x in (c1.get("total_tokens"), c2.get("total_tokens"),
                                       kimi.get("output_tokens") if isinstance(kimi, dict) else None)
                          if isinstance(x, (int, float))]
        measured = sum(measured_parts) if measured_parts else None
        provider_turns = {}
        provider_turns["codex_1_cli_turns"] = c1.get("turn_count")
        provider_turns["codex_2_cli_turns"] = c2.get("turn_count")
        provider_turns["kimi_response_records"] = kimi.get("response_count") if isinstance(kimi, dict) else None
        grok_window = grok_stats.get("windows")
        provider_turns["grok_response_records"] = (grok_stats.get("call_count") if window == "all"
                                                     else grok_window.get(window) if isinstance(grok_window, dict) else None)
        provider_turns["claude_provider_requests"] = None
        provider_turns["openrouter_requests"] = None
        provider_turns["local_qwen_requests"] = None
        provider_turns["agy_requests"] = None
        fett_jobs_in_window = [j for j in fett_jobs.get("jobs", [])
                               if isinstance(j.get("created_epoch"), (int, float)) and j["created_epoch"] >= cutoff]
        fett_completions = {}
        for job in fett_jobs_in_window:
            for event in job.get("subprocess_completions", []):
                if event.get("ts") is not None and event["ts"] >= cutoff:
                    agent = event["agent"]
                    fett_completions[agent] = fett_completions.get(agent, 0) + 1
        return {
            "window_seconds": seconds_ago,
            "measured_tokens": measured,
            "measured_tokens_scope": "Known source subtotal only; not a complete fleet total when unknown token sources remain",
            "estimated_tokens": 0,
            "unknown_tokens": ["Claude attribution and windows", "Codex total where cumulative-counter completeness is unknown",
                               "Codex input/output/cache breakdown",
                               "Kimi input/cache breakdown", "OpenRouter", "Local Qwen", "AGY"],
            "input_tokens": None,
            "output_tokens": None,
            "cache_read_tokens": None,
            "cache_write_tokens": None,
            "token_sources": {
                "codex_1_total_usage_tokens": c1.get("total_tokens"),
                "codex_2_total_usage_tokens": c2.get("total_tokens"),
                "kimi_output_tokens": kimi.get("output_tokens") if isinstance(kimi, dict) else None,
            },
            "rate_limit_events": len([rl for rl in rate_limits if cutoff <= rl["ts"] <= cur_now]),
            "fett_jobs": len(fett_jobs_in_window),
            "fett_subprocess_completions_by_agent": fett_completions,
            "fleet_dot_cost_usd": fleet_dot_usd if window == "all" and fleet_cost_known else None,
            "requests_by_provider": provider_turns,
            "local_log_coverage_start": {
                "codex_1": codex1_stats.get("first_event_at"),
                "codex_2": codex2_stats.get("first_event_at"),
                "kimi": kimi_stats.get("first_event_at"),
                "grok": grok_stats.get("first_event_at"),
            },
            "request_count_scope": "Timestamped records present in retained local logs; not provider HTTP request counts; history before each coverage start is UNKNOWN",
        }

    analytics = {
        "1h": calc_window("1h", 3600),
        "24h": calc_window("24h", 86400),
        "7d": calc_window("7d", 604800),
        "30d": calc_window("30d", 2592000),
        "all": calc_window("all", None),
    }

    # Generate Alerts
    alerts = []
    if active_hermes_rl:
        alerts.append({
            "severity": "critical",
            "kind": "PROVIDER_RATE_LIMITED",
            "provider": "anthropic",
            "message": f"Observed Anthropic HTTP 429; retry window remains {active_hermes_rl['remaining_s']}s. Initiating workflow UNKNOWN.",
            "ts": active_hermes_rl["ts"],
        })
    if active_claude_rl:
        alerts.append({
            "severity": "critical",
            "kind": "SESSION_LIMIT_EXCEEDED",
            "provider": "anthropic",
            "message": f"Claude Code session limit reached: resets {active_claude_rl['reset_time_str']}.",
            "ts": active_claude_rl["ts"],
        })
    # An expired retry interval does not prove that a provider recovered.
    for rl in rate_limits:
        if not rl.get("is_active") and (cur_now - rl["ts"]) < 3600:
            alerts.append({
                "severity": "unknown",
                "kind": "RETRY_WINDOW_ELAPSED",
                "provider": rl["provider"],
                "message": f"Provider retry window elapsed; recovery was not verified. Workflow UNKNOWN.",
                "ts": rl["ts"] + rl["retry_after_s"],
            })
            if len(alerts) >= 10:
                break

    # Security check: ensure no secrets leak in output
    raw_snapshot = {
        "as_of": cur_now,
        "as_of_iso": datetime.fromtimestamp(cur_now, timezone.utc).isoformat(),
        "providers": providers,
        "rate_limits": rate_limits[:15],
        "claude_session_limits": claude_session_limits[:10],
        "quota_cache": quota_info,
        "quota_cache_freshness": {"status": quota_info.get("status"), "fetched_at": quota_info.get("fetched_at"),
                                  "age_s": quota_info.get("as_of_age_s")},
        "nova_router_status": router_info,
        "claude_cumulative_usage": {
            "source": "~/.claude/stats-cache.json",
            "scope": "CUMULATIVE_ACCOUNT_TOTAL; interactive and autonomous activity are not attributable separately",
            "last_computed_date": claude_stats.get("last_computed_date"),
            "total_messages": claude_stats.get("total_messages"),
            "total_sessions": claude_stats.get("total_sessions"),
            "model_usage": {model: {key: value.get(key) for key in
                                     ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")}
                            for model, value in claude_stats.get("model_usage", {}).items()},
            "status": claude_stats.get("status", TAG_UNKNOWN_DATA),
        },
        "fett_lite": fett_jobs,
        "analytics": analytics,
        "alerts": alerts,
        "spending": {
            "fleet_model_usd": fleet_dot_usd if fleet_cost_known else None,
            "fleet_model_calls": fleet_calls if fleet_cost_known else None,
            "nous_spend_usd": 0.0,
            "subscription_notes": "Codex Plus, Claude, SuperGrok are monthly/weekly subscription tiers. Measured API cost reflects direct token billing.",
        },
    }

    return _mask_secrets(raw_snapshot)
