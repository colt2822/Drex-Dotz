"""Pluggable model router and cost/token tracking.

Policy:
deterministic/mechanical -> code
cheap routine reasoning -> cheap model (default)
hard ambiguity / escalation -> frontier
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
import httpx

from dotz.store import Store


@dataclass
class ModelUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cost_usd: Optional[float] = None


@dataclass
class CompletionResult:
    text: str
    model: str
    provider: str
    usage: ModelUsage
    ok: bool = True
    error: Optional[str] = None


class ModelRouter:
    """Routes model calls based on tier intent ('cheap', 'frontier', etc.)."""

    def __init__(self, store: Store):
        self.store = store
        self.http = httpx.Client(timeout=30.0)

    def complete(
        self,
        prompt: str,
        tier: str = "cheap",
        dot: str = "unknown",
        task_id: str = "unknown",
        system: str = "You are a bounded autonomous worker.",
    ) -> CompletionResult:
        # Check if an external OpenAI-compatible API or test stub is provided
        api_base = os.environ.get("DOTZ_MODEL_API_BASE")
        api_key = os.environ.get("DOTZ_MODEL_API_KEY")

        resolved_model = "cheap-worker-v1" if tier == "cheap" else "frontier-worker-v1"
        provider_name = "mock"

        if api_base:
            provider_name = "openai-compatible"
            headers = {"Authorization": f"Bearer {api_key or 'none'}"}
            body = {
                "model": resolved_model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
            }
            try:
                resp = self.http.post(f"{api_base}/v1/chat/completions", headers=headers, json=body)
                resp.raise_for_status()
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
                u = data.get("usage", {})
                usage = ModelUsage(
                    input_tokens=u.get("prompt_tokens", 0),
                    output_tokens=u.get("completion_tokens", 0),
                    cache_read=0,
                )
                res = CompletionResult(
                    text=content,
                    model=resolved_model,
                    provider=provider_name,
                    usage=usage,
                    ok=True,
                )
            except Exception as e:
                res = CompletionResult(
                    text="",
                    model=resolved_model,
                    provider=provider_name,
                    usage=ModelUsage(),
                    ok=False,
                    error=str(e),
                )
        else:
            # Deterministic local mock response when no live provider configured
            usage = ModelUsage(
                input_tokens=len(prompt) // 4,
                output_tokens=30,
                cost_usd=0.0001 if tier == "cheap" else 0.001,
            )
            res = CompletionResult(
                text=f"[Completed bounded task response via {resolved_model}]",
                model=resolved_model,
                provider="local-mock",
                usage=usage,
                ok=True,
            )

        # Record call in persistent store
        self.store.insert(
            "model_calls",
            {
                "ts": time.time(),
                "dot": dot,
                "task_id": task_id,
                "tier": tier,
                "provider": res.provider,
                "model": res.model,
                "input_tokens": res.usage.input_tokens,
                "output_tokens": res.usage.output_tokens,
                "cache_read": res.usage.cache_read,
                "cost_usd": res.usage.cost_usd,
                "ok": 1 if res.ok else 0,
            },
        )
        return res
