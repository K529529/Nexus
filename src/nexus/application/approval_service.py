"""Day 3 approval persistence and Run/Session correlation rules."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from nexus.domain.approvals import ApprovalRequest
from nexus.domain.ports.approval_unit_of_work import ApprovalUnitOfWork, ApprovalUnitOfWorkFactory
from nexus.domain.tooling import ApprovalDecision, RiskLevel, ToolInvocation
from nexus.errors import PermissionDeniedError, ToolExecutionError


class ApprovalService:
    def __init__(self, unit_of_work_factory: ApprovalUnitOfWorkFactory) -> None:
        self._unit_of_work_factory = unit_of_work_factory

    async def create_pending(
        self,
        invocation: ToolInvocation,
        *,
        risk_level: RiskLevel,
        summary: str,
    ) -> ApprovalRequest:
        approval = ApprovalRequest(
            approval_id=str(uuid4()),
            run_id=invocation.run_id,
            session_id=invocation.session_id,
            operation=invocation.tool_name,
            risk_level=risk_level,
            resource_or_command_summary=summary,
            decision=ApprovalDecision.PENDING,
            actor="runtime",
            reason=None,
            created_at=datetime.now(UTC),
            decided_at=None,
        )
        try:
            async with self._unit_of_work_factory() as unit_of_work:
                await _validate_correlation(unit_of_work, approval)
                await unit_of_work.approvals.add(approval)
            return approval
        except (PermissionDeniedError, ToolExecutionError):
            raise
        except Exception as exc:
            raise ToolExecutionError(
                "Nexus could not persist the pending approval.",
                code="APPROVAL_PERSISTENCE_ERROR",
                retryable=True,
            ) from exc

    async def persist_decision(self, approval: ApprovalRequest) -> ApprovalRequest:
        if approval.decision is ApprovalDecision.PENDING:
            raise ToolExecutionError(
                "A pending approval cannot be persisted as a terminal decision.",
                code="INVALID_APPROVAL_TRANSITION",
            )
        try:
            async with self._unit_of_work_factory() as unit_of_work:
                await _validate_correlation(unit_of_work, approval)
                current = await unit_of_work.approvals.get(approval.approval_id)
                if current is None:
                    raise ToolExecutionError(
                        "The approval does not exist.",
                        code="APPROVAL_NOT_FOUND",
                    )
                if current.decision is not ApprovalDecision.PENDING:
                    raise ToolExecutionError(
                        "The approval already has a terminal decision.",
                        code="INVALID_APPROVAL_TRANSITION",
                    )
                await unit_of_work.approvals.update(approval)
            return approval
        except (PermissionDeniedError, ToolExecutionError):
            raise
        except Exception as exc:
            raise ToolExecutionError(
                "Nexus could not persist the approval decision.",
                code="APPROVAL_PERSISTENCE_ERROR",
                retryable=True,
            ) from exc

    async def record_denied(
        self,
        invocation: ToolInvocation,
        *,
        risk_level: RiskLevel,
        summary: str,
        reason: str,
    ) -> ApprovalRequest:
        now = datetime.now(UTC)
        approval = ApprovalRequest(
            approval_id=str(uuid4()),
            run_id=invocation.run_id,
            session_id=invocation.session_id,
            operation=invocation.tool_name,
            risk_level=risk_level,
            resource_or_command_summary=summary,
            decision=ApprovalDecision.DENIED,
            actor="security_policy",
            reason=reason,
            created_at=now,
            decided_at=now,
        )
        try:
            async with self._unit_of_work_factory() as unit_of_work:
                await _validate_correlation(unit_of_work, approval)
                await unit_of_work.approvals.add(approval)
            return approval
        except (PermissionDeniedError, ToolExecutionError):
            raise
        except Exception as exc:
            raise ToolExecutionError(
                "Nexus could not persist the denied security decision.",
                code="APPROVAL_PERSISTENCE_ERROR",
                retryable=True,
            ) from exc


async def _validate_correlation(
    unit_of_work: ApprovalUnitOfWork,
    approval: ApprovalRequest,
) -> None:
    session = await unit_of_work.sessions.get(approval.session_id)
    run = await unit_of_work.runs.get(approval.run_id)
    if session is None or run is None or run.session_id != approval.session_id:
        raise PermissionDeniedError(
            "The approval Run/Session correlation is invalid.",
            code="APPROVAL_CORRELATION_DENIED",
        )
