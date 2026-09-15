from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4, uuid5

import pytest
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver

from nexus.application.diff_service import FinalDiffCollector, FinalDiffEvidence
from nexus.application.execution_ledger import RuntimeEventBuffer, ToolExecutionLedger
from nexus.application.plan_approval_service import PlanApprovalService
from nexus.application.planning import JsonAgentDecisionAdapter, ModelPlanner
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
from nexus.domain.model import ModelChunk, ModelMessage, ModelResponse
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
from nexus.domain.runtime_events import FinalResult, RepairStarted, RuntimeStatus
from nexus.domain.tooling import (
    ApprovalDecision,
    PolicyDecision,
    RiskLevel,
    ToolError,
    ToolInvocation,
    ToolResult,
)
from nexus.domain.validation import (
    ValidationCheck,
    ValidationCheckKind,
    ValidationCheckResult,
    ValidationConfidence,
    ValidationPlan,
    ValidationResult,
    ValidationStatus,
)
from nexus.errors import ModelError
from nexus.infrastructure.graph.day4_runtime import (
    Day4LangGraphRuntime,
    _observation_summary,
)


def _tool_result(
    invocation_id: str,
    tool_name: str,
    *,
    success: bool,
    error_code: str | None = None,
    error_message: str = "safe failure",
    output: dict[str, object] | None = None,
) -> ToolResult:
    return ToolResult(
        invocation_id,
        tool_name,
        success,
        output,
        None if error_code is None else ToolError(error_code, error_message, False),
        RiskLevel.WRITE if tool_name == "apply_patch" else RiskLevel.SAFE,
        PolicyDecision.ALLOWED if error_code is None else PolicyDecision.DENIED,
        ApprovalDecision.APPROVED,
        1,
    )


def test_patch_observation_exposes_only_allowlisted_safe_failure_detail() -> None:
    count_mismatch = _tool_result(
        str(uuid4()),
        "apply_patch",
        success=False,
        error_code="INVALID_PATCH",
        error_message="Patch hunk counts do not match.",
    )
    unknown_message = _tool_result(
        str(uuid4()),
        "apply_patch",
        success=False,
        error_code="INVALID_PATCH",
        error_message="model-authored or dynamic detail",
    )
    action = ToolAction(
        "apply_patch",
        {
            "path": "alpha.py",
            "patch": (
                "--- a/alpha.py\n+++ b/alpha.py\n@@ -1,2 +1,1 @@\n first\n-old\n+new"
            ),
        },
    )

    assert _observation_summary(count_mismatch, action) == (
        "apply_patch failed with INVALID_PATCH: unified-diff hunk header counts do not "
        "match the hunk body; the hunk declares old_count=2 and new_count=1, but its "
        "body contains old_count=2 and new_count=2; regenerate the full patch with "
        "header counts equal to the body counts."
    )
    assert _observation_summary(unknown_message) == "apply_patch failed with INVALID_PATCH."


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


class FailingDecisions:
    def __init__(self, ledger: ToolExecutionLedger) -> None:
        self._ledger = ledger

    async def decide(self, request: AgentDecisionRequest) -> AgentDecision:
        self._ledger.begin_model(request.plan.run_id)
        raise ModelError("Agent model failed.", retryable=True)


class ReadyDecisions:
    async def decide(self, request: AgentDecisionRequest) -> AgentDecision:
        del request
        return AgentDecision(AgentDecisionKind.TASK_READY, None, "Ready to validate.")


class GroundedReadOnlyDecisions:
    async def decide(self, request: AgentDecisionRequest) -> AgentDecision:
        del request
        return AgentDecision(
            AgentDecisionKind.TASK_READY,
            None,
            "Blank records return None; malformed records raise ValueError.",
        )


class ReadOnlyPlanner(FixedPlanner):
    async def create_plan(self, request: PlanningRequest) -> Plan:
        self.calls += 1
        plan_id = str(uuid4())
        steps = (
            PlanStep(
                str(uuid4()),
                1,
                "Explain the selected repository behavior",
                None,
                (),
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
            1,
            request.kind,
            PlanStatus.CREATED,
            ApprovalDecision.PENDING,
            steps,
            scope,
            "Grounded read-only explanation.",
            None,
            None,
            compute_scope_digest(plan_id, 1, scope),
            datetime.now(UTC),
            None,
        )


class QueueAgentGateway:
    def __init__(self, ledger: ToolExecutionLedger, *responses: str) -> None:
        self._ledger = ledger
        self._responses = list(responses)
        self._run_id: str | None = None

    async def complete(self, messages: Sequence[ModelMessage]) -> ModelResponse:
        del messages
        assert self._run_id is not None
        self._ledger.begin_model(self._run_id)
        return ModelResponse(self._responses.pop(0))

    async def stream(self, messages: Sequence[ModelMessage]) -> AsyncIterator[ModelChunk]:
        del messages
        if False:
            yield ModelChunk("")

    def bind_run(self, run_id: str) -> None:
        self._run_id = run_id


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


class RepairableValidationRunner:
    async def run(
        self,
        plan: ValidationPlan,
        **kwargs: Any,
    ) -> ValidationResult:
        del plan
        check_id = str(uuid4())
        check = ValidationCheck(
            check_id,
            1,
            ValidationCheckKind.TEST,
            "shell",
            {"argv": ["pytest", "-q"], "cwd": "."},
            "Focused test is required.",
            True,
        )
        executed = (
            ValidationCheckResult(
                check,
                ValidationStatus.FAIL,
                _tool_result(
                    check_id,
                    "shell",
                    success=False,
                    error_code="COMMAND_EXIT_NONZERO",
                ),
                "Focused test exited non-zero.",
            ),
        )
        return ValidationResult(
            (check,),
            executed,
            ValidationStatus.FAIL,
            ValidationConfidence.HIGH,
            True,
            kwargs["repair_count"],
            "Validation failed with a repairable implementation defect.",
        )


class FailingRepairPlanner(FixedPlanner):
    def __init__(self, ledger: ToolExecutionLedger) -> None:
        super().__init__()
        self._ledger = ledger

    async def create_repair_guidance(
        self, request: RepairPlanningRequest
    ) -> RepairGuidance:
        self._ledger.begin_model(request.plan.run_id)
        raise ModelError("Repair planning model failed.", retryable=True)


class RetryRepairPlanner(FixedPlanner):
    def __init__(self, ledger: ToolExecutionLedger) -> None:
        super().__init__()
        self.gateway = QueueAgentGateway(
            ledger,
            json.dumps(
                {
                    "failure_summary": "Invalid target count.",
                    "steps": [
                        {
                            "description": "Edit alpha",
                            "tool_name": "apply_patch",
                            "target_paths": [],
                            "command_argv": None,
                            "command_cwd": None,
                        }
                    ],
                }
            ),
            json.dumps(
                {
                    "failure_summary": "Use the approved edit.",
                    "steps": [
                        {
                            "description": "Edit alpha",
                            "tool_name": "apply_patch",
                            "target_paths": ["alpha.py"],
                            "command_argv": None,
                            "command_cwd": None,
                        }
                    ],
                }
            ),
        )
        self.model_planner = ModelPlanner(self.gateway)

    async def create_repair_guidance(
        self, request: RepairPlanningRequest
    ) -> RepairGuidance:
        self.gateway.bind_run(request.plan.run_id)
        return await self.model_planner.create_repair_guidance(request)


class RepairThenPassValidationRunner:
    def __init__(self) -> None:
        self.calls = 0

    async def run(
        self,
        plan: ValidationPlan,
        **kwargs: Any,
    ) -> ValidationResult:
        self.calls += 1
        if self.calls == 1:
            return await RepairableValidationRunner().run(plan, **kwargs)
        return await PassValidationRunner().run(plan, **kwargs)


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
    planner: Planner,
    agent: AgentDecisionAdapter,
    tool_runtime: OneActionRuntime,
    max_replans: int = 2,
    validation_runner: ValidationRunner | None = None,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
) -> Day4LangGraphRuntime:
    return Day4LangGraphRuntime(
        explorer=cast(RepositoryExplorer, Explorer()),
        context_builder=cast(ContextBuilder, Builder()),
        planner=planner,
        agent=agent,
        tool_runtime=cast(ToolRuntime, tool_runtime),
        validation_planner=cast(ValidationPlanner, PassValidationPlanner()),
        validation_runner=validation_runner or cast(
            ValidationRunner, PassValidationRunner()
        ),
        plan_approval_service=cast(PlanApprovalService, AutoPlanApprovals()),
        diff_collector=cast(FinalDiffCollector, DiffCollector()),
        event_buffer=event_buffer,
        ledger=ledger,
        approval_mode=ApprovalMode.AUTO,
        max_steps=5,
        max_repair_attempts=3,
        max_replans=max_replans,
        checkpointer=checkpointer,
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
async def test_read_only_task_ready_summary_reaches_final_result() -> None:
    ledger = ToolExecutionLedger()
    events = RuntimeEventBuffer()
    runtime = _runtime(
        ledger=ledger,
        event_buffer=events,
        planner=ReadOnlyPlanner(),
        agent=GroundedReadOnlyDecisions(),
        tool_runtime=OneActionRuntime(ledger),
    )
    state = AgentState(
        "explain record parsing",
        [ModelMessage(role="user", content="explain record parsing")],
        str(uuid4()),
        str(uuid4()),
        RuntimeStatus.STARTED,
    )

    result = await runtime.run(state)
    final = events.drain(state.run_id)[-1]

    assert result.terminal_status is TerminalStatus.SUCCEEDED
    assert isinstance(final, FinalResult)
    assert final.content == (
        "Blank records return None; malformed records raise ValueError."
    )


@pytest.mark.asyncio
async def test_agent_structured_retry_executes_no_tool_before_valid_decision() -> None:
    ledger = ToolExecutionLedger()
    gateway = QueueAgentGateway(
        ledger,
        json.dumps(
            {
                "kind": "TASK_READY",
                "summary": "Malformed action relation.",
                "action": {"tool_name": "apply_patch", "arguments": {}},
            }
        ),
        json.dumps(
            {
                "kind": "TOOL_ACTION",
                "summary": "Apply the approved edit.",
                "action": {
                    "tool_name": "apply_patch",
                    "arguments": {"path": "alpha.py", "patch": "bounded"},
                },
            }
        ),
        json.dumps(
            {"kind": "TASK_READY", "summary": "Ready.", "action": None}
        ),
    )
    runtime = _runtime(
        ledger=ledger,
        event_buffer=RuntimeEventBuffer(),
        planner=FixedPlanner(),
        agent=JsonAgentDecisionAdapter(gateway),
        tool_runtime=OneActionRuntime(ledger),
    )
    state = AgentState(
        "edit alpha",
        [ModelMessage(role="user", content="edit alpha")],
        str(uuid4()),
        str(uuid4()),
        RuntimeStatus.STARTED,
    )
    gateway.bind_run(state.run_id)

    result = await runtime.run(state)

    assert result.terminal_status is TerminalStatus.SUCCEEDED
    assert result.step_count == 2
    assert result.llm_call_count == 3
    assert result.tool_call_count == 1
    assert len(result.observations) == 1


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


@pytest.mark.asyncio
async def test_agent_step_and_failed_model_call_are_checkpointed_on_entry() -> None:
    ledger = ToolExecutionLedger()
    runtime = _runtime(
        ledger=ledger,
        event_buffer=RuntimeEventBuffer(),
        planner=cast(Planner, FixedPlanner()),
        agent=cast(AgentDecisionAdapter, FailingDecisions(ledger)),
        tool_runtime=OneActionRuntime(ledger),
        checkpointer=InMemorySaver(),
    )
    state = AgentState(
        "edit alpha",
        [ModelMessage(role="user", content="edit alpha")],
        str(uuid4()),
        str(uuid4()),
        RuntimeStatus.STARTED,
    )
    thread_id = f"nexus-run:{state.run_id}"

    with pytest.raises(ModelError, match="Agent model failed"):
        await runtime.run(state, thread_id=thread_id)

    snapshot = await runtime._graph.aget_state(  # noqa: SLF001
        {"configurable": {"thread_id": thread_id}}
    )
    assert snapshot.values["step_count"] == 1
    assert snapshot.values["llm_call_count"] == 1


@pytest.mark.asyncio
async def test_repair_attempt_and_failed_planning_call_are_checkpointed_on_entry() -> None:
    ledger = ToolExecutionLedger()
    runtime = _runtime(
        ledger=ledger,
        event_buffer=RuntimeEventBuffer(),
        planner=cast(Planner, FailingRepairPlanner(ledger)),
        agent=cast(AgentDecisionAdapter, ReadyDecisions()),
        tool_runtime=OneActionRuntime(ledger),
        validation_runner=cast(ValidationRunner, RepairableValidationRunner()),
        checkpointer=InMemorySaver(),
    )
    state = AgentState(
        "edit alpha",
        [ModelMessage(role="user", content="edit alpha")],
        str(uuid4()),
        str(uuid4()),
        RuntimeStatus.STARTED,
    )
    thread_id = f"nexus-run:{state.run_id}"

    with pytest.raises(ModelError, match="Repair planning model failed"):
        await runtime.run(state, thread_id=thread_id)

    snapshot = await runtime._graph.aget_state(  # noqa: SLF001
        {"configurable": {"thread_id": thread_id}}
    )
    assert snapshot.values["step_count"] == 1
    assert snapshot.values["repair_count"] == 1
    assert snapshot.values["llm_call_count"] == 1


@pytest.mark.asyncio
async def test_repair_structured_retry_does_not_consume_validation_repair_budget() -> None:
    ledger = ToolExecutionLedger()
    events = RuntimeEventBuffer()
    validation = RepairThenPassValidationRunner()
    runtime = _runtime(
        ledger=ledger,
        event_buffer=events,
        planner=RetryRepairPlanner(ledger),
        agent=cast(AgentDecisionAdapter, ReadyDecisions()),
        tool_runtime=OneActionRuntime(ledger),
        validation_runner=cast(ValidationRunner, validation),
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
    assert result.step_count == 2
    assert result.llm_call_count == 2
    assert result.repair_count == 1
    assert result.tool_call_count == 0
    assert len([event for event in emitted if isinstance(event, RepairStarted)]) == 1
