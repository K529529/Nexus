"""Policy-governed single-invocation Day 3 Tool Runtime."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace

from nexus.application.approval_service import ApprovalService
from nexus.application.execution_ledger import ToolExecutionLedger
from nexus.application.plan_approval_service import PlanApprovalService
from nexus.domain.approvals import ApprovalRequest
from nexus.domain.planning import ApprovedPlanEvidence
from nexus.domain.ports.tooling import ApprovalPolicy, CommandPolicy, Tool
from nexus.domain.runtime_events import (
    ApprovalActorCategory,
    ApprovalRequested,
    ApprovalResolved,
    ApprovalSubject,
    RuntimeEvent,
    ToolFinished,
    ToolStarted,
)
from nexus.domain.tooling import (
    ApprovalDecision,
    PolicyDecision,
    RiskLevel,
    ToolError,
    ToolInvocation,
    ToolResult,
    risk_rank,
)
from nexus.errors import NexusError, ToolExecutionError
from nexus.tools.registry import ToolRegistry

RuntimeEventEmitter = Callable[[RuntimeEvent], Awaitable[None]]


class ToolRuntime:
    def __init__(
        self,
        registry: ToolRegistry,
        command_policy: CommandPolicy,
        approval_policy: ApprovalPolicy,
        approval_service: ApprovalService,
        *,
        emit: RuntimeEventEmitter | None = None,
        plan_approval_service: PlanApprovalService | None = None,
        normalize_argv: Callable[[list[str]], list[str]] | None = None,
        ledger: ToolExecutionLedger | None = None,
        unsupported_write_operations: frozenset[str] = frozenset(),
    ) -> None:
        self._registry = registry
        self._command_policy = command_policy
        self._approval_policy = approval_policy
        self._approval_service = approval_service
        self._plan_approval_service = plan_approval_service
        self._normalize_argv = normalize_argv or (lambda argv: list(argv))
        self._ledger = ledger
        self._unsupported_write_operations = frozenset(unsupported_write_operations)
        self._emit = emit or _ignore_event

    async def execute(
        self,
        invocation: ToolInvocation,
        *,
        authorization: ApprovedPlanEvidence | None = None,
    ) -> ToolResult:
        if self._ledger is not None:
            self._ledger.begin(invocation)
        started = time.perf_counter()
        try:
            tool = self._registry.resolve(invocation.tool_name)
        except ToolExecutionError as exc:
            result = replace(
                _failure(
                    invocation,
                    risk_level=RiskLevel.DANGEROUS,
                    policy_decision=PolicyDecision.DENIED,
                    approval_decision=None,
                    error=exc,
                ),
                duration_ms=_duration_ms(started),
            )
            await self._emit(
                ToolStarted(
                    run_id=invocation.run_id,
                    session_id=invocation.session_id,
                    invocation_id=invocation.invocation_id,
                    tool_name=invocation.tool_name,
                    risk_level=result.risk_level,
                )
            )
            await self._emit(_finished_event(invocation, result))
            if self._ledger is not None:
                self._ledger.record(invocation, result)
            return result

        proposed_risk = self._command_policy.classify(
            operation=invocation.tool_name,
            arguments=invocation.arguments,
        )
        await self._emit(
            ToolStarted(
                run_id=invocation.run_id,
                session_id=invocation.session_id,
                invocation_id=invocation.invocation_id,
                tool_name=invocation.tool_name,
                risk_level=proposed_risk,
            )
        )
        if proposed_risk is RiskLevel.DANGEROUS:
            result = await self._deny_dangerous(invocation)
        elif (
            proposed_risk is RiskLevel.WRITE
            and invocation.tool_name in self._unsupported_write_operations
        ):
            result = await self._deny_unsupported_mcp_write(invocation)
        elif authorization is not None and invocation.tool_name in {
            "apply_patch",
            "write_file",
            "shell",
        }:
            result = await self._resolve_authorized_action(
                tool,
                invocation,
                authorization,
                proposed_risk,
            )
        elif proposed_risk is RiskLevel.WRITE:
            result = await self._resolve_write(invocation)
        else:
            result = await self._execute_safe(tool, invocation, proposed_risk)
        result = replace(result, duration_ms=_duration_ms(started))
        await self._emit(_finished_event(invocation, result))
        if self._ledger is not None:
            self._ledger.record(invocation, result)
        return result

    async def _resolve_authorized_action(
        self,
        tool: Tool,
        invocation: ToolInvocation,
        authorization: ApprovedPlanEvidence,
        proposed_risk: RiskLevel,
    ) -> ToolResult:
        try:
            if self._plan_approval_service is None:
                raise ToolExecutionError(
                    "Plan authorization validation is unavailable.",
                )
            await self._plan_approval_service.require_approved(authorization)
            if (
                authorization.run_id != invocation.run_id
                or authorization.session_id != invocation.session_id
                or not self._is_in_scope(invocation, authorization)
            ):
                raise ToolExecutionError(
                    "The Tool action is outside the approved Plan scope.",
                    code="PLAN_SCOPE_DENIED",
                )
        except NexusError as exc:
            return _failure(
                invocation,
                risk_level=proposed_risk,
                policy_decision=PolicyDecision.DENIED,
                approval_decision=(
                    ApprovalDecision.APPROVED
                    if exc.code == "PLAN_SCOPE_DENIED"
                    else None
                ),
                error=exc,
            )
        result = await self._execute_safe(tool, invocation, proposed_risk)
        if result.risk_level is RiskLevel.DANGEROUS:
            return result
        return replace(
            result,
            policy_decision=PolicyDecision.ALLOWED,
            approval_decision=ApprovalDecision.APPROVED,
        )

    def _is_in_scope(
        self,
        invocation: ToolInvocation,
        authorization: ApprovedPlanEvidence,
    ) -> bool:
        scope = authorization.authorization_scope
        if invocation.tool_name in {"apply_patch", "write_file"}:
            path = invocation.arguments.get("path")
            return isinstance(path, str) and (
                invocation.tool_name,
                path,
            ) in scope.allowed_write_actions
        if invocation.tool_name == "shell":
            argv = invocation.arguments.get("argv")
            cwd = invocation.arguments.get("cwd", ".")
            if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
                return False
            if not isinstance(cwd, str):
                return False
            return (tuple(self._normalize_argv(argv)), cwd) in scope.allowed_commands
        return False

    async def _execute_safe(
        self,
        tool: Tool,
        invocation: ToolInvocation,
        proposed_risk: RiskLevel,
    ) -> ToolResult:
        try:
            result = await tool.execute(invocation)
        except asyncio.CancelledError:
            raise
        except Exception:
            return _failure(
                invocation,
                risk_level=proposed_risk,
                policy_decision=PolicyDecision.ALLOWED,
                approval_decision=None,
                error=ToolExecutionError("The Tool failed safely."),
            )
        if result.invocation_id != invocation.invocation_id or result.tool_name != tool.name:
            return _failure(
                invocation,
                risk_level=RiskLevel.DANGEROUS,
                policy_decision=PolicyDecision.DENIED,
                approval_decision=None,
                error=ToolExecutionError("The Tool returned mismatched identity."),
            )
        if risk_rank(result.risk_level) < risk_rank(proposed_risk):
            return _failure(
                invocation,
                risk_level=RiskLevel.DANGEROUS,
                policy_decision=PolicyDecision.DENIED,
                approval_decision=None,
                error=ToolExecutionError("The Tool attempted to downgrade security risk."),
            )
        if result.risk_level is RiskLevel.DANGEROUS:
            try:
                decision = await self._approval_service.record_denied(
                    invocation,
                    risk_level=RiskLevel.DANGEROUS,
                    summary=_safe_summary(invocation),
                    reason="Resource security validation escalated the operation risk.",
                )
                await self._emit(_resolved_event(invocation, decision))
            except NexusError as exc:
                return _failure(
                    invocation,
                    risk_level=RiskLevel.DANGEROUS,
                    policy_decision=PolicyDecision.DENIED,
                    approval_decision=None,
                    error=exc,
                )
            if result.success:
                result = replace(
                    result,
                    success=False,
                    output=None,
                    error=ToolError(
                        "PERMISSION_DENIED",
                        "Resource security validation denied the operation.",
                        False,
                    ),
                )
            result = replace(
                result,
                policy_decision=PolicyDecision.DENIED,
                approval_decision=ApprovalDecision.DENIED,
            )
        elif risk_rank(result.risk_level) > risk_rank(proposed_risk):
            return _failure(
                invocation,
                risk_level=result.risk_level,
                policy_decision=PolicyDecision.DENIED,
                approval_decision=None,
                error=ToolExecutionError(
                    "The escalated operation requires authorization before execution."
                ),
            )
        return result

    async def _resolve_write(self, invocation: ToolInvocation) -> ToolResult:
        try:
            pending = await self._approval_service.create_pending(
                invocation,
                risk_level=RiskLevel.WRITE,
                summary=_safe_summary(invocation),
            )
            await self._emit(
                ApprovalRequested(
                    run_id=invocation.run_id,
                    session_id=invocation.session_id,
                    approval_id=pending.approval_id,
                    invocation_id=invocation.invocation_id,
                    operation=invocation.tool_name,
                    risk_level=RiskLevel.WRITE,
                    resource_or_command_summary=pending.resource_or_command_summary,
                )
            )
            decision = await self._approval_policy.request(pending)
            _validate_approval_decision(pending, decision)
            decision = await self._approval_service.persist_decision(decision)
            await self._emit(_resolved_event(invocation, decision))
        except asyncio.CancelledError:
            raise
        except NexusError as exc:
            return _failure(
                invocation,
                risk_level=RiskLevel.WRITE,
                policy_decision=PolicyDecision.DENIED,
                approval_decision=None,
                error=exc,
            )
        except Exception:
            return _failure(
                invocation,
                risk_level=RiskLevel.WRITE,
                policy_decision=PolicyDecision.DENIED,
                approval_decision=None,
                error=ToolExecutionError("The approval decision source failed safely."),
            )
        if decision.decision is ApprovalDecision.APPROVED:
            error = ToolExecutionError(
                "WRITE execution requires an approved Plan, which Day 3 does not provide.",
                code="PLAN_REQUIRED",
            )
        else:
            error = ToolExecutionError(
                "The WRITE operation was denied.",
                code="COMMAND_DENIED",
            )
        return _failure(
            invocation,
            risk_level=RiskLevel.WRITE,
            policy_decision=PolicyDecision.DENIED,
            approval_decision=decision.decision,
            error=error,
        )

    async def _deny_dangerous(self, invocation: ToolInvocation) -> ToolResult:
        try:
            decision = await self._approval_service.record_denied(
                invocation,
                risk_level=RiskLevel.DANGEROUS,
                summary=_safe_summary(invocation),
                reason="The operation is hard-denied by the Day 3 security policy.",
            )
            await self._emit(_resolved_event(invocation, decision))
        except NexusError as exc:
            return _failure(
                invocation,
                risk_level=RiskLevel.DANGEROUS,
                policy_decision=PolicyDecision.DENIED,
                approval_decision=None,
                error=exc,
            )
        return _failure(
            invocation,
            risk_level=RiskLevel.DANGEROUS,
            policy_decision=PolicyDecision.DENIED,
            approval_decision=decision.decision,
            error=ToolExecutionError(
                "The operation is hard-denied by the Day 3 security policy.",
                code="PERMISSION_DENIED",
            ),
        )

    async def _deny_unsupported_mcp_write(
        self,
        invocation: ToolInvocation,
    ) -> ToolResult:
        try:
            decision = await self._approval_service.record_denied(
                invocation,
                risk_level=RiskLevel.WRITE,
                summary=_safe_summary(invocation),
                reason="Generic MCP WRITE authorization is unavailable in Day 6.",
            )
            await self._emit(_resolved_event(invocation, decision))
        except NexusError as exc:
            return _failure(
                invocation,
                risk_level=RiskLevel.WRITE,
                policy_decision=PolicyDecision.DENIED,
                approval_decision=None,
                error=exc,
            )
        return _failure(
            invocation,
            risk_level=RiskLevel.WRITE,
            policy_decision=PolicyDecision.DENIED,
            approval_decision=decision.decision,
            error=ToolExecutionError(
                "Generic MCP WRITE authorization is unavailable in Day 6.",
                code="MCP_WRITE_NOT_AUTHORIZED",
            ),
        )


async def _ignore_event(event: RuntimeEvent) -> None:
    del event


def _finished_event(invocation: ToolInvocation, result: ToolResult) -> ToolFinished:
    return ToolFinished(
        run_id=invocation.run_id,
        session_id=invocation.session_id,
        invocation_id=invocation.invocation_id,
        tool_name=invocation.tool_name,
        success=result.success,
        risk_level=result.risk_level,
        policy_decision=result.policy_decision,
        approval_decision=result.approval_decision,
        duration_ms=result.duration_ms,
        error_code=None if result.error is None else result.error.code,
    )


def _resolved_event(
    invocation: ToolInvocation, approval: ApprovalRequest
) -> ApprovalResolved:
    return ApprovalResolved(
        run_id=invocation.run_id,
        session_id=invocation.session_id,
        approval_id=approval.approval_id,
        subject=ApprovalSubject.TOOL,
        invocation_id=invocation.invocation_id,
        plan_id=None,
        plan_version=None,
        decision=approval.decision,
        actor_category=(
            ApprovalActorCategory.USER
            if approval.actor == "user"
            else ApprovalActorCategory.POLICY
        ),
    )


def _failure(
    invocation: ToolInvocation,
    *,
    risk_level: RiskLevel,
    policy_decision: PolicyDecision,
    approval_decision: ApprovalDecision | None,
    error: NexusError,
) -> ToolResult:
    return ToolResult(
        invocation.invocation_id,
        invocation.tool_name,
        False,
        None,
        ToolError(error.code, str(error), error.retryable),
        risk_level,
        policy_decision,
        approval_decision,
        0,
    )


def _safe_summary(invocation: ToolInvocation) -> str:
    if invocation.tool_name == "shell":
        argv = invocation.arguments.get("argv")
        if isinstance(argv, list) and argv:
            executable = str(argv[0]).rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
            return f"shell:{executable} ({len(argv)} argv items)"[:512]
    path = invocation.arguments.get("path")
    if isinstance(path, str):
        return f"{invocation.tool_name}:{path}"[:512]
    return invocation.tool_name[:512]


def _validate_approval_decision(
    pending: ApprovalRequest,
    decision: ApprovalRequest,
) -> None:
    immutable_fields = (
        "approval_id",
        "run_id",
        "session_id",
        "operation",
        "risk_level",
        "resource_or_command_summary",
        "created_at",
    )
    if any(getattr(pending, field) != getattr(decision, field) for field in immutable_fields):
        raise ToolExecutionError(
            "The approval policy changed immutable request fields.",
            code="INVALID_APPROVAL_TRANSITION",
        )


def _duration_ms(started: float) -> int:
    return max(0, round((time.perf_counter() - started) * 1000))
