from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import asdict
from datetime import UTC, datetime
from typing import cast
from uuid import uuid4

import pytest

from nexus.application.approval_service import ApprovalService
from nexus.application.tool_runtime import ToolRuntime
from nexus.domain.approvals import ApprovalRequest
from nexus.domain.runtime_events import (
    ApprovalRequested,
    ApprovalResolved,
    RuntimeEvent,
    RuntimeStatus,
    ToolFinished,
    ToolStarted,
)
from nexus.domain.tooling import (
    ApprovalDecision,
    JsonObject,
    PolicyDecision,
    RiskLevel,
    ToolError,
    ToolInvocation,
    ToolResult,
)
from nexus.errors import ToolExecutionError
from nexus.security.approval_policies import InteractiveApprovalPolicy
from nexus.tools.registry import ToolRegistry


class FixedPolicy:
    def __init__(self, risk: RiskLevel) -> None:
        self._risk = risk

    def classify(self, *, operation: str, arguments: JsonObject) -> RiskLevel:
        del operation, arguments
        return self._risk


class RecordingTool:
    name = "record"

    def __init__(self, order: list[str], *, final_risk: RiskLevel = RiskLevel.SAFE) -> None:
        self.order = order
        self.final_risk = final_risk
        self.called = False

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        self.called = True
        self.order.append(f"execute:{invocation.arguments['label']}")
        dangerous = self.final_risk is RiskLevel.DANGEROUS
        return ToolResult(
            invocation.invocation_id,
            self.name,
            not dangerous,
            None if dangerous else {"ok": True},
            (
                ToolError("WORKSPACE_PATH_DENIED", "escape denied", False)
                if dangerous
                else None
            ),
            self.final_risk,
            PolicyDecision.DENIED if dangerous else PolicyDecision.ALLOWED,
            None,
            1,
        )


class FakeApprovalService:
    def __init__(self) -> None:
        self.records: list[ApprovalRequest] = []

    async def create_pending(
        self,
        invocation: ToolInvocation,
        *,
        risk_level: RiskLevel,
        summary: str,
    ) -> ApprovalRequest:
        request = _approval(invocation, risk_level, summary, ApprovalDecision.PENDING)
        self.records.append(request)
        return request

    async def persist_decision(self, approval: ApprovalRequest) -> ApprovalRequest:
        self.records.append(approval)
        return approval

    async def record_denied(
        self,
        invocation: ToolInvocation,
        *,
        risk_level: RiskLevel,
        summary: str,
        reason: str,
    ) -> ApprovalRequest:
        del reason
        request = _approval(invocation, risk_level, summary, ApprovalDecision.DENIED)
        self.records.append(request)
        return request


def _invocation(label: str, *, tool_name: str = "record") -> ToolInvocation:
    return ToolInvocation(
        str(uuid4()),
        tool_name,
        {"label": label},
        str(uuid4()),
        str(uuid4()),
    )


def _approval(
    invocation: ToolInvocation,
    risk: RiskLevel,
    summary: str,
    decision: ApprovalDecision,
) -> ApprovalRequest:
    now = datetime.now(UTC)
    return ApprovalRequest(
        str(uuid4()),
        invocation.run_id,
        invocation.session_id,
        invocation.tool_name,
        risk,
        summary,
        decision,
        "runtime" if decision is ApprovalDecision.PENDING else "security_policy",
        None,
        now,
        None if decision is ApprovalDecision.PENDING else now,
    )


@pytest.mark.asyncio
async def test_single_execute_calls_are_deterministically_sequential() -> None:
    order: list[str] = []
    events: list[RuntimeEvent] = []
    tool = RecordingTool(order)

    async def emit(event: RuntimeEvent) -> None:
        events.append(event)
        order.append(type(event).__name__)

    runtime = ToolRuntime(
        ToolRegistry([tool]),
        FixedPolicy(RiskLevel.SAFE),
        InteractiveApprovalPolicy(_approve),
        cast(ApprovalService, FakeApprovalService()),
        emit=emit,
    )
    first = await runtime.execute(_invocation("a"))
    second = await runtime.execute(_invocation("b"))

    assert first.success and second.success
    assert order == [
        "ToolStarted",
        "execute:a",
        "ToolFinished",
        "ToolStarted",
        "execute:b",
        "ToolFinished",
    ]
    assert isinstance(events[-1], ToolFinished)
    assert events[-1].status is RuntimeStatus.STARTED


@pytest.mark.asyncio
async def test_resource_escape_escalates_final_risk_without_terminalizing_run() -> None:
    events: list[RuntimeEvent] = []
    approvals = FakeApprovalService()
    runtime = ToolRuntime(
        ToolRegistry([RecordingTool([], final_risk=RiskLevel.DANGEROUS)]),
        FixedPolicy(RiskLevel.SAFE),
        InteractiveApprovalPolicy(_approve),
        cast(ApprovalService, approvals),
        emit=_collector(events),
    )

    result = await runtime.execute(_invocation("escape"))

    assert not result.success
    assert result.risk_level is RiskLevel.DANGEROUS
    assert result.approval_decision is ApprovalDecision.DENIED
    assert [type(event) for event in events] == [
        ToolStarted,
        ApprovalResolved,
        ToolFinished,
    ]
    started, _, finished = events
    assert isinstance(started, ToolStarted) and started.risk_level is RiskLevel.SAFE
    assert isinstance(finished, ToolFinished)
    assert finished.risk_level is RiskLevel.DANGEROUS
    assert finished.status is RuntimeStatus.STARTED
    assert len(approvals.records) == 1


@pytest.mark.asyncio
async def test_write_approval_is_persisted_but_tool_is_not_executed_without_plan() -> None:
    events: list[RuntimeEvent] = []
    approvals = FakeApprovalService()
    tool = RecordingTool([])
    runtime = ToolRuntime(
        ToolRegistry([tool]),
        FixedPolicy(RiskLevel.WRITE),
        InteractiveApprovalPolicy(_approve),
        cast(ApprovalService, approvals),
        emit=_collector(events),
    )

    result = await runtime.execute(_invocation("write"))

    assert not tool.called
    assert not result.success
    assert result.approval_decision is ApprovalDecision.APPROVED
    assert result.error is not None and result.error.code == "PLAN_REQUIRED"
    assert [type(event) for event in events] == [
        ToolStarted,
        ApprovalRequested,
        ApprovalResolved,
        ToolFinished,
    ]


@pytest.mark.asyncio
async def test_unknown_tool_emits_start_finish_pair_with_tool_not_found() -> None:
    events: list[RuntimeEvent] = []
    approvals = FakeApprovalService()
    runtime = ToolRuntime(
        ToolRegistry([]),
        FixedPolicy(RiskLevel.SAFE),
        InteractiveApprovalPolicy(_approve),
        cast(ApprovalService, approvals),
        emit=_collector(events),
    )

    result = await runtime.execute(_invocation("missing", tool_name="not_registered"))

    assert not result.success
    assert result.error is not None and result.error.code == "TOOL_NOT_FOUND"
    assert [type(event) for event in events] == [ToolStarted, ToolFinished]
    started, finished = events
    assert isinstance(started, ToolStarted)
    assert started.risk_level is RiskLevel.DANGEROUS
    assert isinstance(finished, ToolFinished)
    assert finished.error_code == "TOOL_NOT_FOUND"
    assert finished.risk_level is RiskLevel.DANGEROUS
    assert approvals.records == []


def test_tool_result_has_structured_serializable_fields() -> None:
    result = ToolResult(
        str(uuid4()),
        "read_file",
        False,
        None,
        ToolError("TOOL_EXECUTION_ERROR", "safe", False),
        RiskLevel.SAFE,
        PolicyDecision.ALLOWED,
        None,
        3,
    )
    values = asdict(result)
    assert values["error"] == {
        "code": "TOOL_EXECUTION_ERROR",
        "message": "safe",
        "retryable": False,
    }


def test_registry_is_exact_immutable_and_rejects_duplicates() -> None:
    tool = RecordingTool([])
    registry = ToolRegistry([tool])

    assert registry.resolve("record") is tool
    with pytest.raises(ToolExecutionError) as missing:
        registry.resolve("Record")
    assert missing.value.code == "TOOL_NOT_FOUND"
    with pytest.raises(ValueError, match="Duplicate Tool name"):
        ToolRegistry([tool, tool])


def test_tool_event_serialization_uses_safe_enum_values() -> None:
    event = ToolFinished(
        run_id=str(uuid4()),
        session_id=str(uuid4()),
        invocation_id=str(uuid4()),
        tool_name="read_file",
        success=False,
        risk_level=RiskLevel.DANGEROUS,
        policy_decision=PolicyDecision.DENIED,
        approval_decision=ApprovalDecision.DENIED,
        duration_ms=2,
        error_code="WORKSPACE_PATH_DENIED",
    )

    serialized = event.to_dict()
    assert serialized["status"] == "STARTED"
    assert serialized["payload"]["risk_level"] == "DANGEROUS"
    assert serialized["payload"]["policy_decision"] == "DENIED"


async def _approve(
    request: ApprovalRequest,
) -> tuple[ApprovalDecision, str | None]:
    del request
    return ApprovalDecision.APPROVED, "approved for test"


def _collector(
    events: list[RuntimeEvent],
) -> Callable[[RuntimeEvent], Awaitable[None]]:
    async def emit(event: RuntimeEvent) -> None:
        events.append(event)

    return emit
