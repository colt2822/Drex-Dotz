"""Drex capability enforcement and grant boundary.

Every consequential action flows through this gate:
Dot -> capability request -> Drex -> ALLOW / BLOCK / bounded grant -> execution -> receipt
"""
from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from drex_agent_firewall.policy.packs import get_policy_pack, get_safe_local_coding_pack
from drex_agent_firewall.providers.base import BaseDecisionProvider
from drex_agent_firewall.schemas.config import FirewallConfig
from drex_agent_firewall.schemas.decision import (
    ActionClass,
    ActionRisk,
    DrexEvaluationResult,
    ExternalEffect,
    FinalDecision,
    FirewallDecision,
    Reversibility,
    ScopeMatch,
)
from drex_agent_firewall.sdk.client import DrexFirewall
from drex_agent_firewall.security.redactor import SecretRedactor

from dotz.models import DotConfig
from dotz.store import Store


class LocalBoundaryProvider(BaseDecisionProvider):
    """Deterministic local permission provider for bounded operations.

    Labels actions clearly as LOCAL_BOUNDARY, never pretending to be probabilistic Drex.
    """

    @property
    def provider_name(self) -> str:
        return "LOCAL_BOUNDARY"

    def evaluate(self, envelope) -> DrexEvaluationResult:
        if envelope.tool == "shell":
            kind = ActionClass.EXECUTE
        elif envelope.tool == "http":
            kind = ActionClass.NETWORK
        elif envelope.filesystem_write:
            kind = ActionClass.WRITE
        else:
            kind = ActionClass.READ

        return DrexEvaluationResult(
            provider=self.provider_name,
            resolved_model="deterministic-boundary-v1",
            risk=ActionRisk.LOW,
            action_class=kind,
            scope_match=ScopeMatch.IN_SCOPE,
            reversibility=Reversibility.PARTIALLY_REVERSIBLE,
            external_effect=ExternalEffect.LOCAL_ONLY,
            confidence=1.0,
            evaluation_notes=["Permission derives from executor confinement; no probabilistic safety grant."],
        )

    async def evaluate_async(self, envelope) -> DrexEvaluationResult:
        return self.evaluate(envelope)


@dataclass
class CapabilityGrant:
    grant_id: str
    dot: str
    task_id: str
    session_id: str
    workspace: str
    capability: str
    resources: List[str]
    issued_at: float
    expires_at: float
    max_uses: int
    uses: int = 0
    approved_by: Optional[str] = None
    revoked: bool = False

    def is_valid(self, dot: str, task_id: str, workspace: str, resource: Optional[str] = None) -> Tuple[bool, str]:
        if self.revoked:
            return False, "GRANT_REVOKED"
        if time.time() > self.expires_at:
            return False, "GRANT_EXPIRED"
        if self.uses >= self.max_uses:
            return False, "GRANT_EXHAUSTED"
        if self.dot != dot:
            return False, "GRANT_DOT_MISMATCH"
        if self.task_id != task_id:
            return False, "GRANT_TASK_MISMATCH"
        if Path(self.workspace).resolve() != Path(workspace).resolve():
            return False, "GRANT_WORKSPACE_MISMATCH"
        if resource:
            # If explicit resources are bound, resource must match one of them
            res_path = Path(resource).resolve()
            matched = False
            for bound in self.resources:
                bound_path = (Path(self.workspace) / bound).resolve() if not Path(bound).is_absolute() else Path(bound).resolve()
                if res_path == bound_path or (bound.endswith("/*") and str(res_path).startswith(str(bound_path.parent))):
                    matched = True
                    break
            if not matched:
                return False, "GRANT_RESOURCE_UNSCOPED"
        return True, "OK"


class DrexCapabilityGate:
    """The capability firewall protecting host access and issuing bounded grants."""

    def __init__(self, dot_config: DotConfig, workspace: Path, store: Store, state_dir: Path):
        self.config = dot_config
        self.workspace = workspace.resolve()
        self.workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.store = store
        self.state_dir = state_dir
        self.redactor = SecretRedactor()

        # Build underlying Drex Firewall with specified policy pack
        pack_name = dot_config.drex.get("pack", "safe-local-coding")
        try:
            self.fw_cfg = get_policy_pack(pack_name)
        except Exception:
            self.fw_cfg = get_safe_local_coding_pack()

        self.fw_cfg.filesystem.allowed_roots = [str(self.workspace)]
        # Confine database path to state_dir
        self.fw_cfg.database_path = str(self.state_dir / f"firewall_{dot_config.name}.db")
        if dot_config.capabilities.is_explicitly_denied("shell.execute"):
            self.fw_cfg.shell.max_runtime_seconds = 0.0

        self.provider = LocalBoundaryProvider()
        self.firewall = DrexFirewall(self.fw_cfg, provider=self.provider)

    def issue_grant(
        self,
        task_id: str,
        session_id: str,
        capability: str,
        resources: List[str],
        ttl_seconds: float = 300.0,
        max_uses: int = 10,
        approved_by: Optional[str] = None,
    ) -> CapabilityGrant:
        """Issue a bounded grant scoped to this dot, task, and workspace."""
        if not self.config.capabilities.is_allowed(capability):
            raise PermissionError(f"Dot {self.config.name} policy does not permit capability {capability}")

        grant_id = f"grant_{uuid.uuid4().hex[:12]}"
        now = time.time()
        grant = CapabilityGrant(
            grant_id=grant_id,
            dot=self.config.name,
            task_id=task_id,
            session_id=session_id,
            workspace=str(self.workspace),
            capability=capability,
            resources=resources,
            issued_at=now,
            expires_at=now + ttl_seconds,
            max_uses=max_uses,
            approved_by=approved_by,
        )

        import json
        self.store.insert(
            "grants",
            {
                "grant_id": grant.grant_id,
                "dot": grant.dot,
                "task_id": grant.task_id,
                "session_id": grant.session_id,
                "workspace": grant.workspace,
                "capability": grant.capability,
                "resources": json.dumps(grant.resources),
                "issued_at": grant.issued_at,
                "expires_at": grant.expires_at,
                "max_uses": grant.max_uses,
                "uses": grant.uses,
                "approved_by": grant.approved_by,
                "revoked": 0,
            },
        )
        return grant

    def get_grant(self, grant_id: str) -> Optional[CapabilityGrant]:
        import json
        row = self.store.one("SELECT * FROM grants WHERE grant_id = ?", (grant_id,))
        if not row:
            return None
        return CapabilityGrant(
            grant_id=row["grant_id"],
            dot=row["dot"],
            task_id=row["task_id"],
            session_id=row["session_id"],
            workspace=row["workspace"],
            capability=row["capability"],
            resources=json.loads(row["resources"]),
            issued_at=row["issued_at"],
            expires_at=row["expires_at"],
            max_uses=row["max_uses"],
            uses=row["uses"],
            approved_by=row["approved_by"],
            revoked=bool(row["revoked"]),
        )

    def consume_grant_use(self, grant_id: str) -> None:
        self.store.x("UPDATE grants SET uses = uses + 1 WHERE grant_id = ?", (grant_id,))

    def check_capability(self, capability: str) -> Tuple[bool, str]:
        """Check if capability is allowed by Dot definition without a grant."""
        if self.config.capabilities.is_explicitly_denied(capability):
            return False, f"Capability {capability} is explicitly denied by Dot policy"
        if not self.config.capabilities.is_allowed(capability):
            return False, f"Capability {capability} is not allowed by Dot policy"
        return True, "OK"

    def record_receipt(
        self,
        task_id: str,
        session_id: str,
        capability: str,
        action: str,
        target: str,
        decision: str,
        reason: str,
        rule: Optional[str] = None,
        grant_id: Optional[str] = None,
        model: Optional[str] = None,
        cost_usd: Optional[float] = None,
        input_tokens: Optional[int] = None,
        output_tokens: Optional[int] = None,
        result: Optional[str] = None,
        verification: Optional[str] = None,
        upstream_executed: bool = False,
        drex_action_id: Optional[str] = None,
    ) -> str:
        receipt_id = f"rcpt_{uuid.uuid4().hex[:12]}"
        now = time.time()
        redacted_target = self.redactor.redact_text(target) if target else ""
        redacted_result = self.redactor.redact_text(result) if result else ""

        sha = hashlib.sha256((redacted_result or "").encode()).hexdigest() if redacted_result else None

        self.store.insert(
            "receipts",
            {
                "receipt_id": receipt_id,
                "ts": now,
                "who": f"dot:{self.config.name}",
                "dot": self.config.name,
                "task_id": task_id,
                "session_id": session_id,
                "why": f"Action {action} under capability {capability}",
                "model": model,
                "capability": capability,
                "grant_id": grant_id,
                "target": redacted_target,
                "action": action,
                "decision": decision,
                "reason": reason,
                "rule": rule,
                "drex_action_id": drex_action_id,
                "cost_usd": cost_usd,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "result": (redacted_result[:2000] if redacted_result else None),
                "verification": verification,
                "upstream_executed": 1 if upstream_executed else 0,
                "output_sha256": sha,
            },
        )
        return receipt_id
