"""Interactive and auto approval decisions without terminal input coupling."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from nexus.domain.approvals import ApprovalRequest
from nexus.domain.ports.tooling import InteractiveDecisionCallback
from nexus.domain.tooling import ApprovalDecision, RiskLevel
from nexus.errors import PermissionDeniedError, ToolExecutionError


class InteractiveApprovalPolicy:
    def __init__(self, decision_callback: InteractiveDecisionCallback) -> None:
        self._decision_callback = decision_callback

    async def request(self, request: ApprovalRequest) -> ApprovalRequest:
        _require_pending(request)
        if request.risk_level is RiskLevel.DANGEROUS:
            return _decide(
                request,
                ApprovalDecision.DENIED,
                actor="security_policy",
                reason="Dangerous operations are hard-denied.",
            )
        decision, reason = await self._decision_callback(request)
        if decision not in {ApprovalDecision.APPROVED, ApprovalDecision.DENIED}:
            raise ToolExecutionError(
                "Interactive approval returned an invalid decision.",
                code="INVALID_APPROVAL_TRANSITION",
            )
        return _decide(request, decision, actor="user", reason=reason)


class AutoApprovalPolicy:
    async def request(self, request: ApprovalRequest) -> ApprovalRequest:
        _require_pending(request)
        actor = (
            "security_policy"
            if request.risk_level is RiskLevel.DANGEROUS
            else "auto_policy"
        )
        return _decide(
            request,
            ApprovalDecision.DENIED,
            actor=actor,
            reason="Auto policy cannot authorize this operation.",
        )


def _require_pending(request: ApprovalRequest) -> None:
    if request.decision is not ApprovalDecision.PENDING:
        raise PermissionDeniedError(
            "Only a pending approval can be decided.",
            code="INVALID_APPROVAL_TRANSITION",
        )


def _decide(
    request: ApprovalRequest,
    decision: ApprovalDecision,
    *,
    actor: str,
    reason: str | None,
) -> ApprovalRequest:
    return replace(
        request,
        decision=decision,
        actor=actor,
        reason=reason,
        decided_at=datetime.now(UTC),
    )
