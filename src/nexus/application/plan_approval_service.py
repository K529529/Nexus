"""Day 4 Plan approval persistence and evidence validation."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid5

from nexus.domain.approvals import ApprovalRequest
from nexus.domain.planning import (
    ApprovedPlanEvidence,
    Plan,
    PlanApprovalResumeInput,
    PlanAuthorizationSource,
    PlanStatus,
    approval_summary,
    derive_authorization_scope,
)
from nexus.domain.ports.approval_unit_of_work import (
    ApprovalUnitOfWork,
    ApprovalUnitOfWorkFactory,
)
from nexus.domain.tooling import ApprovalDecision, RiskLevel
from nexus.errors import PermissionDeniedError, ToolExecutionError


class PlanApprovalService:
    def __init__(self, unit_of_work_factory: ApprovalUnitOfWorkFactory) -> None:
        self._unit_of_work_factory = unit_of_work_factory

    async def create_pending(self, plan: Plan) -> ApprovalRequest:
        self._validate_created_plan(plan)
        approval = self._request(plan, auto=False)
        return await self._persist_idempotent(approval)

    async def create_auto_approved(self, plan: Plan) -> ApprovalRequest:
        self._validate_created_plan(plan)
        approval = self._request(plan, auto=True)
        return await self._persist_idempotent(approval)

    async def persist_user_decision(
        self,
        approval_id: str,
        decision: PlanApprovalResumeInput,
    ) -> ApprovalRequest:
        try:
            async with self._unit_of_work_factory() as unit_of_work:
                current = await unit_of_work.approvals.get(approval_id)
                if current is None:
                    raise ToolExecutionError(
                        "The Plan approval does not exist.",
                        code="APPROVAL_NOT_FOUND",
                    )
                await _validate_correlation(unit_of_work, current)
                if current.operation != "approve_plan":
                    raise PermissionDeniedError(
                        "The approval does not authorize a Plan.",
                        code="PLAN_APPROVAL_REQUIRED",
                    )
                if current.decision is not ApprovalDecision.PENDING:
                    if current.decision is decision.decision:
                        return current
                    raise ToolExecutionError(
                        "The Plan approval already has a terminal decision.",
                        code="INVALID_APPROVAL_TRANSITION",
                    )
                updated = replace(
                    current,
                    decision=decision.decision,
                    actor="user",
                    reason=decision.reason,
                    decided_at=datetime.now(UTC),
                )
                await unit_of_work.approvals.update(updated)
                return updated
        except (PermissionDeniedError, ToolExecutionError):
            raise
        except Exception as exc:
            raise ToolExecutionError(
                "Nexus could not persist the Plan approval decision.",
                code="APPROVAL_PERSISTENCE_ERROR",
                retryable=True,
            ) from exc

    async def require_approved(
        self,
        evidence: ApprovedPlanEvidence,
    ) -> ApprovalRequest:
        try:
            async with self._unit_of_work_factory() as unit_of_work:
                approval = await unit_of_work.approvals.get(evidence.approval_id)
                if approval is None:
                    raise PermissionDeniedError(
                        "Approved Plan evidence was not found.",
                        code="PLAN_SCOPE_DENIED",
                    )
                try:
                    await _validate_correlation(unit_of_work, approval)
                except PermissionDeniedError as exc:
                    raise PermissionDeniedError(
                        "Approved Plan correlation is invalid.",
                        code="PLAN_SCOPE_DENIED",
                    ) from exc
                expected = approval_summary(
                    evidence.plan_id,
                    evidence.plan_version,
                    evidence.scope_digest,
                )
                if (
                    approval.run_id != evidence.run_id
                    or approval.session_id != evidence.session_id
                    or approval.operation != "approve_plan"
                    or approval.risk_level is not RiskLevel.WRITE
                    or approval.resource_or_command_summary != expected
                    or approval.decision is not ApprovalDecision.APPROVED
                    or approval.actor not in {"user", "auto_policy"}
                ):
                    raise PermissionDeniedError(
                        "Approved Plan evidence does not match persisted approval.",
                        code="PLAN_SCOPE_DENIED",
                    )
                return approval
        except (PermissionDeniedError, ToolExecutionError):
            raise
        except Exception as exc:
            raise ToolExecutionError(
                "Nexus could not validate approved Plan evidence.",
                code="APPROVAL_PERSISTENCE_ERROR",
                retryable=True,
            ) from exc

    @staticmethod
    def evidence(plan: Plan, approval: ApprovalRequest) -> ApprovedPlanEvidence:
        if approval.decision is not ApprovalDecision.APPROVED or approval.decided_at is None:
            raise PermissionDeniedError(
                "The Plan has no approved evidence.",
                code="PLAN_APPROVAL_REQUIRED",
            )
        return ApprovedPlanEvidence(
            plan.plan_id,
            plan.version,
            plan.run_id,
            plan.session_id,
            PlanAuthorizationSource.AUTO_MODE
            if approval.actor == "auto_policy"
            else PlanAuthorizationSource.INTERACTIVE,
            approval.approval_id,
            plan.scope_digest,
            plan.authorization_scope,
            approval.decided_at,
        )

    @staticmethod
    def activate(plan: Plan, approval: ApprovalRequest) -> Plan:
        if approval.decision is ApprovalDecision.APPROVED and approval.decided_at is not None:
            return replace(
                plan,
                status=PlanStatus.ACTIVE,
                approval_status=ApprovalDecision.APPROVED,
                approval_id=approval.approval_id,
                approved_at=approval.decided_at,
            )
        return replace(
            plan,
            status=PlanStatus.FAILED,
            approval_status=ApprovalDecision.DENIED,
            approval_id=approval.approval_id,
            approved_at=approval.decided_at,
        )

    @staticmethod
    def _validate_created_plan(plan: Plan) -> None:
        if plan.status is not PlanStatus.CREATED:
            raise ToolExecutionError(
                "Only a CREATED Plan may enter approval.",
                code="INVALID_PLAN_OUTPUT",
            )
        if plan.authorization_scope != derive_authorization_scope(plan.steps):
            raise ToolExecutionError(
                "Plan authorization exceeds visible Plan steps.",
                code="INVALID_PLAN_OUTPUT",
            )

    @staticmethod
    def _request(plan: Plan, *, auto: bool) -> ApprovalRequest:
        now = datetime.now(UTC)
        return ApprovalRequest(
            approval_id=str(uuid5(UUID(plan.plan_id), f"approval:v{plan.version}")),
            run_id=plan.run_id,
            session_id=plan.session_id,
            operation="approve_plan",
            risk_level=RiskLevel.WRITE,
            resource_or_command_summary=approval_summary(
                plan.plan_id,
                plan.version,
                plan.scope_digest,
            ),
            decision=ApprovalDecision.APPROVED if auto else ApprovalDecision.PENDING,
            actor="auto_policy" if auto else "runtime",
            reason="Approved by scoped Plan auto policy." if auto else None,
            created_at=now,
            decided_at=now if auto else None,
        )

    async def _persist_idempotent(self, approval: ApprovalRequest) -> ApprovalRequest:
        try:
            async with self._unit_of_work_factory() as unit_of_work:
                await _validate_correlation(unit_of_work, approval)
                existing = await unit_of_work.approvals.get(approval.approval_id)
                if existing is not None:
                    if _identity(existing) != _identity(approval):
                        raise PermissionDeniedError(
                            "Persisted Plan approval identity is inconsistent.",
                            code="PLAN_APPROVAL_REQUIRED",
                        )
                    return existing
                await unit_of_work.approvals.add(approval)
                return approval
        except (PermissionDeniedError, ToolExecutionError):
            raise
        except Exception as exc:
            raise ToolExecutionError(
                "Nexus could not persist the Plan approval.",
                code="APPROVAL_PERSISTENCE_ERROR",
                retryable=True,
            ) from exc


def _identity(approval: ApprovalRequest) -> tuple[object, ...]:
    return (
        approval.approval_id,
        approval.run_id,
        approval.session_id,
        approval.operation,
        approval.risk_level,
        approval.resource_or_command_summary,
    )


async def _validate_correlation(
    unit_of_work: ApprovalUnitOfWork,
    approval: ApprovalRequest,
) -> None:
    session = await unit_of_work.sessions.get(approval.session_id)
    run = await unit_of_work.runs.get(approval.run_id)
    if session is None or run is None or run.session_id != approval.session_id:
        raise PermissionDeniedError(
            "The Plan approval Run/Session correlation is invalid.",
            code="APPROVAL_CORRELATION_DENIED",
        )
