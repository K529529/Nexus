from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
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
    AgentRuntimeFeedback,
    Observation,
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
    CompletionRequirement,
    Plan,
    PlanAuthorizationSource,
    PlanKind,
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
from nexus.domain.runtime_events import (
    AgentSemanticRetryStarted,
    AgentStepCompleted,
    FinalResult,
    PlanStepCompleted,
    RepairStarted,
    RuntimeStatus,
)
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
    _action_fingerprint,
    _activate_first_plan_step,
    _complete_active_plan_step,
    _observation_summary,
)
from nexus.security.workspace import WorkspaceGuard
from nexus.tools.editing import EditFileTool


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


def test_read_file_observation_preserves_range_and_complete_small_content() -> None:
    result = _tool_result(
        str(uuid4()),
        "read_file",
        success=True,
        output={
            "path": "pvlib/iam.py",
            "start_line": 401,
            "end_line": 402,
            "truncated": False,
            "content": "first line\r\nsecond line\r\n",
        },
    )

    summary = _observation_summary(result)
    assert "start_line=401 end_line=402 truncated=false" in summary
    assert "visible_start_line=401 visible_end_line=402" in summary
    assert "observation_truncated=false next_start_line=None" in summary
    assert summary.endswith("first line\r\nsecond line\r\n")


def test_read_file_observation_is_contiguous_bounded_and_pageable() -> None:
    content = "".join(f"line {i}: {'x' * 80}\r\n" for i in range(401, 501))
    result = _tool_result(
        str(uuid4()),
        "read_file",
        success=True,
        output={
            "path": "pvlib/iam.py",
            "start_line": 401,
            "end_line": 500,
            "truncated": True,
            "content": content,
        },
    )

    summary = _observation_summary(result)
    header, visible = summary.split(":\n", 1)
    shown = visible.splitlines()
    assert len(summary) <= 4096
    assert "observation_truncated=true" in header
    assert "line_content_truncated=false continuation_unavailable=false" in header
    assert f"visible_end_line={400 + len(shown)}" in header
    assert f"next_start_line={401 + len(shown)}" in header
    assert shown == content.splitlines()[:len(shown)]
    assert "\r\n" in visible
    assert "line 500:" not in visible


def test_search_files_observation_preserves_returned_paths() -> None:
    result = _tool_result(
        str(uuid4()), "search_files", success=True,
        output={"paths": ["pvlib/iam.py"], "truncated": False},
    )
    summary = _observation_summary(result)
    assert "pvlib/iam.py" in summary
    assert "truncated=false" in summary
    assert "observation_truncated=false" in summary


class Explorer:
    def __init__(self, status_text: str = "") -> None:
        self.status_text = status_text

    async def explore(self, request: ExplorationRequest) -> ExplorationResult:
        status = _tool_result(
            str(uuid4()),
            "git_status",
            success=True,
            output={"stdout": self.status_text, "truncated": False},
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
            CompletionRequirement.WORKSPACE_CHANGE_REQUIRED,
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
            CompletionRequirement.WORKSPACE_CHANGE_NOT_REQUIRED,
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
        self.mutated = False

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
        if result.success and invocation.tool_name in {
            "apply_patch", "edit_file", "write_file", "shell",
        }:
            self.mutated = True
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
    async def create_plan(self, request: PlanningRequest) -> Plan:
        plan = await super().create_plan(request)
        return replace(
            plan,
            completion_requirement=CompletionRequirement.WORKSPACE_CHANGE_NOT_REQUIRED,
        )

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
    def __init__(self, tool_runtime: OneActionRuntime, *, drift_on: int | None = None) -> None:
        self.tool_runtime = tool_runtime
        self.drift_on = drift_on
        self.calls = 0

    async def collect(self, **kwargs: Any) -> FinalDiffEvidence:
        del kwargs
        self.calls += 1
        diff = "diff --git a/alpha.py b/alpha.py\n" if self.tool_runtime.mutated else ""
        if self.drift_on is not None and self.calls >= self.drift_on:
            diff += "+drift\n"
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
    model_call_id: Callable[[], str] | None = None,
    normalize_argv: Callable[[list[str]], list[str]] | None = None,
    diff_collector: DiffCollector | None = None,
    explorer: Explorer | None = None,
) -> Day4LangGraphRuntime:
    return Day4LangGraphRuntime(
        explorer=cast(RepositoryExplorer, explorer or Explorer()),
        context_builder=cast(ContextBuilder, Builder()),
        planner=planner,
        agent=agent,
        tool_runtime=cast(ToolRuntime, tool_runtime),
        validation_planner=cast(ValidationPlanner, PassValidationPlanner()),
        validation_runner=validation_runner or cast(
            ValidationRunner, PassValidationRunner()
        ),
        plan_approval_service=cast(PlanApprovalService, AutoPlanApprovals()),
        diff_collector=cast(FinalDiffCollector, diff_collector or DiffCollector(tool_runtime)),
        event_buffer=event_buffer,
        ledger=ledger,
        approval_mode=ApprovalMode.AUTO,
        max_steps=5,
        max_repair_attempts=3,
        max_replans=max_replans,
        checkpointer=checkpointer,
        model_call_id=model_call_id,
        normalize_argv=normalize_argv,
    )


def _state_with_completed_read() -> AgentState:
    invocation_id = str(uuid4())
    result = _tool_result(
        invocation_id, "read_file", success=True,
        output={
            "path": "alpha.py", "start_line": 1, "end_line": 400,
            "truncated": True, "content": "PRIVATE_FILE_CONTENT",
        },
    )
    observation = Observation(
        invocation_id, "read_file", True, "read_file alpha.py: evidence",
        None, None, "alpha.py",
    )
    return AgentState(
        "edit alpha", [ModelMessage("user", "edit alpha")],
        str(uuid4()), str(uuid4()), RuntimeStatus.STARTED,
        observations=(observation,), tool_results=(result,),
    )


@pytest.mark.asyncio
async def test_agent_step_preserves_proposed_edit_summary() -> None:
    class ProposeEdit:
        async def decide(self, request: AgentDecisionRequest) -> AgentDecision:
            del request
            return AgentDecision(
                AgentDecisionKind.TOOL_ACTION,
                ToolAction(
                    "edit_file",
                    {"path": "alpha.py", "old_str": "old", "new_str": "new"},
                ),
                "Apply the exact edit now.",
            )

    run_id, session_id = str(uuid4()), str(uuid4())
    step = PlanStep(
        str(uuid4()), 1, "Edit alpha", "edit_file", ("alpha.py",),
        None, None, PlanStepStatus.PENDING,
    )
    scope = derive_authorization_scope((step,))
    plan_id = str(uuid4())
    plan = Plan(
        plan_id, run_id, session_id, 1, PlanKind.INITIAL,
        PlanStatus.CREATED, ApprovalDecision.PENDING, (step,), scope,
        "Approved edit.", None, None,
        compute_scope_digest(plan_id, 1, scope), datetime.now(UTC), None,
        CompletionRequirement.WORKSPACE_CHANGE_REQUIRED,
    )
    context = WorkingContext("edit alpha", (), (), ("alpha.py",), (), False)
    state = AgentState(
        "edit alpha", [ModelMessage("user", "edit alpha")],
        run_id, session_id, RuntimeStatus.STARTED, context=context, plan=plan,
    )
    ledger = ToolExecutionLedger()
    events = RuntimeEventBuffer()
    runtime = _runtime(
        ledger=ledger,
        event_buffer=events,
        planner=FixedPlanner(),
        agent=ProposeEdit(),
        tool_runtime=OneActionRuntime(ledger),
        model_call_id=lambda: str(uuid4()),
    )

    await runtime._agent_step(state)  # noqa: SLF001
    completed = [
        event for event in events.drain(run_id)
        if isinstance(event, AgentStepCompleted)
    ]
    assert len(completed) == 1
    assert completed[0].decision_kind is AgentDecisionKind.TOOL_ACTION
    assert completed[0].guard_reason is None
    assert completed[0].decision_summary == "Apply the exact edit now."


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
    assert final.diff == ""


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


def _progress_plan() -> Plan:
    plan_id, run_id, session_id = str(uuid4()), str(uuid4()), str(uuid4())
    steps = (
        PlanStep(str(uuid4()), 1, "Read iam.py", "read_file", (), None, None,
                 PlanStepStatus.PENDING),
        PlanStep(str(uuid4()), 2, "Read test_iam.py", "read_file", (), None, None,
                 PlanStepStatus.PENDING),
        PlanStep(str(uuid4()), 3, "Edit iam.py", "edit_file", ("iam.py",), None,
                 None, PlanStepStatus.PENDING),
        PlanStep(str(uuid4()), 4, "Validate", "shell", (), ("pytest", "-q"),
                 ".", PlanStepStatus.PENDING),
    )
    scope = derive_authorization_scope(steps)
    return Plan(
        plan_id, run_id, session_id, 1, PlanKind.INITIAL, PlanStatus.CREATED,
        ApprovalDecision.PENDING, steps, scope, "Repair and validate.", None,
        None, compute_scope_digest(plan_id, 1, scope), datetime.now(UTC), None,
        CompletionRequirement.WORKSPACE_CHANGE_REQUIRED,
    )


def _active_progress_plan(*statuses: PlanStepStatus) -> Plan:
    plan = _progress_plan()
    assert len(statuses) == len(plan.steps)
    return replace(
        plan,
        status=PlanStatus.ACTIVE,
        approval_status=ApprovalDecision.APPROVED,
        approval_id=str(uuid4()),
        approved_at=datetime.now(UTC),
        steps=tuple(
            replace(step, status=status)
            for step, status in zip(plan.steps, statuses, strict=True)
        ),
    )


@pytest.mark.asyncio
async def test_completed_plan_step_survives_checkpoint_and_resume() -> None:
    ledger = ToolExecutionLedger()
    runtime = _runtime(
        ledger=ledger, event_buffer=RuntimeEventBuffer(), planner=FixedPlanner(),
        agent=Decisions(), tool_runtime=OneActionRuntime(ledger),
        checkpointer=InMemorySaver(),
    )
    state = AgentState(
        "edit alpha", [ModelMessage("user", "edit alpha")],
        str(uuid4()), str(uuid4()), RuntimeStatus.STARTED,
    )
    thread_id = f"nexus-run:{state.run_id}"
    completed = await runtime.run(state, thread_id=thread_id)
    assert completed.plan is not None
    assert completed.plan.steps[0].status is PlanStepStatus.COMPLETED
    snapshot = await runtime._graph.aget_state(
        {"configurable": {"thread_id": thread_id}}
    )
    assert snapshot.values["plan"].steps[0].status is PlanStepStatus.COMPLETED
    resumed = await runtime.resume(thread_id=thread_id)
    assert resumed.plan is not None
    assert resumed.plan.steps[0].status is PlanStepStatus.COMPLETED
    assert resumed.active_step_started_observation_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("repeat_after_feedback", [False, True])
@pytest.mark.parametrize("error_code", ["PERMISSION_DENIED", "COMMAND_DENIED"])
async def test_deterministic_policy_denial_gets_one_semantic_retry_without_execution(
    repeat_after_feedback: bool, error_code: str,
) -> None:
    class DenialDecisions:
        def __init__(self) -> None:
            self.requests: list[AgentDecisionRequest] = []

        async def decide(self, request: AgentDecisionRequest) -> AgentDecision:
            self.requests.append(request)
            if len(self.requests) == 1 or repeat_after_feedback:
                return AgentDecision(AgentDecisionKind.TOOL_ACTION, action,
                                     "Try the denied command.")
            return AgentDecision(AgentDecisionKind.TASK_READY, None,
                                 "Use another approved path.")

    plan = _progress_plan()
    action = ToolAction("shell", {"argv": ["python", "-m", "pip"], "cwd": "."})
    denied = _tool_result(
        str(uuid4()), "shell", success=False, error_code=error_code,
    )
    state = AgentState(
        "repair", [ModelMessage("user", "repair")], plan.run_id,
        plan.session_id, RuntimeStatus.STARTED,
        context=WorkingContext("repair", (), (), (), (), False), plan=plan,
        pending_tool_action=action, latest_tool_result=denied,
    )
    agent = DenialDecisions()
    ledger, events = ToolExecutionLedger(), RuntimeEventBuffer()
    runtime = _runtime(
        ledger=ledger, event_buffer=events, planner=FixedPlanner(),
        agent=agent, tool_runtime=OneActionRuntime(ledger),
    )
    observed = await runtime._observe(state)
    update = observed.update
    assert isinstance(update, dict)
    next_state = replace(state, **update)
    assert next_state.last_policy_denial_fingerprint == _action_fingerprint(
        action, lambda argv: argv,
    )
    if repeat_after_feedback:
        with pytest.raises(ModelError, match="deterministically denied") as failure:
            await runtime._agent_step(next_state)
        assert failure.value.code == "INVALID_AGENT_DECISION"
    else:
        command = await runtime._agent_step(next_state)
        assert command.goto == "validate"
    assert len(agent.requests) == 2
    assert agent.requests[0].runtime_feedback is None
    assert agent.requests[1].runtime_feedback is AgentRuntimeFeedback.REPEATED_POLICY_DENIAL
    assert agent.requests[1].context.compacted_observations is None
    assert ledger.count(plan.run_id) == 0
    emitted = events.drain(plan.run_id)
    assert sum(isinstance(item, AgentSemanticRetryStarted) for item in emitted) == 1


def test_policy_denial_fingerprint_uses_normalized_shell_argv() -> None:
    def normalize(argv: list[str]) -> list[str]:
        return ["C:/trusted/python.exe" if argv[0] == "python" else argv[0],
                *argv[1:]]
    alias = ToolAction("shell", {"argv": ["python", "-m", "pip"]})
    absolute = ToolAction("shell", {"argv": ["C:/trusted/python.exe", "-m", "pip"],
                                    "cwd": "."})
    different = ToolAction("shell", {"argv": ["python", "-m", "venv"]})
    assert _action_fingerprint(alias, normalize) == _action_fingerprint(absolute, normalize)
    assert _action_fingerprint(alias, normalize) != _action_fingerprint(different, normalize)


@pytest.mark.asyncio
async def test_normal_runtime_executes_repeated_successful_reads_without_semantic_retry() -> None:
    class RepeatRead:
        def __init__(self) -> None:
            self.requests: list[AgentDecisionRequest] = []

        async def decide(self, request: AgentDecisionRequest) -> AgentDecision:
            self.requests.append(request)
            return AgentDecision(
                AgentDecisionKind.TOOL_ACTION,
                ToolAction("read_file", {
                    "path": "alpha.py", "start_line": 1, "max_lines": 400,
                }),
                "Read alpha again.",
            )

    state = _state_with_completed_read()
    plan = _progress_plan()
    authorization = ApprovedPlanEvidence(
        plan.plan_id, plan.version, plan.run_id, plan.session_id,
        PlanAuthorizationSource.INTERACTIVE, str(uuid4()), plan.scope_digest,
        plan.authorization_scope, datetime.now(UTC),
    )
    state = replace(
        state, run_id=plan.run_id, session_id=plan.session_id,
        context=WorkingContext("inspect", (), (), (), (), False),
        plan=plan, approved_plan=authorization,
    )
    ledger, events = ToolExecutionLedger(), RuntimeEventBuffer()
    agent = RepeatRead()
    runtime = _runtime(
        ledger=ledger, event_buffer=events, planner=FixedPlanner(),
        agent=agent, tool_runtime=OneActionRuntime(ledger),
    )
    for _ in range(2):
        chosen = await runtime._agent_step(state)
        assert chosen.goto == "execute_tool"
        assert isinstance(chosen.update, dict)
        state = replace(state, **chosen.update)
        executed = await runtime._execute_tool(state)
        state = replace(state, **executed)
        observed = await runtime._observe(state)
        assert isinstance(observed.update, dict)
        state = replace(state, **observed.update)
    assert ledger.count(state.run_id) == 2
    assert len(agent.requests) == 2
    assert all(request.runtime_feedback is None for request in agent.requests)
    assert not any(isinstance(item, AgentSemanticRetryStarted)
                   for item in events.drain(state.run_id))


@pytest.mark.asyncio
async def test_normal_runtime_executes_two_real_edits_to_same_file(tmp_path: Path) -> None:
    (tmp_path / "iam.py").write_text("A C\n", encoding="utf-8")
    plan = _progress_plan()
    authorization = ApprovedPlanEvidence(
        plan.plan_id, plan.version, plan.run_id, plan.session_id,
        PlanAuthorizationSource.INTERACTIVE, str(uuid4()), plan.scope_digest,
        plan.authorization_scope, datetime.now(UTC),
    )
    actions = (
        ToolAction("edit_file", {"path": "iam.py", "old_str": "A", "new_str": "B"}),
        ToolAction("edit_file", {"path": "iam.py", "old_str": "C", "new_str": "D"}),
    )

    class TwoEdits:
        def __init__(self) -> None:
            self.calls = 0

        async def decide(self, request: AgentDecisionRequest) -> AgentDecision:
            del request
            action = actions[self.calls]
            self.calls += 1
            return AgentDecision(AgentDecisionKind.TOOL_ACTION, action, "Edit iam.py.")

    class RealEditorRuntime(OneActionRuntime):
        def __init__(self, ledger: ToolExecutionLedger) -> None:
            super().__init__(ledger)
            self.editor = EditFileTool(WorkspaceGuard(tmp_path))

        async def execute(
            self, invocation: ToolInvocation, *,
            authorization: ApprovedPlanEvidence | None = None,
        ) -> ToolResult:
            assert authorization is not None
            assert authorization.authorization_scope == plan.authorization_scope
            self.ledger.begin(invocation)
            result = await self.editor.execute(invocation)
            self.ledger.record(invocation, result)
            return result

    ledger, events = ToolExecutionLedger(), RuntimeEventBuffer()
    runtime = _runtime(
        ledger=ledger, event_buffer=events, planner=FixedPlanner(),
        agent=TwoEdits(), tool_runtime=RealEditorRuntime(ledger),
    )
    state = AgentState(
        "edit iam", [ModelMessage("user", "edit iam")], plan.run_id,
        plan.session_id, RuntimeStatus.STARTED,
        context=WorkingContext("edit iam", (), (), (), (), False),
        plan=plan, approved_plan=authorization,
    )
    for expected in actions:
        chosen = await runtime._agent_step(state)
        assert chosen.goto == "execute_tool"
        assert isinstance(chosen.update, dict)
        assert chosen.update["pending_tool_action"] == expected
        state = replace(state, **chosen.update)
        executed = await runtime._execute_tool(state)
        assert isinstance(executed["latest_tool_result"], ToolResult)
        assert executed["latest_tool_result"].success
        state = replace(state, **executed)
        observed = await runtime._observe(state)
        assert isinstance(observed.update, dict)
        assert "plan" not in observed.update
        state = replace(state, **observed.update)
    assert (tmp_path / "iam.py").read_text(encoding="utf-8") == "B D\n"
    assert ledger.count(plan.run_id) == 2
    assert all(step.status is PlanStepStatus.PENDING for step in state.plan.steps)
    assert not any(isinstance(item, PlanStepCompleted) for item in events.drain(plan.run_id))


@pytest.mark.asyncio
async def test_normal_runtime_preserves_edit_decision_without_fresh_read() -> None:
    plan = _progress_plan()
    action = ToolAction("edit_file", {
        "path": "iam.py", "old_str": "old", "new_str": "new",
    })

    class EditDecision:
        async def decide(self, request: AgentDecisionRequest) -> AgentDecision:
            del request
            return AgentDecision(AgentDecisionKind.TOOL_ACTION, action, "Edit iam.py.")

    ledger, events = ToolExecutionLedger(), RuntimeEventBuffer()
    runtime = _runtime(
        ledger=ledger, event_buffer=events, planner=FixedPlanner(),
        agent=EditDecision(), tool_runtime=OneActionRuntime(ledger),
    )
    state = AgentState(
        "edit iam", [], plan.run_id, plan.session_id, RuntimeStatus.STARTED,
        context=WorkingContext("edit iam", (), (), (), (), False), plan=plan,
    )
    chosen = await runtime._agent_step(state)
    assert chosen.goto == "execute_tool"
    assert isinstance(chosen.update, dict)
    assert chosen.update["pending_tool_action"] == action
    assert all(not isinstance(item, AgentSemanticRetryStarted)
                   for item in events.drain(plan.run_id))


@pytest.mark.asyncio
async def test_read_observation_keeps_active_step_in_progress() -> None:
    plan = _active_progress_plan(
        PlanStepStatus.IN_PROGRESS,
        PlanStepStatus.PENDING,
        PlanStepStatus.PENDING,
        PlanStepStatus.PENDING,
    )
    result = _tool_result(
        str(uuid4()), "read_file", success=True,
        output={"path": "iam.py", "start_line": 1, "end_line": 2,
                "truncated": False, "content": "A C\n"},
    )
    state = AgentState(
        "inspect", [], plan.run_id, plan.session_id, RuntimeStatus.STARTED,
        plan=plan, pending_tool_action=ToolAction("read_file", {"path": "iam.py"}),
        latest_tool_result=result,
    )
    events = RuntimeEventBuffer()
    runtime = _runtime(
        ledger=ToolExecutionLedger(), event_buffer=events,
        planner=FixedPlanner(), agent=ReadyDecisions(),
        tool_runtime=OneActionRuntime(ToolExecutionLedger()),
    )
    observed = await runtime._observe(state)
    assert isinstance(observed.update, dict)
    assert "plan" not in observed.update
    assert state.plan is plan
    assert plan.steps[0].status is PlanStepStatus.IN_PROGRESS
    assert not any(isinstance(item, PlanStepCompleted) for item in events.drain(plan.run_id))


def test_agent_prompt_exposes_authoritative_plan_progress() -> None:
    from nexus.application.planning import _agent_messages

    plan = _active_progress_plan(
        PlanStepStatus.COMPLETED,
        PlanStepStatus.IN_PROGRESS,
        PlanStepStatus.PENDING,
        PlanStepStatus.PENDING,
    )
    request = AgentDecisionRequest(
        "edit iam", WorkingContext("edit iam", (), (), (), (), False),
        plan, (),
    )
    system, user = _agent_messages(request)
    payload = json.loads(user.content)
    assert "execution guidance" in system.content
    assert "Follow approved Plan progress" not in system.content
    assert "Never repeat an approved edit" not in system.content
    assert payload["plan"]["active_step"]["step_id"] == plan.steps[1].step_id
    assert payload["plan"]["active_step"]["sequence"] == 2
    assert payload["plan"]["completed_step_ids"] == [plan.steps[0].step_id]
    assert [step["status"] for step in payload["plan"]["steps"]] == [
        "COMPLETED", "IN_PROGRESS", "PENDING", "PENDING",
    ]
    assert "Work primarily on its current IN_PROGRESS step" in system.content
    assert "CONTINUE cannot claim their completion" in system.content
    assert payload["plan"]["steps"][2]["target_paths"] == ["iam.py"]


@pytest.mark.asyncio
async def test_finalize_rejects_workspace_drift_after_validation() -> None:
    ledger = ToolExecutionLedger()
    tool_runtime = OneActionRuntime(ledger)
    collector = DiffCollector(tool_runtime, drift_on=4)
    runtime = _runtime(
        ledger=ledger,
        event_buffer=RuntimeEventBuffer(),
        planner=FixedPlanner(),
        agent=Decisions(),
        tool_runtime=tool_runtime,
        diff_collector=collector,
    )
    state = AgentState(
        "edit alpha",
        [ModelMessage(role="user", content="edit alpha")],
        str(uuid4()),
        str(uuid4()),
        RuntimeStatus.STARTED,
    )
    result = await runtime.run(state)
    assert collector.calls == 4
    assert result.terminal_status is TerminalStatus.FAILED


@pytest.mark.asyncio
async def test_read_only_completion_keeps_preexisting_patch_flag() -> None:
    class PreexistingDiffCollector:
        async def collect(self, **kwargs: Any) -> FinalDiffEvidence:
            del kwargs
            return FinalDiffEvidence("diff --git a/preexisting.txt b/preexisting.txt\n", False, ())

    ledger = ToolExecutionLedger()
    events = RuntimeEventBuffer()
    runtime = _runtime(
        ledger=ledger,
        event_buffer=events,
        planner=ReadOnlyPlanner(),
        agent=GroundedReadOnlyDecisions(),
        tool_runtime=OneActionRuntime(ledger),
        diff_collector=cast(DiffCollector, PreexistingDiffCollector()),
        explorer=Explorer("?? preexisting.txt\n"),
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
    assert final.includes_preexisting_changes is True
    assert final.diff == ""


@pytest.mark.asyncio
async def test_legacy_unspecified_plan_cannot_complete_as_read_only() -> None:
    class LegacyPlanner(ReadOnlyPlanner):
        async def create_plan(self, request: PlanningRequest) -> Plan:
            current = await super().create_plan(request)
            return replace(
                current, completion_requirement=CompletionRequirement.UNSPECIFIED
            )

    ledger = ToolExecutionLedger()
    runtime = _runtime(
        ledger=ledger,
        event_buffer=RuntimeEventBuffer(),
        planner=LegacyPlanner(),
        agent=GroundedReadOnlyDecisions(),
        tool_runtime=OneActionRuntime(ledger),
    )
    state = AgentState(
        "explain record parsing",
        [ModelMessage(role="user", content="explain record parsing")],
        str(uuid4()), str(uuid4()), RuntimeStatus.STARTED,
    )

    result = await runtime.run(state)
    assert result.terminal_status is TerminalStatus.FAILED_VALIDATION_UNKNOWN


@pytest.mark.asyncio
async def test_plan_creation_and_approval_activate_only_first_step() -> None:
    plan = await FixedPlanner().create_plan(PlanningRequest(
        "edit alpha", WorkingContext("edit alpha", (), (), (), (), False),
        PlanKind.INITIAL, None, None, str(uuid4()), str(uuid4()),
    ))
    assert all(step.status is PlanStepStatus.PENDING for step in plan.steps)
    plan = _progress_plan()
    assert all(step.status is PlanStepStatus.PENDING for step in plan.steps)

    ledger = ToolExecutionLedger()
    runtime = _runtime(
        ledger=ledger, event_buffer=RuntimeEventBuffer(),
        planner=FixedPlanner(), agent=ReadyDecisions(),
        tool_runtime=OneActionRuntime(ledger),
    )
    state = AgentState(
        "edit alpha", [], plan.run_id, plan.session_id, RuntimeStatus.STARTED,
        context=WorkingContext("edit alpha", (), (), (), (), False),
        plan=plan, baseline_workspace_digest="baseline",
    )
    approved = await runtime._approval_gate(state)
    assert isinstance(approved.update, dict)
    active = approved.update["plan"]
    assert isinstance(active, Plan)
    assert [step.status for step in active.steps] == [
        PlanStepStatus.IN_PROGRESS, PlanStepStatus.PENDING,
        PlanStepStatus.PENDING, PlanStepStatus.PENDING,
    ]
    assert active.approval_id is not None
    assert approved.update["active_step_started_observation_count"] == 0
    assert active.authorization_scope == plan.authorization_scope
    assert active.scope_digest == plan.scope_digest


@pytest.mark.asyncio
async def test_read_pages_wait_for_continue_and_empty_continue_retries() -> None:
    plan = _active_progress_plan(
        PlanStepStatus.IN_PROGRESS, PlanStepStatus.PENDING,
        PlanStepStatus.PENDING, PlanStepStatus.PENDING,
    )
    ledger, events = ToolExecutionLedger(), RuntimeEventBuffer()

    class ContinueAgent:
        def __init__(self) -> None:
            self.requests: list[AgentDecisionRequest] = []

        async def decide(self, request: AgentDecisionRequest) -> AgentDecision:
            self.requests.append(request)
            return AgentDecision(AgentDecisionKind.CONTINUE, None, "Inspection done.")

    agent = ContinueAgent()
    runtime = _runtime(
        ledger=ledger, event_buffer=events, planner=FixedPlanner(),
        agent=agent, tool_runtime=OneActionRuntime(ledger),
    )
    state = AgentState(
        "inspect", [], plan.run_id, plan.session_id, RuntimeStatus.STARTED,
        context=WorkingContext("inspect", (), (), (), (), False), plan=plan,
    )
    for page in range(3):
        result = _tool_result(
            str(uuid4()), "read_file", success=True,
            output={
                "path": "iam.py", "start_line": page + 1,
                "end_line": page + 1, "truncated": False,
                "content": f"page {page}\n",
            },
        )
        observed = await runtime._observe(replace(
            state, pending_tool_action=ToolAction("read_file", {"path": "iam.py"}),
            latest_tool_result=result,
        ))
        assert isinstance(observed.update, dict)
        assert "plan" not in observed.update
        state = replace(state, **observed.update)
        assert state.plan is not None
        assert state.plan.steps[0].status is PlanStepStatus.IN_PROGRESS

    advanced = await runtime._agent_step(state)
    assert isinstance(advanced.update, dict)
    state = replace(state, **advanced.update)
    assert state.plan is not None
    assert [step.status for step in state.plan.steps[:2]] == [
        PlanStepStatus.COMPLETED, PlanStepStatus.IN_PROGRESS,
    ]
    assert state.active_step_started_observation_count == 3
    completed = [item for item in events.drain(plan.run_id)
                 if isinstance(item, PlanStepCompleted)]
    assert len(completed) == 1 and completed[0].step_id == plan.steps[0].step_id
    with pytest.raises(ModelError) as failure:
        await runtime._agent_step(state)
    assert failure.value.code == "INVALID_AGENT_DECISION"
    assert len(agent.requests) == 3
    assert agent.requests[-1].runtime_feedback is AgentRuntimeFeedback.INVALID_PLAN_CONTINUE
    assert state.plan.steps[1].status is PlanStepStatus.IN_PROGRESS


@pytest.mark.asyncio
async def test_edit_progress_requires_matching_success_and_auxiliary_read_does_not_reopen() -> None:
    plan = _active_progress_plan(
        PlanStepStatus.COMPLETED, PlanStepStatus.COMPLETED,
        PlanStepStatus.IN_PROGRESS, PlanStepStatus.PENDING,
    )
    ledger, events = ToolExecutionLedger(), RuntimeEventBuffer()
    runtime = _runtime(
        ledger=ledger, event_buffer=events, planner=FixedPlanner(),
        agent=ReadyDecisions(), tool_runtime=OneActionRuntime(ledger),
    )
    state = AgentState(
        "edit", [], plan.run_id, plan.session_id, RuntimeStatus.STARTED,
        plan=plan,
    )

    async def observe(action: ToolAction, result: ToolResult) -> None:
        nonlocal state
        transition = await runtime._observe(replace(
            state, pending_tool_action=action, latest_tool_result=result,
        ))
        assert isinstance(transition.update, dict)
        state = replace(state, **transition.update)

    await observe(
        ToolAction("read_file", {"path": "iam.py"}),
        _tool_result(str(uuid4()), "read_file", success=True, output={
            "path": "iam.py", "start_line": 1, "end_line": 1,
            "truncated": False, "content": "old\n",
        }),
    )
    await observe(
        ToolAction("edit_file", {"path": "iam.py"}),
        _tool_result(str(uuid4()), "edit_file", success=False,
                     error_code="EDIT_TARGET_NOT_FOUND"),
    )
    await observe(
        ToolAction("edit_file", {"path": "other.py"}),
        _tool_result(str(uuid4()), "edit_file", success=True,
                     output={"path": "other.py"}),
    )
    assert state.plan is not None
    assert [step.status for step in state.plan.steps] == [
        PlanStepStatus.COMPLETED, PlanStepStatus.COMPLETED,
        PlanStepStatus.IN_PROGRESS, PlanStepStatus.PENDING,
    ]
    await observe(
        ToolAction("edit_file", {"path": "iam.py"}),
        _tool_result(str(uuid4()), "edit_file", success=True,
                     output={"path": "iam.py"}),
    )
    assert state.plan is not None
    assert [step.status for step in state.plan.steps] == [
        PlanStepStatus.COMPLETED, PlanStepStatus.COMPLETED,
        PlanStepStatus.COMPLETED, PlanStepStatus.IN_PROGRESS,
    ]
    assert state.active_step_started_observation_count == 4
    completed = [item for item in events.drain(plan.run_id)
                 if isinstance(item, PlanStepCompleted)]
    assert len(completed) == 1 and completed[0].step_id == plan.steps[2].step_id


@pytest.mark.asyncio
async def test_shell_progress_requires_exact_approved_command_and_success() -> None:
    plan = _active_progress_plan(
        PlanStepStatus.COMPLETED, PlanStepStatus.COMPLETED,
        PlanStepStatus.COMPLETED, PlanStepStatus.IN_PROGRESS,
    )
    ledger = ToolExecutionLedger()
    runtime = _runtime(
        ledger=ledger, event_buffer=RuntimeEventBuffer(),
        planner=FixedPlanner(), agent=ReadyDecisions(),
        tool_runtime=OneActionRuntime(ledger),
    )
    state = AgentState(
        "test", [], plan.run_id, plan.session_id, RuntimeStatus.STARTED,
        plan=plan,
    )
    for action, result in (
        (
            ToolAction("shell", {"argv": ["pytest", "-x"], "cwd": "."}),
            _tool_result(str(uuid4()), "shell", success=True),
        ),
        (
            ToolAction("shell", {"argv": ["pytest", "-q"], "cwd": "subdir"}),
            _tool_result(str(uuid4()), "shell", success=True),
        ),
        (
            ToolAction("shell", {"argv": ["pytest", "-q"]}),
            _tool_result(str(uuid4()), "shell", success=False,
                         error_code="COMMAND_EXIT_NONZERO"),
        ),
    ):
        observed = await runtime._observe(replace(
            state, pending_tool_action=action, latest_tool_result=result,
        ))
        assert isinstance(observed.update, dict)
        assert "plan" not in observed.update
        state = replace(state, **observed.update)
    observed = await runtime._observe(replace(
        state, pending_tool_action=ToolAction("shell", {"argv": ["pytest", "-q"]}),
        latest_tool_result=_tool_result(str(uuid4()), "shell", success=True),
    ))
    assert isinstance(observed.update, dict)
    advanced = observed.update["plan"]
    assert isinstance(advanced, Plan)
    assert advanced.steps[-1].status is PlanStepStatus.COMPLETED


def test_progress_transitions_preserve_authorization_and_approval_identity() -> None:
    created = _progress_plan()
    active = _activate_first_plan_step(replace(
        created, status=PlanStatus.ACTIVE,
        approval_status=ApprovalDecision.APPROVED,
        approval_id=str(uuid4()), approved_at=datetime.now(UTC),
    ))
    evidence = ApprovedPlanEvidence(
        active.plan_id, active.version, active.run_id, active.session_id,
        PlanAuthorizationSource.AUTO_MODE, cast(str, active.approval_id),
        active.scope_digest, active.authorization_scope,
        cast(datetime, active.approved_at),
    )
    advanced, completed = _complete_active_plan_step(active)
    assert completed.step_id == created.steps[0].step_id
    assert advanced.steps[0].status is PlanStepStatus.COMPLETED
    assert advanced.steps[1].status is PlanStepStatus.IN_PROGRESS
    assert (
        advanced.plan_id, advanced.version, advanced.authorization_scope,
        advanced.scope_digest, advanced.approval_id, advanced.approved_at,
    ) == (
        active.plan_id, active.version, active.authorization_scope,
        active.scope_digest, active.approval_id, active.approved_at,
    )
    assert evidence.scope_digest == advanced.scope_digest
    assert evidence.authorization_scope == advanced.authorization_scope


@pytest.mark.asyncio
async def test_replan_history_keeps_old_progress_and_activates_new_version() -> None:
    plan = _active_progress_plan(
        PlanStepStatus.COMPLETED, PlanStepStatus.COMPLETED,
        PlanStepStatus.IN_PROGRESS, PlanStepStatus.PENDING,
    )
    denial = Observation(
        str(uuid4()), "edit_file", False, "scope denied",
        "PLAN_SCOPE_DENIED", "Approved scope is insufficient.",
    )
    ledger = ToolExecutionLedger()
    runtime = _runtime(
        ledger=ledger, event_buffer=RuntimeEventBuffer(),
        planner=FixedPlanner(), agent=ReadyDecisions(),
        tool_runtime=OneActionRuntime(ledger),
    )
    state = AgentState(
        "edit", [], plan.run_id, plan.session_id, RuntimeStatus.STARTED,
        context=WorkingContext("edit", (), (), (), (), False),
        plan=plan, observations=(denial,), baseline_workspace_digest="baseline",
    )
    created = await runtime._create_plan(state)
    old = created["plan_history"][-1]
    new = created["plan"]
    assert isinstance(old, Plan) and isinstance(new, Plan)
    assert old.status is PlanStatus.SUPERSEDED
    assert [step.status for step in old.steps] == [
        PlanStepStatus.COMPLETED, PlanStepStatus.COMPLETED,
        PlanStepStatus.IN_PROGRESS, PlanStepStatus.PENDING,
    ]
    assert new.version == plan.version + 1
    assert all(step.status is PlanStepStatus.PENDING for step in new.steps)
    approved = await runtime._approval_gate(replace(state, **created))
    assert isinstance(approved.update, dict)
    replacement = approved.update["plan"]
    assert isinstance(replacement, Plan)
    assert replacement.steps[0].status is PlanStepStatus.IN_PROGRESS


@pytest.mark.asyncio
@pytest.mark.parametrize("active_sequence", [3, 4])
async def test_continue_cannot_complete_edit_or_shell_step(active_sequence: int) -> None:
    statuses = [PlanStepStatus.COMPLETED] * (active_sequence - 1)
    statuses.append(PlanStepStatus.IN_PROGRESS)
    statuses.extend([PlanStepStatus.PENDING] * (4 - active_sequence))
    plan = _active_progress_plan(*statuses)

    class ContinueAgent:
        async def decide(self, request: AgentDecisionRequest) -> AgentDecision:
            del request
            return AgentDecision(AgentDecisionKind.CONTINUE, None, "Skip.")

    ledger = ToolExecutionLedger()
    runtime = _runtime(
        ledger=ledger, event_buffer=RuntimeEventBuffer(), planner=FixedPlanner(),
        agent=ContinueAgent(), tool_runtime=OneActionRuntime(ledger),
    )
    state = AgentState(
        "work", [], plan.run_id, plan.session_id, RuntimeStatus.STARTED,
        context=WorkingContext("work", (), (), (), (), False),
        plan=plan,
        observations=(Observation(
            str(uuid4()), "read_file", True, "fresh read", None, None,
        ),),
    )
    with pytest.raises(ModelError) as failure:
        await runtime._agent_step(state)
    assert failure.value.code == "INVALID_AGENT_DECISION"
    assert plan.steps[active_sequence - 1].status is PlanStepStatus.IN_PROGRESS


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("step_tool", "evidence_tool", "success"),
    [
        ("search_files", "search_files", True),
        (None, "write_file", False),
    ],
)
async def test_inspection_and_narrative_continue_need_new_tool_evidence(
    step_tool: str | None, evidence_tool: str, success: bool,
) -> None:
    plan = _active_progress_plan(
        PlanStepStatus.IN_PROGRESS, PlanStepStatus.PENDING,
        PlanStepStatus.PENDING, PlanStepStatus.PENDING,
    )
    plan = replace(
        plan,
        steps=(replace(plan.steps[0], tool_name=step_tool), *plan.steps[1:]),
    )

    class ContinueAgent:
        async def decide(self, request: AgentDecisionRequest) -> AgentDecision:
            del request
            return AgentDecision(AgentDecisionKind.CONTINUE, None, "Step done.")

    ledger = ToolExecutionLedger()
    runtime = _runtime(
        ledger=ledger, event_buffer=RuntimeEventBuffer(), planner=FixedPlanner(),
        agent=ContinueAgent(), tool_runtime=OneActionRuntime(ledger),
    )
    state = AgentState(
        "inspect", [], plan.run_id, plan.session_id, RuntimeStatus.STARTED,
        context=WorkingContext("inspect", (), (), (), (), False), plan=plan,
        observations=(Observation(
            str(uuid4()), evidence_tool, success, "new Tool evidence", None, None,
        ),),
    )
    decision = await runtime._agent_step(state)
    assert isinstance(decision.update, dict)
    advanced = decision.update["plan"]
    assert isinstance(advanced, Plan)
    assert [step.status for step in advanced.steps[:2]] == [
        PlanStepStatus.COMPLETED, PlanStepStatus.IN_PROGRESS,
    ]
