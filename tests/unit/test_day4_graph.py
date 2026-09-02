from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4, uuid5

import pytest

from nexus.application.diff_service import FinalDiffCollector, FinalDiffEvidence
from nexus.application.execution_ledger import RuntimeEventBuffer, ToolExecutionLedger
from nexus.application.plan_approval_service import PlanApprovalService
from nexus.application.tool_runtime import ToolRuntime
from nexus.config.models import ApprovalMode
from nexus.domain.agent_decision import (
    AgentDecision,
    AgentDecisionKind,
    AgentDecisionRequest,
    ToolAction,
)
from nexus.domain.agent_state import AgentState
from nexus.domain.approvals import ApprovalRequest
from nexus.domain.exploration import (
    ContextBuildRequest,
    ExplorationRequest,
    ExplorationResult,
    WorkingContext,
)
from nexus.domain.model import ModelMessage
from nexus.domain.planning import (
    ApprovedPlanEvidence,
    Plan,
    PlanAuthorizationSource,
    PlanStatus,
    PlanStep,
    PlanStepStatus,
    RepairGuidance,
    TerminalStatus,
    compute_scope_digest,
    derive_authorization_scope,
)
from nexus.domain.ports.agent_decision import AgentDecisionAdapter
from nexus.domain.ports.planning import Planner, PlanningRequest, RepairPlanningRequest
from nexus.domain.ports.repository_context import ContextBuilder, RepositoryExplorer
from nexus.domain.ports.validation import ValidationPlanner, ValidationRunner
from nexus.domain.runtime_events import FinalResult, RuntimeStatus
from nexus.domain.tooling import (
    ApprovalDecision,
    PolicyDecision,
    RiskLevel,
    ToolError,
    ToolInvocation,
    ToolResult,
)
from nexus.domain.validation import (
    ValidationConfidence,
    ValidationPlan,
    ValidationResult,
    ValidationStatus,
)
from nexus.infrastructure.graph.day4_runtime import Day4LangGraphRuntime


def _tool_result(
    invocation_id: str,
    tool_name: str,
    *,
    success: bool,
    error_code: str | None = None,
    output: dict[str, object] | None = None,
) -> ToolResult:
    return ToolResult(
        invocation_id,
        tool_name,
        success,
        output,
        None if error_code is None else ToolError(error_code, "safe failure", False),
        RiskLevel.WRITE if tool_name == "apply_patch" else RiskLevel.SAFE,
        PolicyDecision.ALLOWED if error_code is None else PolicyDecision.DENIED,
        ApprovalDecision.APPROVED,
        1,
    )


class Explorer:
    async def explore(self, request: ExplorationRequest) -> ExplorationResult:
        status = _tool_result(
            str(uuid4()),
            "git_status",
            success=True,
            output={"stdout": "", "truncated": False},
        )
        return ExplorationResult((), (), ("alpha.py",), (), status, (status,), False)


class Builder:
    async def build(self, request: ContextBuildRequest) -> WorkingContext:
        return WorkingContext(request.task, (), (), ("alpha.py",), (), False)


class FixedPlanner:
    def __init__(self) -> None:
        self.calls = 0

    async def create_plan(self, request: PlanningRequest) -> Plan:
        self.calls += 1
        plan_id = str(uuid4()) if request.previous_plan is None else request.previous_plan.plan_id
        version = 1 if request.previous_plan is None else request.previous_plan.version + 1
        steps = (
            PlanStep(
                str(uuid4()),
                1,
                "Edit alpha",
                "apply_patch",
                ("alpha.py",),
                None,
                None,
                PlanStepStatus.PENDING,
            ),
        )
        scope = derive_authorization_scope(steps)
        return Plan(
            plan_id,
            request.run_id,
            request.session_id,
            version,
            request.kind,
            PlanStatus.CREATED,
            ApprovalDecision.PENDING,
            steps,
            scope,
            "Bounded change.",
            request.reason,
            None,
            compute_scope_digest(plan_id, version, scope),
            datetime.now(UTC),
            None,
        )

    async def create_repair_guidance(
        self, request: RepairPlanningRequest
    ) -> RepairGuidance:
        raise AssertionError(request)


class Decisions:
    def __init__(self) -> None:
        self.calls = 0

    async def decide(self, request: AgentDecisionRequest) -> AgentDecision:
        del request
        self.calls += 1
        if self.calls == 1:
            return AgentDecision(
                AgentDecisionKind.TOOL_ACTION,
                ToolAction("apply_patch", {"path": "alpha.py", "patch": "bounded"}),
                "Apply the approved edit.",
            )
        return AgentDecision(AgentDecisionKind.TASK_READY, None, "Ready to validate.")


class OneActionRuntime:
    def __init__(
        self,
        ledger: ToolExecutionLedger,
        *,
        error_code: str | None = None,
    ) -> None:
        self.ledger = ledger
        self.error_code = error_code

    async def execute(
        self,
        invocation: ToolInvocation,
        *,
        authorization: ApprovedPlanEvidence | None = None,
    ) -> ToolResult:
        del authorization
        self.ledger.begin(invocation)
        result = _tool_result(
            invocation.invocation_id,
            invocation.tool_name,
            success=self.error_code is None,
            error_code=self.error_code,
            output=(
                {"path": "alpha.py", "change_kind": "MODIFIED"}
                if self.error_code is None
                else None
            ),
        )
        self.ledger.record(invocation, result)
        return result


class PassValidationPlanner:
    async def plan(self, **kwargs: Any) -> ValidationPlan:
        del kwargs
        return ValidationPlan(())


class PassValidationRunner:
    async def run(
        self,
        plan: ValidationPlan,
        **kwargs: Any,
    ) -> ValidationResult:
        del plan, kwargs
        return ValidationResult(
            (),
            (),
            ValidationStatus.PASS,
            ValidationConfidence.MEDIUM,
            False,
            0,
            "Validation passed.",
        )


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
            "auto",
            now,
            now,
        )

    def activate(self, plan: Plan, approval: ApprovalRequest) -> Plan:
        return PlanApprovalService.activate(plan, approval)

    def evidence(
        self,
        plan: Plan,
        approval: ApprovalRequest,
    ) -> ApprovedPlanEvidence:
        evidence = PlanApprovalService.evidence(plan, approval)
        assert evidence.source is PlanAuthorizationSource.AUTO_MODE
        return evidence


class DiffCollector:
    async def collect(self, **kwargs: Any) -> FinalDiffEvidence:
        changed = kwargs["changed_files"]
        diff = "diff --git a/alpha.py b/alpha.py\n" if changed else ""
        return FinalDiffEvidence(diff, False, ())


def _runtime(
    *,
    ledger: ToolExecutionLedger,
    event_buffer: RuntimeEventBuffer,
    planner: FixedPlanner,
    agent: Decisions,
    tool_runtime: OneActionRuntime,
    max_replans: int = 2,
) -> Day4LangGraphRuntime:
    return Day4LangGraphRuntime(
        explorer=cast(RepositoryExplorer, Explorer()),
        context_builder=cast(ContextBuilder, Builder()),
        planner=cast(Planner, planner),
        agent=cast(AgentDecisionAdapter, agent),
        tool_runtime=cast(ToolRuntime, tool_runtime),
        validation_planner=cast(ValidationPlanner, PassValidationPlanner()),
        validation_runner=cast(ValidationRunner, PassValidationRunner()),
        plan_approval_service=cast(PlanApprovalService, AutoPlanApprovals()),
        diff_collector=cast(FinalDiffCollector, DiffCollector()),
        event_buffer=event_buffer,
        ledger=ledger,
        approval_mode=ApprovalMode.AUTO,
        max_steps=5,
        max_repair_attempts=3,
        max_replans=max_replans,
    )


@pytest.mark.asyncio
async def test_day4_graph_runs_approved_edit_observe_validate_and_finalize() -> None:
    ledger = ToolExecutionLedger()
    events = RuntimeEventBuffer()
    planner = FixedPlanner()
    agent = Decisions()
    runtime = _runtime(
        ledger=ledger,
        event_buffer=events,
        planner=planner,
        agent=agent,
        tool_runtime=OneActionRuntime(ledger),
    )
    state = AgentState(
        "edit alpha",
        [ModelMessage(role="user", content="edit alpha")],
        str(uuid4()),
        str(uuid4()),
        RuntimeStatus.STARTED,
    )

    result = await runtime.run(state)
    emitted = events.drain(state.run_id)

    assert result.terminal_status is TerminalStatus.SUCCEEDED
    assert result.status is RuntimeStatus.COMPLETED
    assert result.plan is not None and result.plan.status is PlanStatus.COMPLETED
    assert len(result.observations) == 1
    assert result.observations[0].replan_reason is None
    assert [item.path for item in result.changed_files] == ["alpha.py"]
    assert result.step_count == 2
    assert result.tool_call_count == 1
    assert planner.calls == 1
    assert isinstance(emitted[-1], FinalResult)


@pytest.mark.asyncio
async def test_plan_scope_denial_is_only_replan_trigger_and_stops_at_limit() -> None:
    ledger = ToolExecutionLedger()
    events = RuntimeEventBuffer()
    planner = FixedPlanner()
    runtime = _runtime(
        ledger=ledger,
        event_buffer=events,
        planner=planner,
        agent=Decisions(),
        tool_runtime=OneActionRuntime(ledger, error_code="PLAN_SCOPE_DENIED"),
        max_replans=0,
    )
    state = AgentState(
        "edit alpha",
        [ModelMessage(role="user", content="edit alpha")],
        str(uuid4()),
        str(uuid4()),
        RuntimeStatus.STARTED,
    )

    result = await runtime.run(state)

    assert result.terminal_status is TerminalStatus.STOPPED_MAX_REPLANS
    assert result.status is RuntimeStatus.FAILED
    assert result.observations[-1].error_code == "PLAN_SCOPE_DENIED"
    assert result.observations[-1].replan_reason is not None
    assert result.replan_count == 0
    assert planner.calls == 1
