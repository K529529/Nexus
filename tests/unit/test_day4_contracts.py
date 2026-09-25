from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Callable, Sequence
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
from nexus.application.validation import DeterministicValidationPlanner, ToolValidationRunner
from nexus.domain.agent_decision import (
    AgentDecision,
    AgentDecisionKind,
    AgentDecisionRequest,
    Observation,
    ToolAction,
)
from nexus.domain.agent_state import AgentState
from nexus.domain.approvals import ApprovalRequest
from nexus.domain.exploration import ExplorationResult, WorkingContext
from nexus.domain.model import ModelChunk, ModelMessage, ModelResponse
from nexus.domain.planning import (
    ApprovedPlanEvidence,
    ChangedFile,
    ChangeKind,
    Plan,
    PlanAuthorizationSource,
    PlanKind,
    PlanStatus,
    PlanStep,
    PlanStepStatus,
    RepairGuidance,
    compute_scope_digest,
    derive_authorization_scope,
)
from nexus.domain.ports.planning import PlanningRequest, RepairPlanningRequest
from nexus.domain.ports.tooling import ApprovalPolicy
from nexus.domain.runtime_events import RuntimeStatus
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
from nexus.infrastructure.checkpoint.postgres import _checkpoint_serializer
from nexus.infrastructure.graph.day4_runtime import (
    _observation_summary,
    _record_change,
    _require_current_edit_evidence,
)
from nexus.security.command_policy import DefaultCommandPolicy
from nexus.security.executables import TrustedExecutables
from nexus.security.workspace import WorkspaceGuard
from nexus.tools import editing as editing_module
from nexus.tools.editing import EditFileTool, PatchTool, WriteFileTool
from nexus.tools.registry import ToolRegistry


class QueueGateway:
    def __init__(self, *responses: str) -> None:
        self.responses = list(responses)
        self.last_messages: tuple[ModelMessage, ...] = ()
        self.messages: list[tuple[ModelMessage, ...]] = []

    async def complete(self, messages: Sequence[ModelMessage]) -> ModelResponse:
        self.last_messages = tuple(messages)
        self.messages.append(self.last_messages)
        return ModelResponse(self.responses.pop(0))

    async def stream(self, messages: Sequence[ModelMessage]) -> AsyncIterator[ModelChunk]:
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


def _edit_file_plan() -> Plan:
    plan = _plan()
    steps = (replace(plan.steps[0], tool_name="edit_file"), plan.steps[1])
    scope = derive_authorization_scope(steps)
    return replace(
        plan,
        steps=steps,
        authorization_scope=scope,
        scope_digest=compute_scope_digest(plan.plan_id, plan.version, scope),
    )


@pytest.mark.parametrize("plan_factory", [_plan, _edit_file_plan])
def test_checkpoint_roundtrip_preserves_legacy_and_new_edit_scope(
    plan_factory: Callable[[], Plan],
) -> None:
    plan = plan_factory()
    serializer = _checkpoint_serializer()
    restored = serializer.loads_typed(serializer.dumps_typed(plan))
    assert restored == plan
    assert restored.scope_digest == plan.scope_digest


def test_new_and_legacy_edit_tools_are_classified_as_writes() -> None:
    policy = DefaultCommandPolicy(TrustedExecutables("python", None, None, None, None, None))
    for name in ("edit_file", "apply_patch", "write_file"):
        assert policy.classify(operation=name, arguments={}) is RiskLevel.WRITE


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


def _read_only_plan() -> Plan:
    current = _plan()
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
    return replace(
        current,
        steps=steps,
        authorization_scope=scope,
        scope_digest=compute_scope_digest(current.plan_id, current.version, scope),
    )


def _editing_only_plan() -> Plan:
    current = _plan()
    steps = (current.steps[0],)
    scope = derive_authorization_scope(steps)
    return replace(
        current,
        steps=steps,
        authorization_scope=scope,
        scope_digest=compute_scope_digest(current.plan_id, current.version, scope),
    )


def _exploration() -> ExplorationResult:
    status = ToolResult(
        str(uuid4()),
        "git_status",
        True,
        {"stdout": "", "truncated": False},
        None,
        RiskLevel.SAFE,
        PolicyDecision.ALLOWED,
        None,
        1,
    )
    return ExplorationResult((), (), (), (), status, (status,), False)


@pytest.mark.asyncio
async def test_planner_initial_and_replan_preserve_canonical_correlation() -> None:
    first = {
        "rationale_summary": "Initial bounded plan.",
        "steps": [
            {
                "description": "Edit alpha",
                "tool_name": "edit_file",
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
    planner = ModelPlanner(QueueGateway("not-json", "not-json"))
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
async def test_planner_retries_invalid_output_with_independently_fitted_messages() -> None:
    invalid = json.dumps(
        {
            "rationale_summary": "SENSITIVE invalid plan",
            "steps": [
                {
                    "description": "edit",
                    "tool_name": "edit_file",
                    "target_paths": [],
                    "command_argv": None,
                    "command_cwd": None,
                }
            ],
        }
    )
    valid = json.dumps(
        {
            "rationale_summary": "Bounded plan.",
            "steps": [
                {
                    "description": "Edit alpha",
                    "tool_name": "edit_file",
                    "target_paths": ["alpha.py"],
                    "command_argv": None,
                    "command_cwd": None,
                }
            ],
        }
    )
    gateway = QueueGateway(invalid, valid)
    fitted: list[tuple[ModelMessage, ...]] = []

    def prepare(
        context: WorkingContext,
        render: Callable[[WorkingContext], Sequence[ModelMessage]],
    ) -> WorkingContext:
        fitted.append(tuple(render(context)))
        return context

    current = _plan()
    result = await ModelPlanner(gateway, prepare_input=prepare).create_plan(
        PlanningRequest(
            "task",
            _context(),
            PlanKind.INITIAL,
            None,
            None,
            current.run_id,
            current.session_id,
        )
    )

    assert result.steps[0].target_paths == ("alpha.py",)
    assert len(gateway.messages) == len(fitted) == 2
    assert len(gateway.messages[0]) == 2
    assert len(gateway.messages[1]) == 3
    assert gateway.messages[0][1] == gateway.messages[1][1]
    feedback = gateway.messages[1][-1].content
    assert "Category: EDIT_TARGET_COUNT" in feedback
    assert "SENSITIVE" not in feedback


@pytest.mark.asyncio
async def test_planner_prompt_freezes_strict_schema_and_validation_argv() -> None:
    response = {
        "rationale_summary": "Inspect and validate.",
        "steps": [
            {
                "description": "Inspect alpha",
                "tool_name": "read_file",
                "target_paths": [],
                "command_argv": None,
                "command_cwd": None,
            }
        ],
    }
    gateway = QueueGateway(json.dumps(response))
    plan = _plan()

    await ModelPlanner(gateway).create_plan(
        PlanningRequest(
            "explain alpha",
            _context(),
            PlanKind.INITIAL,
            None,
            None,
            plan.run_id,
            plan.session_id,
        )
    )

    system = gateway.last_messages[0].content
    assert "exactly the keys rationale_summary and steps" in system
    assert "steps is a required non-empty array" in system
    assert "Every step has exactly the keys" in system
    assert "Read-only and repository-explanation tasks still require" in system
    assert "must target exactly one file" in system
    assert "create multiple separate PlanStep objects" in system
    assert '["src/a.py","tests/test_a.py"]' in system
    assert "An empty target_paths array is also invalid" in system
    assert "Valid editing example" in system
    assert '"tool_name":"edit_file","target_paths":["src/a.py"]' in system
    assert "Security tasks still require a legal non-authorizing narrative PlanStep" in system
    assert "Agent must then propose the requested operation" in system
    assert "submit the unchanged requested Tool action to ToolRuntime" in system
    assert "Do not tell the Agent to preemptively refuse" in system
    assert "Valid security-boundary Plan example" in system
    assert "without granting it Plan authority" in system
    assert "pytest ..., uv run pytest ..." in system
    assert "Never use python -m pytest" in system
    assert "Do not use a markdown fence" in system


@pytest.mark.asyncio
async def test_repair_prompt_preserves_single_target_and_approved_scope_rules() -> None:
    response = {
        "failure_summary": "Use the existing approved edit.",
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
    gateway = QueueGateway(json.dumps(response))
    plan = _plan()
    validation = ValidationResult(
        (),
        (),
        ValidationStatus.UNKNOWN,
        ValidationConfidence.LOW,
        False,
        0,
        "Validation evidence was inconclusive.",
    )

    await ModelPlanner(gateway).create_repair_guidance(
        RepairPlanningRequest("edit alpha", _context(), plan, validation, 1)
    )

    system = gateway.last_messages[0].content
    assert "must target exactly one file" in system
    assert "multiple separate PlanStep objects" in system
    assert "Never use an empty target_paths array" in system
    assert "Valid editing step" in system
    assert "already present in the approved Plan" in system


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "category"),
    [
        ("SENSITIVE-not-json", "JSON_DECODE"),
        (
            json.dumps(
                {
                    "rationale_summary": "bounded",
                    "steps": [],
                    "SENSITIVE_EXTRA": True,
                }
            ),
            "TOP_LEVEL_KEYS",
        ),
        (
            json.dumps(
                {
                    "rationale_summary": "bounded",
                    "steps": [{"description": "SENSITIVE"}],
                }
            ),
            "STEP_KEYS",
        ),
        (json.dumps({"rationale_summary": "bounded", "steps": []}), "STEPS_SCHEMA"),
        (
            json.dumps(
                {
                    "rationale_summary": "",
                    "steps": [
                        {
                            "description": "inspect",
                            "tool_name": None,
                            "target_paths": [],
                            "command_argv": None,
                            "command_cwd": None,
                        }
                    ],
                }
            ),
            "REQUIRED_FIELD",
        ),
    ],
)
async def test_planner_reports_only_sanitized_parser_category(response: str, category: str) -> None:
    plan = _plan()

    with pytest.raises(ModelError) as failure:
        await ModelPlanner(QueueGateway(response, response)).create_plan(
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
    assert f"Category: {category}" in str(failure.value)
    assert "SENSITIVE" not in str(failure.value)


@pytest.mark.parametrize(
    ("step", "category"),
    [
        (
            {
                "description": "edit",
                "tool_name": "edit_file",
                "target_paths": [],
                "command_argv": None,
                "command_cwd": None,
            },
            "EDIT_TARGET_COUNT",
        ),
        (
            {
                "description": "edit and run",
                "tool_name": "write_file",
                "target_paths": ["new.py"],
                "command_argv": ["pytest", "-q"],
                "command_cwd": ".",
            },
            "COMMAND_TOOL_RELATION",
        ),
        (
            {
                "description": "cwd without command",
                "tool_name": "shell",
                "target_paths": [],
                "command_argv": None,
                "command_cwd": ".",
            },
            "COMMAND_CWD_RELATION",
        ),
        (
            {
                "description": "narrative with authority",
                "tool_name": None,
                "target_paths": ["alpha.py"],
                "command_argv": None,
                "command_cwd": None,
            },
            "NARRATIVE_AUTHORITY",
        ),
        (
            {
                "description": "read with authority",
                "tool_name": "read_file",
                "target_paths": ["alpha.py"],
                "command_argv": None,
                "command_cwd": None,
            },
            "NON_FROZEN_TOOL_AUTHORITY",
        ),
        (
            {
                "description": "invalid path",
                "tool_name": "edit_file",
                "target_paths": ["../alpha.py"],
                "command_argv": None,
                "command_cwd": None,
            },
            "INVALID_REPOSITORY_PATH",
        ),
    ],
)
@pytest.mark.asyncio
async def test_planner_reports_specific_sanitized_plan_step_category(
    step: dict[str, object],
    category: str,
) -> None:
    response = json.dumps({"rationale_summary": "bounded", "steps": [step]})
    current_plan = _plan()

    with pytest.raises(ModelError) as failure:
        await ModelPlanner(QueueGateway(response, response)).create_plan(
            PlanningRequest(
                "task",
                _context(),
                PlanKind.INITIAL,
                None,
                None,
                current_plan.run_id,
                current_plan.session_id,
            )
        )

    assert failure.value.code == "INVALID_PLAN_OUTPUT"
    assert str(failure.value) == f"The model returned invalid Plan. Category: {category}."


@pytest.mark.asyncio
async def test_agent_prompt_freezes_native_edit_argument_and_patch_format() -> None:
    gateway = QueueGateway(json.dumps({"kind": "TASK_READY", "summary": "ready", "action": None}))
    plan = _plan()

    await JsonAgentDecisionAdapter(gateway).decide(
        AgentDecisionRequest("edit alpha", _context(), plan, ())
    )

    system = gateway.last_messages[0].content
    assert "{path:string,patch:string}" in system
    assert "For this previously approved Plan only" in system
    assert "write_file only for a path that does not exist" in system
    assert "TASK_READY only after all file modifications" in system
    assert "Validation node runs the exact commands" in system
    assert "Never repeat an approved edit" in system
    assert "TASK_READY.summary must contain the complete grounded" in system
    assert "the next decision must be TOOL_ACTION" in system
    assert "Submit it exactly once" in system
    assert "does not authorize or perform the operation by itself" in system
    assert "Valid authorization-boundary example" in system
    assert '"path":"../external.txt"' in system
    assert "PLAN_SCOPE_DENIED, PERMISSION_DENIED, or COMMAND_DENIED" in system
    assert "exactly the keys kind, summary, and action" in system
    assert "For CONTINUE or TASK_READY, action must be null" in system
    assert "Do not use a markdown fence" in system


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "category"),
    [
        ("SENSITIVE-not-json", "JSON_DECODE"),
        (
            json.dumps(
                {
                    "kind": "TASK_READY",
                    "summary": "ready",
                    "action": None,
                    "SENSITIVE_EXTRA": True,
                }
            ),
            "TOP_LEVEL_KEYS",
        ),
        (
            json.dumps({"kind": "SENSITIVE_KIND", "summary": "ready", "action": None}),
            "KIND_ENUM",
        ),
        (
            json.dumps(
                {
                    "kind": "TOOL_ACTION",
                    "summary": "act",
                    "action": {"tool_name": "read_file"},
                }
            ),
            "ACTION_SCHEMA",
        ),
        (
            json.dumps(
                {
                    "kind": "TASK_READY",
                    "summary": "ready",
                    "action": {"tool_name": "read_file", "arguments": {}},
                }
            ),
            "ACTION_RELATION",
        ),
        (
            json.dumps({"kind": "TASK_READY", "summary": "", "action": None}),
            "REQUIRED_FIELD",
        ),
    ],
)
async def test_agent_reports_only_sanitized_parser_category(response: str, category: str) -> None:
    with pytest.raises(ModelError) as failure:
        await JsonAgentDecisionAdapter(QueueGateway(response, response)).decide(
            AgentDecisionRequest("task", _context(), _plan(), ())
        )

    assert failure.value.code == "INVALID_AGENT_DECISION"
    assert f"Category: {category}" in str(failure.value)
    assert "SENSITIVE" not in str(failure.value)


@pytest.mark.asyncio
async def test_agent_prompt_continues_a_multi_edit_plan_after_first_success() -> None:
    gateway = QueueGateway(
        json.dumps(
            {
                "kind": "TASK_READY",
                "summary": "SENSITIVE malformed decision",
                "action": {"tool_name": "read_file", "arguments": {}},
            }
        ),
        json.dumps(
            {
                "kind": "TOOL_ACTION",
                "summary": "Continue with the remaining approved edit.",
                "action": {
                    "tool_name": "apply_patch",
                    "arguments": {
                        "path": "beta.py",
                        "patch": ("--- a/beta.py\n+++ b/beta.py\n@@ -1 +1 @@\n-old\n+new\n"),
                    },
                },
            }
        ),
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

    system = gateway.messages[0][0].content
    first_payload = gateway.messages[0][1].content
    payload = json.loads(gateway.messages[1][1].content)
    feedback = gateway.messages[1][-1].content
    assert decision.kind is AgentDecisionKind.TOOL_ACTION
    assert decision.action is not None
    assert decision.action.arguments["path"] == "beta.py"
    assert gateway.messages[1][1].content == first_payload
    assert (
        len([step for step in payload["plan"]["steps"] if step["tool_name"] == "apply_patch"]) == 2
    )
    assert payload["observations"][0]["success"] is True
    assert "Category: ACTION_RELATION" in feedback
    assert "approved Plan and observations remain authoritative" in feedback
    assert "Do not repeat actions recorded as successful" in feedback
    assert "SENSITIVE" not in feedback
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


@pytest.mark.asyncio
async def test_edit_file_replaces_one_exact_match_and_records_change(tmp_path: Path) -> None:
    target = tmp_path / "alpha.py"
    target.write_bytes(b"first\r\nold\r\nlast\r\n")
    tool = EditFileTool(WorkspaceGuard(tmp_path))
    invocation = ToolInvocation(
        str(uuid4()),
        "edit_file",
        {"path": "alpha.py", "old_str": "old", "new_str": "new"},
        str(uuid4()),
        str(uuid4()),
    )

    result = await tool.execute(invocation)

    assert result.success and result.output is not None
    assert target.read_bytes() == b"first\r\nnew\r\nlast\r\n"
    assert result.output == {
        "path": "alpha.py",
        "change_kind": "MODIFIED",
        "bytes_before": len(b"first\r\nold\r\nlast\r\n"),
        "bytes_after": len(b"first\r\nnew\r\nlast\r\n"),
        "sha256_before": hashlib.sha256(b"first\r\nold\r\nlast\r\n").hexdigest(),
        "sha256_after": hashlib.sha256(b"first\r\nnew\r\nlast\r\n").hexdigest(),
    }
    changed = _record_change((), result)
    assert [(item.path, item.change_kind) for item in changed] == [
        ("alpha.py", ChangeKind.MODIFIED)
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "old_str", "new_str", "code"),
    [
        ("alpha\n", "missing", "new", "EDIT_TARGET_NOT_FOUND"),
        ("alpha alpha\n", "alpha", "new", "EDIT_TARGET_AMBIGUOUS"),
        ("aaa\n", "aa", "new", "EDIT_TARGET_AMBIGUOUS"),
        ("alpha\n", "alpha", "alpha", "EDIT_NO_CHANGES"),
    ],
)
async def test_edit_file_rejects_missing_ambiguous_and_noop_without_writing(
    tmp_path: Path, source: str, old_str: str, new_str: str, code: str
) -> None:
    target = tmp_path / "alpha.py"
    target.write_text(source, encoding="utf-8")
    action = {"path": "alpha.py", "old_str": old_str, "new_str": new_str}
    result = await EditFileTool(WorkspaceGuard(tmp_path)).execute(
        ToolInvocation(str(uuid4()), "edit_file", action, str(uuid4()), str(uuid4()))
    )
    assert not result.success and result.error is not None
    assert result.error.code == code
    assert target.read_text(encoding="utf-8") == source
    summary = _observation_summary(result)
    assert code in summary


@pytest.mark.asyncio
async def test_edit_file_reports_line_ending_mismatch_without_weakening_exact_match(
    tmp_path: Path,
) -> None:
    target = tmp_path / "alpha.py"
    target.write_bytes(b"old\r\nnext\r\n")
    tool = EditFileTool(WorkspaceGuard(tmp_path))

    async def edit(old_str: str, new_str: str) -> ToolResult:
        return await tool.execute(
            ToolInvocation(
                str(uuid4()), "edit_file",
                {"path": "alpha.py", "old_str": old_str, "new_str": new_str},
                str(uuid4()), str(uuid4()),
            )
        )

    mismatch = await edit("old\nnext", "sensitive replacement")
    assert mismatch.error is not None
    assert mismatch.error.code == "EDIT_TARGET_NOT_FOUND"
    assert target.read_bytes() == b"old\r\nnext\r\n"
    summary = _observation_summary(mismatch)
    assert "line-ending normalization" in summary
    assert "old\nnext" not in summary
    assert "sensitive replacement" not in summary

    exact = await edit("old\r\nnext", "updated\r\nnext")
    assert exact.success
    assert target.read_bytes() == b"updated\r\nnext\r\n"


@pytest.mark.asyncio
async def test_edit_file_rejects_unsafe_files_and_paths(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    tool = EditFileTool(WorkspaceGuard(repository))

    async def edit(path: str) -> ToolResult:
        return await tool.execute(
            ToolInvocation(
                str(uuid4()),
                "edit_file",
                {"path": path, "old_str": "old", "new_str": "new"},
                str(uuid4()),
                str(uuid4()),
            )
        )

    (repository / "binary.py").write_bytes(b"old\xff")
    (repository / "large.py").write_bytes(b"old" + b"x" * 1_048_576)
    (tmp_path / "outside.py").write_text("old", encoding="utf-8")
    for path in ("binary.py", "large.py"):
        result = await edit(path)
        assert result.error is not None and result.error.code == "UNSUPPORTED_FILE"
    outside = await edit("../outside.py")
    assert outside.error is not None and outside.error.code == "WORKSPACE_PATH_DENIED"
    assert (tmp_path / "outside.py").read_text(encoding="utf-8") == "old"

    invalid = await tool.execute(ToolInvocation(
        str(uuid4()), "edit_file",
        {"path": "binary.py", "old_str": "\ud800", "new_str": "new"},
        str(uuid4()), str(uuid4()),
    ))
    assert invalid.error is not None and invalid.error.code == "INVALID_TOOL_ARGUMENTS"


@pytest.mark.asyncio
async def test_edit_file_atomic_replace_failure_leaves_original_and_no_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "alpha.py"
    target.write_text("old", encoding="utf-8")

    def fail_replace(source: Path, destination: Path) -> None:
        del source, destination
        raise OSError("private failure")

    monkeypatch.setattr(editing_module.os, "replace", fail_replace)
    result = await EditFileTool(WorkspaceGuard(tmp_path)).execute(
        ToolInvocation(
            str(uuid4()),
            "edit_file",
            {"path": "alpha.py", "old_str": "old", "new_str": "new"},
            str(uuid4()),
            str(uuid4()),
        )
    )
    assert result.error is not None and result.error.code == "TOOL_EXECUTION_ERROR"
    assert target.read_text(encoding="utf-8") == "old"
    assert not await asyncio.to_thread(lambda: list(tmp_path.glob(".nexus-patch-*")))


@pytest.mark.asyncio
async def test_new_plan_and_agent_use_edit_file_without_exposing_legacy_patch() -> None:
    step = {
        "description": "Edit alpha",
        "tool_name": "edit_file",
        "target_paths": ["alpha.py"],
        "command_argv": None,
        "command_cwd": None,
    }
    current = _plan()
    planner = ModelPlanner(
        QueueGateway(
            json.dumps(
                {
                    "rationale_summary": "Edit alpha safely.",
                    "steps": [step],
                }
            )
        )
    )
    plan = await planner.create_plan(
        PlanningRequest(
            "edit alpha",
            _context(),
            PlanKind.INITIAL,
            None,
            None,
            current.run_id,
            current.session_id,
        )
    )
    assert plan.authorization_scope.allowed_write_actions == (("edit_file", "alpha.py"),)
    agent_gateway = QueueGateway(
        json.dumps(
            {
                "kind": "TOOL_ACTION",
                "summary": "Edit alpha.",
                "action": {
                    "tool_name": "edit_file",
                    "arguments": {
                        "path": "alpha.py",
                        "old_str": "SENSITIVE_OLD",
                        "new_str": "SENSITIVE_NEW",
                    },
                },
            }
        )
    )
    decision = await JsonAgentDecisionAdapter(agent_gateway).decide(
        AgentDecisionRequest("edit alpha", _context(), plan, ())
    )
    assert decision.action is not None and decision.action.tool_name == "edit_file"
    prompt = agent_gateway.last_messages[0].content
    assert "{path:string,old_str:string,new_str:string}" in prompt
    assert "Prefer a unique old_str without newline characters" in prompt
    assert "apply_patch" not in prompt
    assert "@@" not in prompt

    legacy = {**step, "tool_name": "apply_patch"}
    with pytest.raises(ModelError, match="STEP_SCHEMA"):
        await ModelPlanner(
            QueueGateway(
                *[
                    json.dumps(
                        {
                            "rationale_summary": "Legacy edit.",
                            "steps": [legacy],
                        }
                    )
                ]
                * 2
            )
        ).create_plan(
            PlanningRequest(
                "edit alpha",
                _context(),
                PlanKind.INITIAL,
                None,
                None,
                current.run_id,
                current.session_id,
            )
        )


@pytest.mark.asyncio
async def test_edit_file_approval_is_tool_and_path_and_validation_is_unchanged(
    tmp_path: Path,
) -> None:
    target = tmp_path / "alpha.py"
    target.write_text("old\n", encoding="utf-8")
    (tmp_path / "beta.py").write_text("old\n", encoding="utf-8")
    plan = _edit_file_plan()
    runtime = ToolRuntime(
        ToolRegistry([EditFileTool(WorkspaceGuard(tmp_path))]),
        WritePolicy(),
        cast(ApprovalPolicy, UnusedApprovalPolicy()),
        cast(ApprovalService, UnusedApprovalService()),
        plan_approval_service=cast(PlanApprovalService, ApprovedPlanVerifier()),
    )

    async def edit(path: str, old_str: str, new_str: str) -> ToolResult:
        return await runtime.execute(
            ToolInvocation(
                str(uuid4()),
                "edit_file",
                {"path": path, "old_str": old_str, "new_str": new_str},
                plan.run_id,
                plan.session_id,
            ),
            authorization=_evidence(plan),
        )

    denied = await edit("beta.py", "old", "new")
    assert denied.error is not None and denied.error.code == "PLAN_SCOPE_DENIED"
    assert (tmp_path / "beta.py").read_text(encoding="utf-8") == "old\n"
    allowed = await edit("alpha.py", "old", "new")
    assert allowed.success
    changed = _record_change((), allowed)
    validation_plan = await DeterministicValidationPlanner().plan(
        task="edit alpha",
        plan=plan,
        exploration=_exploration(),
        context=_context(),
        changed_files=changed,
    )
    assert [check.kind for check in validation_plan.checks] == [
        ValidationCheckKind.TEST,
        ValidationCheckKind.DIFF_INSPECTION,
    ]
    assert validation_plan.checks[0].arguments["argv"] == [
        "pytest",
        "-q",
        "tests/test_alpha.py",
    ]
    failed = await edit("alpha.py", "SENSITIVE_OLD", "SENSITIVE_NEW")
    assert failed.error is not None and failed.error.code == "EDIT_TARGET_NOT_FOUND"
    summary = _observation_summary(failed)
    assert "SENSITIVE_OLD" not in summary and "SENSITIVE_NEW" not in summary
    assert _record_change(changed, allowed) == changed


def _read_observation(path: str, content: str) -> tuple[Observation, ToolResult]:
    invocation_id = str(uuid4())
    return (
        Observation(invocation_id, "read_file", True, "current file evidence", None, None),
        ToolResult(
            invocation_id, "read_file", True,
            {"path": path, "content": content}, None,
            RiskLevel.SAFE, PolicyDecision.ALLOWED, None, 1,
        ),
    )


def _edit_decision(path: str = "alpha.py", old_str: str = "old") -> AgentDecision:
    return AgentDecision(
        AgentDecisionKind.TOOL_ACTION,
        ToolAction("edit_file", {"path": path, "old_str": old_str, "new_str": "new"}),
        "Edit the approved file.",
    )


def _edit_guard_state(plan: Plan) -> AgentState:
    return AgentState(
        "edit alpha", [ModelMessage("user", "edit alpha")],
        plan.run_id, plan.session_id, RuntimeStatus.STARTED, plan=plan,
    )


def test_agent_edit_requires_current_same_path_read_after_failure() -> None:
    plan = _edit_file_plan()
    state = _edit_guard_state(plan)
    edit = _edit_decision()
    required = _require_current_edit_evidence(state, edit)
    assert required.action == ToolAction("read_file", {"path": "alpha.py"})

    wrong_read, wrong_result = _read_observation("beta.py", "old")
    state = replace(state, observations=(wrong_read,), tool_results=(wrong_result,))
    assert _require_current_edit_evidence(state, edit).action == required.action

    target_read, target_result = _read_observation("alpha.py", "old\r\n")
    state = replace(
        state, observations=(*state.observations, target_read),
        tool_results=(*state.tool_results, target_result),
    )
    assert _require_current_edit_evidence(state, edit) is edit

    failed = Observation(
        str(uuid4()), "edit_file", False,
        "edit_file failed with EDIT_TARGET_NOT_FOUND.", "EDIT_TARGET_NOT_FOUND", None,
    )
    state = replace(state, observations=(*state.observations, failed))
    assert _require_current_edit_evidence(state, edit).action == required.action

    refreshed, refreshed_result = _read_observation("alpha.py", "old\r\n")
    state = replace(
        state, observations=(*state.observations, refreshed),
        tool_results=(*state.tool_results, refreshed_result),
    )
    assert _require_current_edit_evidence(state, edit) is edit

    ambiguous = replace(failed, invocation_id=str(uuid4()), error_code="EDIT_TARGET_AMBIGUOUS")
    state = replace(state, observations=(*state.observations, ambiguous))
    assert _require_current_edit_evidence(state, edit).action == required.action


def test_agent_edit_success_requires_new_evidence_and_allows_repair() -> None:
    plan = _edit_file_plan()
    read, result = _read_observation("alpha.py", "old\n")
    state = replace(
        _edit_guard_state(plan), observations=(read,), tool_results=(result,),
    )
    edit = _edit_decision()
    assert _require_current_edit_evidence(state, edit) is edit

    success = Observation(str(uuid4()), "edit_file", True, "edit completed", None, None)
    state = replace(state, observations=(*state.observations, success))
    assert _require_current_edit_evidence(state, edit).action == ToolAction(
        "read_file", {"path": "alpha.py"}
    )

    refreshed, refreshed_result = _read_observation("alpha.py", "new\n")
    state = replace(
        state,
        observations=(*state.observations, refreshed),
        tool_results=(*state.tool_results, refreshed_result),
        validation_result=ValidationResult(
            (), (), ValidationStatus.FAIL, ValidationConfidence.LOW,
            False, 1, "A further approved edit is required.",
        ),
        repair_guidance=RepairGuidance(
            plan.plan_id, plan.version, 1, plan.steps,
            "A further approved edit is required.",
        ),
    )
    assert _require_current_edit_evidence(state, edit).action == ToolAction(
        "read_file", {"path": "alpha.py"}
    )
    repair_edit = _edit_decision(old_str="new")
    assert _require_current_edit_evidence(state, repair_edit) is repair_edit


def test_agent_edit_guard_preserves_formal_scope_denial() -> None:
    plan = _edit_file_plan()
    state = _edit_guard_state(plan)
    out_of_scope = _edit_decision(path="beta.py")
    assert _require_current_edit_evidence(state, out_of_scope) is out_of_scope


class ResultRuntime:
    def __init__(self, error_code: str | None, *, truncated: bool = False) -> None:
        self.error_code = error_code
        self.truncated = truncated

    async def execute(
        self,
        invocation: ToolInvocation,
        *,
        authorization: ApprovedPlanEvidence | None = None,
    ) -> ToolResult:
        del authorization
        error = (
            None if self.error_code is None else ToolError(self.error_code, "safe failure", False)
        )
        return ToolResult(
            invocation.invocation_id,
            invocation.tool_name,
            error is None,
            {"truncated": self.truncated},
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
        changed=True,
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
    async def require_approved(self, evidence: ApprovedPlanEvidence) -> object:
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
            changed=False,
        )
    assert failure.value.code == "VALIDATION_PLAN_INVALID"


@pytest.mark.asyncio
async def test_read_only_validation_passes_with_deterministic_no_change_evidence() -> None:
    plan = _read_only_plan()
    validation_plan = await DeterministicValidationPlanner().plan(
        task="explain alpha",
        plan=plan,
        exploration=_exploration(),
        context=_context(),
        changed_files=(),
    )

    assert [check.kind for check in validation_plan.checks] == [ValidationCheckKind.DIFF_INSPECTION]
    result = await ToolValidationRunner(cast(ToolRuntime, ResultRuntime(None))).run(
        validation_plan,
        run_id=plan.run_id,
        session_id=plan.session_id,
        authorization=_evidence(plan),
        repair_count=0,
        changed=False,
    )

    assert result.status is ValidationStatus.PASS
    assert result.executed_checks[0].status is ValidationStatus.PASS


@pytest.mark.asyncio
async def test_changed_edit_without_conclusive_code_check_remains_unknown() -> None:
    plan = _editing_only_plan()
    changed = (
        ChangedFile(
            "alpha.py",
            ChangeKind.MODIFIED,
            str(uuid4()),
            str(uuid4()),
        ),
    )
    validation_plan = await DeterministicValidationPlanner().plan(
        task="edit alpha",
        plan=plan,
        exploration=_exploration(),
        context=_context(),
        changed_files=changed,
    )
    result = await ToolValidationRunner(cast(ToolRuntime, ResultRuntime(None))).run(
        validation_plan,
        run_id=plan.run_id,
        session_id=plan.session_id,
        authorization=_evidence(plan),
        repair_count=0,
        changed=True,
    )

    assert result.status is ValidationStatus.UNKNOWN
    assert result.executed_checks[0].status is ValidationStatus.PASS


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_code", "truncated"),
    [
        ("SANDBOX_EXECUTION_ERROR", False),
        ("PERMISSION_DENIED", False),
        (None, True),
    ],
)
async def test_read_only_no_change_evidence_never_fails_open(
    error_code: str | None,
    truncated: bool,
) -> None:
    plan = _read_only_plan()
    validation_plan = await DeterministicValidationPlanner().plan(
        task="explain alpha",
        plan=plan,
        exploration=_exploration(),
        context=_context(),
        changed_files=(),
    )
    result = await ToolValidationRunner(
        cast(ToolRuntime, ResultRuntime(error_code, truncated=truncated))
    ).run(
        validation_plan,
        run_id=plan.run_id,
        session_id=plan.session_id,
        authorization=_evidence(plan),
        repair_count=0,
        changed=False,
    )

    assert result.status is ValidationStatus.UNKNOWN


class WritePolicy:
    def classify(self, *, operation: str, arguments: JsonObject) -> RiskLevel:
        del operation, arguments
        return RiskLevel.WRITE


@pytest.mark.asyncio
async def test_out_of_scope_write_is_denied_before_workspace_side_effect(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    plan = _read_only_plan()
    runtime = ToolRuntime(
        ToolRegistry([WriteFileTool(WorkspaceGuard(repository))]),
        WritePolicy(),
        cast(ApprovalPolicy, UnusedApprovalPolicy()),
        cast(ApprovalService, UnusedApprovalService()),
        plan_approval_service=cast(PlanApprovalService, ApprovedPlanVerifier()),
    )

    result = await runtime.execute(
        ToolInvocation(
            str(uuid4()),
            "write_file",
            {"path": "../outside.txt", "content": "compromised"},
            plan.run_id,
            plan.session_id,
        ),
        authorization=_evidence(plan),
    )

    assert result.success is False
    assert result.error is not None and result.error.code == "PLAN_SCOPE_DENIED"
    assert list(repository.iterdir()) == []
    assert not (tmp_path / "outside.txt").exists()


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
