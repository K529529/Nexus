from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4, uuid5

import pytest

from nexus.application.approval_service import ApprovalService
from nexus.application.diff_service import FinalDiffCollector, FinalDiffEvidence
from nexus.application.execution_ledger import RuntimeEventBuffer, ToolExecutionLedger
from nexus.application.plan_approval_service import PlanApprovalService
from nexus.application.planning import JsonAgentDecisionAdapter, ModelPlanner
from nexus.application.tool_runtime import ToolRuntime
from nexus.config.models import ApprovalMode
from nexus.domain.agent_state import AgentState
from nexus.domain.approvals import ApprovalRequest
from nexus.domain.exploration import (
    ContextBuildRequest,
    ExplorationRequest,
    ExplorationResult,
    WorkingContext,
)
from nexus.domain.mcp import MCPConnectionInfo, MCPToolDescriptor
from nexus.domain.model import ModelChunk, ModelMessage, ModelResponse
from nexus.domain.planning import ApprovedPlanEvidence, Plan, TerminalStatus
from nexus.domain.ports.context import ContextManager
from nexus.domain.ports.mcp import MCPManager
from nexus.domain.ports.repository_context import ContextBuilder, RepositoryExplorer
from nexus.domain.ports.tooling import ApprovalPolicy
from nexus.domain.ports.validation import ValidationPlanner, ValidationRunner
from nexus.domain.runtime_events import FinalResult, RuntimeStatus, ToolFinished, ToolStarted
from nexus.domain.tooling import (
    ApprovalDecision,
    JsonObject,
    PolicyDecision,
    RiskLevel,
    ToolResult,
)
from nexus.domain.validation import (
    ValidationConfidence,
    ValidationPlan,
    ValidationResult,
    ValidationStatus,
)
from nexus.infrastructure.graph.day4_runtime import Day4LangGraphRuntime
from nexus.security.command_policy import DefaultCommandPolicy
from nexus.security.executables import TrustedExecutables
from nexus.tools.mcp import MCPToolAdapter
from nexus.tools.registry import ToolRegistry


class ModelDrivenGateway:
    def __init__(self) -> None:
        responses = [
            {
                "rationale_summary": "Use the advertised SAFE MCP echo capability.",
                "steps": [
                    {
                        "description": "Echo through MCP",
                        "tool_name": "mcp.everything.echo",
                        "target_paths": [],
                        "command_argv": None,
                        "command_cwd": None,
                    }
                ],
            },
            {
                "kind": "TOOL_ACTION",
                "summary": "Invoke MCP echo.",
                "action": {
                    "tool_name": "mcp.everything.echo",
                    "arguments": {"message": "day6 integration"},
                },
            },
            {
                "kind": "TASK_READY",
                "summary": "The MCP result was observed.",
                "action": None,
            },
        ]
        self._responses = [json.dumps(response) for response in responses]
        self.messages: list[Sequence[ModelMessage]] = []

    async def complete(self, messages: Sequence[ModelMessage]) -> ModelResponse:
        self.messages.append(tuple(messages))
        return ModelResponse(self._responses.pop(0))

    async def stream(
        self, messages: Sequence[ModelMessage]
    ) -> AsyncIterator[ModelChunk]:
        del messages
        if False:
            yield ModelChunk("")


class EchoManager:
    def __init__(self) -> None:
        self.calls: list[JsonObject] = []

    async def connect(self) -> tuple[MCPConnectionInfo, ...]:
        return ()

    async def list_tools(self) -> tuple[MCPToolDescriptor, ...]:
        return ()

    async def call_tool(
        self,
        *,
        server_id: str,
        remote_name: str,
        arguments: JsonObject,
        timeout_seconds: float,
    ) -> JsonObject:
        assert (server_id, remote_name, timeout_seconds) == ("everything", "echo", 5)
        self.calls.append(dict(arguments))
        return {
            "content": [{"type": "text", "text": arguments["message"]}],
            "structured_content": {"message": arguments["message"]},
            "is_error": False,
        }

    async def close(self) -> None:
        return None


class Explorer:
    async def explore(self, request: ExplorationRequest) -> ExplorationResult:
        result = ToolResult(
            str(uuid4()),
            "git_status",
            True,
            {"stdout": "", "truncated": False},
            None,
            RiskLevel.SAFE,
            PolicyDecision.ALLOWED,
            None,
            0,
        )
        return ExplorationResult((), (), (), (), result, (result,), False)


class Builder:
    async def build(self, request: ContextBuildRequest) -> WorkingContext:
        return WorkingContext(request.task, (), (), (), (), False)


class AutoPlanApprovals:
    async def create_auto_approved(self, plan: Plan) -> ApprovalRequest:
        now = datetime.now(UTC)
        return ApprovalRequest(
            str(uuid5(UUID(plan.plan_id), f"approval:v{plan.version}")),
            plan.run_id,
            plan.session_id,
            "approve_plan",
            RiskLevel.WRITE,
            f"scope:{plan.scope_digest}",
            ApprovalDecision.APPROVED,
            "auto_policy",
            "fixture",
            now,
            now,
        )

    def activate(self, plan: Plan, approval: ApprovalRequest) -> Plan:
        return PlanApprovalService.activate(plan, approval)

    def evidence(
        self, plan: Plan, approval: ApprovalRequest
    ) -> ApprovedPlanEvidence:
        return PlanApprovalService.evidence(plan, approval)


class PassValidationPlanner:
    async def plan(self, **kwargs: Any) -> ValidationPlan:
        del kwargs
        return ValidationPlan(())


class PassValidationRunner:
    async def run(self, plan: ValidationPlan, **kwargs: Any) -> ValidationResult:
        del plan, kwargs
        return ValidationResult(
            (),
            (),
            ValidationStatus.PASS,
            ValidationConfidence.MEDIUM,
            False,
            0,
            "No repository changes require code validation.",
        )


class DiffCollector:
    async def collect(self, **kwargs: Any) -> FinalDiffEvidence:
        del kwargs
        return FinalDiffEvidence("", False, ())


class UnusedApprovalService:
    pass


class UnusedApprovalPolicy:
    async def request(self, request: ApprovalRequest) -> ApprovalRequest:
        raise AssertionError(request)


@pytest.mark.asyncio
async def test_model_driven_safe_mcp_action_traverses_execute_and_observe() -> None:
    metadata: tuple[JsonObject, ...] = (
        {
            "registry_name": "mcp.everything.echo",
            "description": "Echo a message.",
            "input_schema": {
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
            },
            "risk_level": "SAFE",
            "source": "mcp",
            "server_id": "everything",
        },
    )
    descriptor = MCPToolDescriptor(
        "everything",
        "echo",
        "mcp.everything.echo",
        "Echo a message.",
        cast(JsonObject, metadata[0]["input_schema"]),
    )
    manager = EchoManager()
    tool = MCPToolAdapter(
        descriptor,
        cast(MCPManager, manager),
        risk_level=RiskLevel.SAFE,
        timeout_seconds=5,
    )
    events = RuntimeEventBuffer()
    ledger = ToolExecutionLedger()
    policy = DefaultCommandPolicy(
        TrustedExecutables("python", None, None, None, None, None),
        {tool.name: RiskLevel.SAFE},
    )
    tool_runtime = ToolRuntime(
        ToolRegistry([tool]),
        policy,
        cast(ApprovalPolicy, UnusedApprovalPolicy()),
        cast(ApprovalService, UnusedApprovalService()),
        emit=events.emit,
        ledger=ledger,
    )
    gateway = ModelDrivenGateway()
    runtime = Day4LangGraphRuntime(
        explorer=cast(RepositoryExplorer, Explorer()),
        context_builder=cast(ContextBuilder, Builder()),
        planner=ModelPlanner(gateway, tool_metadata=metadata),
        agent=JsonAgentDecisionAdapter(gateway, tool_metadata=metadata),
        tool_runtime=tool_runtime,
        validation_planner=cast(ValidationPlanner, PassValidationPlanner()),
        validation_runner=cast(ValidationRunner, PassValidationRunner()),
        plan_approval_service=cast(PlanApprovalService, AutoPlanApprovals()),
        diff_collector=cast(FinalDiffCollector, DiffCollector()),
        event_buffer=events,
        ledger=ledger,
        approval_mode=ApprovalMode.AUTO,
        max_steps=5,
        max_repair_attempts=0,
        max_replans=0,
        context_manager=cast(ContextManager | None, None),
    )
    state = AgentState(
        "Echo day6 integration through MCP",
        [ModelMessage(role="user", content="Echo day6 integration through MCP")],
        str(uuid4()),
        str(uuid4()),
        RuntimeStatus.STARTED,
    )

    result = await runtime.run(state)
    emitted = events.drain(state.run_id)

    assert result.terminal_status is TerminalStatus.SUCCEEDED
    assert result.tool_call_count == 1
    assert result.plan is not None
    assert result.plan.steps[0].tool_name == "mcp.everything.echo"
    assert manager.calls == [{"message": "day6 integration"}]
    assert len(result.observations) == 1
    assert result.observations[0].tool_name == "mcp.everything.echo"
    assert result.observations[0].success is True
    assert sum(isinstance(event, ToolStarted) for event in emitted) == 1
    assert sum(isinstance(event, ToolFinished) for event in emitted) == 1
    assert isinstance(emitted[-1], FinalResult)
    assert len(gateway.messages) == 3
