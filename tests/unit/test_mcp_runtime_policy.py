from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import cast
from uuid import uuid4

import pytest

from nexus.application.approval_service import ApprovalService
from nexus.application.execution_ledger import ToolExecutionLedger
from nexus.application.tool_runtime import ToolRuntime
from nexus.domain.approvals import ApprovalRequest
from nexus.domain.ports.tooling import ApprovalPolicy
from nexus.domain.runtime_events import (
    ApprovalRequested,
    RuntimeEvent,
    ToolFinished,
    ToolStarted,
)
from nexus.domain.tooling import (
    ApprovalDecision,
    PolicyDecision,
    RiskLevel,
    ToolInvocation,
    ToolResult,
)
from nexus.security.approval_policies import AutoApprovalPolicy
from nexus.security.command_policy import DefaultCommandPolicy
from nexus.security.executables import TrustedExecutables
from nexus.tools.registry import ToolRegistry


class RecordingMCPTool:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls = 0

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        self.calls += 1
        return ToolResult(
            invocation.invocation_id,
            invocation.tool_name,
            True,
            {"content": [{"type": "text", "text": "ok"}], "is_error": False},
            None,
            RiskLevel.SAFE,
            PolicyDecision.ALLOWED,
            None,
            0,
        )


class RecordingApprovalService:
    def __init__(self) -> None:
        self.denied: list[tuple[ToolInvocation, RiskLevel, str]] = []
        self.pending_calls = 0

    async def record_denied(
        self,
        invocation: ToolInvocation,
        *,
        risk_level: RiskLevel,
        summary: str,
        reason: str,
    ) -> ApprovalRequest:
        del summary
        self.denied.append((invocation, risk_level, reason))
        now = datetime.now(UTC)
        return ApprovalRequest(
            str(uuid4()),
            invocation.run_id,
            invocation.session_id,
            invocation.tool_name,
            risk_level,
            invocation.tool_name,
            ApprovalDecision.DENIED,
            "security_policy",
            reason,
            now,
            now,
        )

    async def create_pending(self, *args: object, **kwargs: object) -> ApprovalRequest:
        del args, kwargs
        self.pending_calls += 1
        raise AssertionError("MCP WRITE must not create a pending approval")

    async def persist_decision(self, approval: ApprovalRequest) -> ApprovalRequest:
        raise AssertionError(approval)


class NeverCalledApprovalPolicy:
    def __init__(self) -> None:
        self.calls = 0

    async def request(self, request: ApprovalRequest) -> ApprovalRequest:
        self.calls += 1
        raise AssertionError(request)


def _policy(name: str, risk: RiskLevel) -> DefaultCommandPolicy:
    executables = TrustedExecutables("python", None, None, None, None, None)
    return DefaultCommandPolicy(executables, {name: risk})


def _invocation(name: str) -> ToolInvocation:
    return ToolInvocation(
        str(uuid4()),
        name,
        {"message": "hello"},
        str(uuid4()),
        str(uuid4()),
    )


def _collector(events: list[RuntimeEvent]) -> Callable[[RuntimeEvent], Awaitable[None]]:
    async def emit(event: RuntimeEvent) -> None:
        events.append(event)

    return emit


@pytest.mark.asyncio
async def test_safe_mcp_call_executes_once_and_emits_one_event_pair() -> None:
    name = "mcp.everything.echo"
    invocation = _invocation(name)
    tool = RecordingMCPTool(name)
    approvals = RecordingApprovalService()
    ledger = ToolExecutionLedger()
    events: list[RuntimeEvent] = []
    runtime = ToolRuntime(
        ToolRegistry([tool]),
        _policy(name, RiskLevel.SAFE),
        cast(ApprovalPolicy, NeverCalledApprovalPolicy()),
        cast(ApprovalService, approvals),
        ledger=ledger,
        emit=_collector(events),
    )

    result = await runtime.execute(invocation)

    assert result.success
    assert tool.calls == 1
    assert ledger.count(invocation.run_id) == 1
    assert [type(event) for event in events] == [ToolStarted, ToolFinished]
    assert approvals.denied == []


@pytest.mark.asyncio
async def test_write_mcp_fails_closed_before_approval_adapter_or_server() -> None:
    name = "mcp.everything.write"
    invocation = _invocation(name)
    tool = RecordingMCPTool(name)
    approvals = RecordingApprovalService()
    approval_policy = NeverCalledApprovalPolicy()
    events: list[RuntimeEvent] = []
    runtime = ToolRuntime(
        ToolRegistry([tool]),
        _policy(name, RiskLevel.WRITE),
        cast(ApprovalPolicy, approval_policy),
        cast(ApprovalService, approvals),
        emit=_collector(events),
        unsupported_write_operations=frozenset({name}),
    )

    result = await runtime.execute(invocation)

    assert not result.success
    assert result.risk_level is RiskLevel.WRITE
    assert result.policy_decision is PolicyDecision.DENIED
    assert result.approval_decision is ApprovalDecision.DENIED
    assert result.error is not None and result.error.code == "MCP_WRITE_NOT_AUTHORIZED"
    assert tool.calls == 0
    assert approval_policy.calls == 0
    assert approvals.pending_calls == 0
    assert len(approvals.denied) == 1
    assert approvals.denied[0][1] is RiskLevel.WRITE
    assert [type(event) for event in events] == [ToolStarted, ToolFinished]
    assert not any(isinstance(event, ApprovalRequested) for event in events)
    finished = cast(ToolFinished, events[-1])
    assert finished.error_code == "MCP_WRITE_NOT_AUTHORIZED"


@pytest.mark.asyncio
async def test_dangerous_mcp_is_denied_in_auto_mode_before_adapter_or_server() -> None:
    name = "mcp.everything.unconfigured"
    tool = RecordingMCPTool(name)
    approvals = RecordingApprovalService()
    runtime = ToolRuntime(
        ToolRegistry([tool]),
        _policy(name, RiskLevel.DANGEROUS),
        AutoApprovalPolicy(),
        cast(ApprovalService, approvals),
    )

    result = await runtime.execute(_invocation(name))

    assert not result.success
    assert result.risk_level is RiskLevel.DANGEROUS
    assert result.policy_decision is PolicyDecision.DENIED
    assert result.approval_decision is ApprovalDecision.DENIED
    assert tool.calls == 0
    assert len(approvals.denied) == 1


def test_risk_mapping_is_exact_and_never_inferred_from_name() -> None:
    executables = TrustedExecutables("python", None, None, None, None, None)
    policy = DefaultCommandPolicy(
        executables,
        {
            "mcp.everything.delete_everything": RiskLevel.SAFE,
            "mcp.everything.harmless_read": RiskLevel.WRITE,
        },
    )

    assert (
        policy.classify(operation="mcp.everything.delete_everything", arguments={})
        is RiskLevel.SAFE
    )
    assert (
        policy.classify(operation="mcp.everything.harmless_read", arguments={})
        is RiskLevel.WRITE
    )
    assert (
        policy.classify(operation="mcp.everything.unknown_read", arguments={})
        is RiskLevel.DANGEROUS
    )
