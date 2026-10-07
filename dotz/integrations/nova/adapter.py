"""Optional NOVA Frontier engineering delegation adapter.

NOVA is not required for core Dotz operation.
If configured, Dot requests engineering delegation -> NOVA Frontier plan -> Drex bounded grant -> verified result.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional
import httpx

from dotz.runtime import Runtime


class NovaAdapter:
    """Generic interface for optional NOVA Frontier delegation."""

    def __init__(self, endpoint_url: Optional[str] = None):
        self.endpoint_url = endpoint_url or os.environ.get("NOVA_API_URL", "http://127.0.0.1:4000/v1")
        self.http = httpx.Client(timeout=60.0)

    def is_available(self) -> bool:
        try:
            r = self.http.get(f"{self.endpoint_url}/health", timeout=2.0)
            return r.status_code == 200
        except Exception:
            return False

    def delegate_engineering_task(
        self,
        task: str,
        files: list[str],
        verifier_command: str,
        dot_name: str,
    ) -> Dict[str, Any]:
        """Delegate engineering task to NOVA Frontier."""
        if not self.is_available():
            # Return graceful local failure / mock fallback
            return {
                "success": False,
                "error": "NOVA Frontier endpoint unavailable; no proprietary logic called.",
                "verified": False,
                "artifacts": [],
            }

        body = {
            "task": task,
            "files": files,
            "verifier_command": verifier_command,
            "caller": f"dot:{dot_name}",
        }
        try:
            r = self.http.post(f"{self.endpoint_url}/nova/engineering_task", json=body)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            return {
                "success": False,
                "error": f"NOVA delegation failed: {str(e)}",
                "verified": False,
                "artifacts": [],
            }
