from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest

from nexus.application.approval_service import ApprovalService
from nexus.application.plan_approval_service import PlanApprovalService
from nexus.application.planning import JsonAgentDecisionAdapter, ModelPlanner
from nexus.application.tool_runtime import ToolRuntime
from nexus.application.validation import ToolValidationRunner
from nexus.domain.agent_decision import AgentDecisionKind, AgentDecisionRequest, Observation
from nexus.domain.approvals import ApprovalRequest
from nexus.domain.exploration import WorkingContext
from nexus.domain.model import ModelChunk, ModelMessage, ModelResponse
from nexus.domain.planning import (
    ApprovedPlanEvidence,
    Plan,
    PlanAuthorizationSource,
    PlanKind,
    PlanStatus,
    PlanStep,
    PlanStepStatus,
    compute_scope_digest,
    derive_authorization_scope,
)
from nexus.domain.ports.planning import PlanningRequest
from nexus.domain.ports.tooling import ApprovalPolicy
from nexus.domain.tooling import (
    ApprovalDecision,
    JsonObject,
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
    aggregate_repairable,
)
from nexus.errors import ModelError, ValidationError
from nexus.security.workspace import WorkspaceGuard
from nexus.tools.editing import PatchTool, WriteFileTool
from nexus.tools.registry import ToolRegistry


class QueueGateway:
    def __init__(self, *responses: str) -> None:
        self.responses = list(responses)
        self.last_messages: tuple[ModelMessage, ...] = ()

    async def complete(self, messages: Sequence[ModelMessage]) -> ModelResponse:
        self.last_messages = tuple(messages)
        return ModelResponse(self.responses.pop(0))

    async def stream(
        self, messages: Sequence[ModelMessage]
    ) -> AsyncIterator[ModelChunk]:
        del messages
        if False:
            yield ModelChunk("")


def _context() -> WorkingContext:
    return WorkingContext("edit alpha", (), (), ("alpha.py",), (), False)


def _plan(*, run_id: str | None = None, session_id: str | None = None) -> Plan:
    plan_id = str(uuid4())
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
        PlanStep(
            str(uuid4()),
            2,
            "TEST: targeted test",
            "shell",
            (),
            ("pytest", "-q", "tests/test_alpha.py"),
            ".",
            PlanStepStatus.PENDING,
        ),
    )
    scope = derive_authorization_scope(steps)
    return Plan(
        plan_id,
        run_id or str(uuid4()),
        session_id or str(uuid4()),
        1,
        PlanKind.INITIAL,
        PlanStatus.CREATED,
        ApprovalDecision.PENDING,
        steps,
        scope,
        "Bounded edit and targeted validation.",
        None,
        None,
        compute_scope_digest(plan_id, 1, scope),
        datetime.now(UTC),
        None,
    )


def _evidence(plan: Plan) -> ApprovedPlanEvidence:
    return ApprovedPlanEvidence(
        plan.plan_id,
        plan.version,
        plan.run_id,
        plan.session_id,
        PlanAuthorizationSource.INTERACTIVE,
        str(uuid4()),
        plan.scope_digest,
        plan.authorization_scope,
        datetime.now(UTC),
    )


def _multi_edit_plan() -> Plan:
    plan = _plan()
    steps = (
        plan.steps[0],
        PlanStep(
            str(uuid4()),
            2,
            "Edit beta",
            "apply_patch",
            ("beta.py",),
            None,
            None,
            PlanStepStatus.PENDING,
        ),
        replace(plan.steps[1], sequence=3),
    )
    scope = derive_authorization_scope(steps)
    return replace(
        plan,
        steps=steps,
        authorization_scope=scope,
        scope_digest=compute_scope_digest(plan.plan_id, plan.version, scope),
    )


@pytest.mark.asyncio
async def test_planner_initial_and_replan_preserve_canonical_correlation() -> None:
    first = {
        "rationale_summary": "Initial bounded plan.",
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
    second = {
        "rationale_summary": "Replanned bounded strategy.",
        "steps": [
            {
                "description": "TEST: validate alpha",
                "tool_name": "shell",
                "target_paths": [],
                "command_argv": ["pytest", "-q"],
                "command_cwd": ".",
            }
        ],
    }
    planner = ModelPlanner(
        QueueGateway(json.dumps(first), json.dumps(second)),
    )
    run_id = str(uuid4())
    session_id = str(uuid4())

    initial = await planner.create_plan(
        PlanningRequest(
            "edit alpha",
            _context(),
            PlanKind.INITIAL,
            None,
            None,
            run_id,
            session_id,
        )
    )
    replanned = await planner.create_plan(
        PlanningRequest(
            "edit alpha",
            _context(),
            PlanKind.REPLAN,
            initial,
            "Approved scope is insufficient.",
            run_id,
            session_id,
        )
    )

    assert initial.run_id == replanned.run_id == run_id
    assert initial.session_id == replanned.session_id == session_id
    assert replanned.plan_id == initial.plan_id
    assert replanned.version == 2


def test_planning_request_rejects_replan_identity_change_and_noncanonical_uuid() -> None:
    previous = _plan()
    with pytest.raises(ValueError, match="cannot change"):
        PlanningRequest(
            "task",
            _context(),
            PlanKind.REPLAN,
            previous,
            "reason",
            str(uuid4()),
            previous.session_id,
        )
    with pytest.raises(ValueError, match="canonical UUID"):
        PlanningRequest(
            "task",
            _context(),
            PlanKind.INITIAL,
            None,
            None,
            previous.run_id.upper(),
            previous.session_id,
        )


@pytest.mark.asyncio
async def test_planner_maps_invalid_structured_output() -> None:
    plan = _plan()
    planner = ModelPlanner(QueueGateway("not-json"))
    with pytest.raises(ModelError) as failure:
        await planner.create_plan(
            PlanningRequest(
                "task",
                _context(),
                PlanKind.INITIAL,
                None,
                None,
                plan.run_id,
                plan.session_id,
            )
        )
    assert failure.value.code == "INVALID_PLAN_OUTPUT"


@pytest.mark.asyncio
async def test_agent_prompt_freezes_native_edit_argument_and_patch_format() -> None:
    gateway = QueueGateway(
        json.dumps({"kind": "TASK_READY", "summary": "ready", "action": None})
    )
    plan = _plan()

    await JsonAgentDecisionAdapter(gateway).decide(
        AgentDecisionRequest("edit alpha", _context(), plan, ())
    )

    system = gateway.last_messages[0].content
    assert "{path:string,patch:string}" in system
    assert "--- a/<path> and +++ b/<path>" in system
    assert "write_file only for a path that does not exist" in system
    assert "TASK_READY only after all file modifications" in system
    assert "Validation node runs the exact commands" in system
    assert "Never repeat an approved edit" in system


@pytest.mark.asyncio
async def test_agent_prompt_continues_a_multi_edit_plan_after_first_success() -> None:
    gateway = QueueGateway(
        json.dumps(
            {
                "kind": "TOOL_ACTION",
                "summary": "Continue with the remaining approved edit.",
                "action": {
                    "tool_name": "apply_patch",
                    "arguments": {
                        "path": "beta.py",
                        "patch": (
                            "--- a/beta.py\n+++ b/beta.py\n"
                            "@@ -1 +1 @@\n-old\n+new\n"
                        ),
                    },
                },
            }
        )
    )
    plan = _multi_edit_plan()
    first_edit = Observation(
        str(uuid4()),
        "apply_patch",
        True,
        "Updated alpha.py.",
        None,
        None,
    )
    context = replace(_context(), recent_observations=(first_edit,))

    decision = await JsonAgentDecisionAdapter(gateway).decide(
        AgentDecisionRequest("edit alpha and beta", context, plan, (first_edit,))
    )

    system = gateway.last_messages[0].content
    payload = json.loads(gateway.last_messages[1].content)
    assert decision.kind is AgentDecisionKind.TOOL_ACTION
    assert decision.action is not None
    assert decision.action.arguments["path"] == "beta.py"
    assert len(
        [step for step in payload["plan"]["steps"] if step["tool_name"] == "apply_patch"]
    ) == 2
    assert payload["observations"][0]["success"] is True
    assert "Never repeat an approved edit" in system
    assert "only after all file modifications" in system
    assert "return the next Tool action" in system
    assert "return TASK_READY immediately" not in system


@pytest.mark.asyncio
async def test_editing_tools_are_atomic_and_reject_conflict_or_collision(
    tmp_path: Path,
) -> None:
    target = tmp_path / "alpha.py"
    target.write_text("old\n", encoding="utf-8")
    guard = WorkspaceGuard(tmp_path)
    patch_tool = PatchTool(guard)
    write_tool = WriteFileTool(guard)
    run_id = str(uuid4())
    session_id = str(uuid4())

    changed = await patch_tool.execute(
        ToolInvocation(
            str(uuid4()),
            "apply_patch",
            {
                "path": "alpha.py",
                "patch": "--- a/alpha.py\n+++ b/alpha.py\n@@ -1 +1 @@\n-old\n+new\n",
            },
            run_id,
            session_id,
        )
    )
    assert changed.success
    assert target.read_text(encoding="utf-8") == "new\n"

    conflict = await patch_tool.execute(
        ToolInvocation(
            str(uuid4()),
            "apply_patch",
            {
                "path": "alpha.py",
                "patch": "--- a/alpha.py\n+++ b/alpha.py\n@@ -1 +1 @@\n-old\n+again\n",
            },
            run_id,
            session_id,
        )
    )
    assert conflict.error is not None and conflict.error.code == "PATCH_CONFLICT"
    assert target.read_text(encoding="utf-8") == "new\n"

    created = await write_tool.execute(
        ToolInvocation(
            str(uuid4()),
            "write_file",
            {"path": "beta.py", "content": "value = 1\n"},
            run_id,
            session_id,
        )
    )
    collision = await write_tool.execute(
        ToolInvocation(
            str(uuid4()),
            "write_file",
            {"path": "beta.py", "content": "value = 2\n"},
            run_id,
            session_id,
        )
    )
    assert created.success
    assert collision.error is not None and collision.error.code == "FILE_ALREADY_EXISTS"
    assert (tmp_path / "beta.py").read_text(encoding="utf-8") == "value = 1\n"


class ResultRuntime:
    def __init__(self, error_code: str | None) -> None:
        self.error_code = error_code

    async def execute(
        self,
        invocation: ToolInvocation,
        *,
        authorization: ApprovedPlanEvidence | None = None,
    ) -> ToolResult:
        del authorization
        error = (
            None
            if self.error_code is None
            else ToolError(self.error_code, "safe failure", False)
        )
        return ToolResult(
            invocation.invocation_id,
            invocation.tool_name,
            error is None,
            {"truncated": False},
            error,
            RiskLevel.SAFE,
            PolicyDecision.ALLOWED,
            ApprovalDecision.APPROVED,
            1,
        )


@pytest.mark.asyncio
async def test_validation_repairability_is_mechanical_without_stdout_heuristics() -> None:
    plan = _plan()
    check = ValidationCheck(
        str(uuid4()),
        1,
        ValidationCheckKind.TEST,
        "shell",
        {"argv": ["pytest", "-q", "tests/test_alpha.py"], "cwd": "."},
        "Approved targeted test.",
        True,
    )
    runner = ToolValidationRunner(cast(ToolRuntime, ResultRuntime("COMMAND_EXIT_NONZERO")))
    result = await runner.run(
        ValidationPlan((check,)),
        run_id=plan.run_id,
        session_id=plan.session_id,
        authorization=_evidence(plan),
        repair_count=0,
    )
    assert result.status is ValidationStatus.FAIL
    assert result.repairable is True

    repository_check = ValidationCheck(
        str(uuid4()),
        1,
        ValidationCheckKind.REPOSITORY_COMMAND,
        "shell",
        {"argv": ["pytest", "-q"], "cwd": "."},
        "Repository command.",
        True,
    )
    repository_result = ValidationCheckResult(
        repository_check,
        ValidationStatus.FAIL,
        ToolResult(
            repository_check.check_id,
            "shell",
            False,
            {"stdout": "looks repairable"},
            ToolError("COMMAND_EXIT_NONZERO", "nonzero", False),
            RiskLevel.SAFE,
            PolicyDecision.ALLOWED,
            ApprovalDecision.APPROVED,
            1,
        ),
        "Non-zero repository command.",
    )
    assert aggregate_repairable(ValidationStatus.FAIL, (repository_result,)) is False


class SafePolicy:
    def classify(self, *, operation: str, arguments: JsonObject) -> RiskLevel:
        del operation, arguments
        return RiskLevel.SAFE


class ShellRecorder:
    name = "shell"

    def __init__(self) -> None:
        self.called = False

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        self.called = True
        return ToolResult(
            invocation.invocation_id,
            self.name,
            True,
            {"truncated": False},
            None,
            RiskLevel.SAFE,
            PolicyDecision.ALLOWED,
            None,
            1,
        )


class ApprovedPlanVerifier:
    async def require_approved(
        self, evidence: ApprovedPlanEvidence
    ) -> object:
        del evidence
        return object()


class UnusedApprovalService:
    pass


class UnusedApprovalPolicy:
    async def request(self, request: ApprovalRequest) -> ApprovalRequest:
        raise AssertionError(request)


@pytest.mark.asyncio
async def test_validation_shell_scope_is_checked_even_when_day3_risk_is_safe() -> None:
    plan = _plan()
    evidence = _evidence(plan)
    tool = ShellRecorder()
    runtime = ToolRuntime(
        ToolRegistry([tool]),
        SafePolicy(),
        cast(ApprovalPolicy, UnusedApprovalPolicy()),
        cast(ApprovalService, UnusedApprovalService()),
        plan_approval_service=cast(PlanApprovalService, ApprovedPlanVerifier()),
    )
    denied = await runtime.execute(
        ToolInvocation(
            str(uuid4()),
            "shell",
            {"argv": ["pytest", "-q", "other.py"], "cwd": "."},
            plan.run_id,
            plan.session_id,
        ),
        authorization=evidence,
    )
    assert not denied.success
    assert denied.error is not None and denied.error.code == "PLAN_SCOPE_DENIED"
    assert not tool.called

    allowed = await runtime.execute(
        ToolInvocation(
            str(uuid4()),
            "shell",
            {"argv": ["pytest", "-q", "tests/test_alpha.py"], "cwd": "."},
            plan.run_id,
            plan.session_id,
        ),
        authorization=evidence,
    )
    assert allowed.success and tool.called


def test_validation_result_rejects_nonmechanical_repairable_value() -> None:
    with pytest.raises(ValueError, match="repairable"):
        ValidationResult(
            (),
            (),
            ValidationStatus.UNKNOWN,
            ValidationConfidence.LOW,
            True,
            0,
            "Unknown evidence.",
        )


@pytest.mark.asyncio
async def test_validation_runner_rejects_command_absent_from_approved_plan() -> None:
    plan = _plan()
    invalid = ValidationCheck(
        str(uuid4()),
        1,
        ValidationCheckKind.TEST,
        "shell",
        {"argv": ["pytest", "-q", "other.py"], "cwd": "."},
        "Unapproved command.",
        True,
    )
    runner = ToolValidationRunner(cast(ToolRuntime, ResultRuntime(None)))
    with pytest.raises(ValidationError) as failure:
        await runner.run(
            ValidationPlan((invalid,)),
            run_id=plan.run_id,
            session_id=plan.session_id,
            authorization=_evidence(plan),
            repair_count=0,
        )
    assert failure.value.code == "VALIDATION_PLAN_INVALID"


def test_observation_rejects_any_additional_replan_heuristic() -> None:
    with pytest.raises(ValueError, match="no additional"):
        Observation(
            str(uuid4()),
            "apply_patch",
            False,
            "Ordinary in-scope failure.",
            "PATCH_CONFLICT",
            "Invented semantic reason.",
        )
