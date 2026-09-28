from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast
from uuid import uuid4

import pytest

from nexus.application.evidence_projection import bounded_output
from nexus.application.planning import (
    JsonAgentDecisionAdapter,
    ModelPlanner,
    _planning_payload,
    _validation_evidence,
)
from nexus.application.tool_runtime import ToolRuntime
from nexus.context.chunking import LineWindowChunker
from nexus.context.manager import BoundedContextManager, model_input_tokens
from nexus.context.retrieval import ToolRepositoryAccess
from nexus.domain.agent_decision import AgentDecisionRequest, Observation, ToolAction
from nexus.domain.context import ContextBudget
from nexus.domain.exploration import WorkingContext
from nexus.domain.model import ModelMessage, ModelResponse
from nexus.domain.planning import (
    AuthorizationScope,
    CompletionRequirement,
    Plan,
    PlanKind,
    PlanStatus,
    PlanStep,
    PlanStepStatus,
    compute_scope_digest,
)
from nexus.domain.ports.context import ContextProvider
from nexus.domain.ports.model_gateway import ModelGateway
from nexus.domain.ports.planning import PlanningRequest, RepairPlanningRequest
from nexus.domain.tooling import (
    ApprovalDecision,
    PolicyDecision,
    RiskLevel,
    ToolError,
    ToolResult,
)
from nexus.domain.validation import (
    ValidationCheck,
    ValidationCheckKind,
    ValidationCheckResult,
    ValidationConfidence,
    ValidationResult,
    ValidationStatus,
)
from nexus.errors import ContextError
from nexus.infrastructure.graph.day4_runtime import (
    _observation_summary,
    _safe_replan_observations,
)


@dataclass
class CaptureGateway:
    response: str
    calls: list[tuple[ModelMessage, ...]]

    def __init__(self, response: str = '{"kind":"TASK_READY","summary":"ready","action":null}'):
        self.response = response
        self.calls = []

    async def complete(self, messages: tuple[ModelMessage, ...]) -> ModelResponse:
        self.calls.append(tuple(messages))
        return ModelResponse(self.response)


def manager(maximum: int = 24000, observations: int = 8) -> BoundedContextManager:
    return BoundedContextManager(
        cast(ContextProvider, object()),
        ToolRepositoryAccess(cast(ToolRuntime, object())),
        LineWindowChunker(),
        ContextBudget(12, 6, min(maximum, 12000), observations),
        maximum,
    )


def plan() -> Plan:
    scope = AuthorizationScope((), ())
    step = PlanStep(
        str(uuid4()), 1, "Inspect evidence", None, (), None, None,
        PlanStepStatus.PENDING,
    )
    plan_id = str(uuid4())
    return Plan(
        plan_id, str(uuid4()), str(uuid4()), 1, PlanKind.INITIAL,
        PlanStatus.CREATED, ApprovalDecision.PENDING, (step,), scope,
        "Inspect evidence", None, None, compute_scope_digest(plan_id, 1, scope),
        datetime.now(UTC), None,
        CompletionRequirement.WORKSPACE_CHANGE_NOT_REQUIRED,
    )


def context() -> WorkingContext:
    return WorkingContext("Inspect evidence", (), (), (), (), False)


def result(
    tool_name: str, output: dict[str, object], *, success: bool = True,
    error_code: str | None = None,
) -> ToolResult:
    return ToolResult(
        str(uuid4()), tool_name, success, output,
        None if error_code is None else ToolError(error_code, "safe error", False),
        RiskLevel.SAFE, PolicyDecision.ALLOWED, None, 1,
    )


def test_successful_edit_observation_preserves_applied_replacement() -> None:
    tool = result("edit_file", {"path": "pvlib/iam.py"})
    action = ToolAction(
        "edit_file",
        {"path": "pvlib/iam.py", "old_str": "return old_value", "new_str": "return new_value"},
    )

    summary = _observation_summary(tool, action)

    assert 'path="pvlib/iam.py"' in summary
    assert 'old_str="return old_value"' in summary
    assert 'new_str="return new_value"' in summary
    assert "already been applied successfully" in summary
    assert "not a complete file snapshot" in summary
    assert "old_str_truncated=false" in summary
    assert "new_str_truncated=false" in summary


def test_successful_edit_observation_bounds_both_replacement_sides() -> None:
    tool = result("edit_file", {"path": "pvlib/iam.py"})
    action = ToolAction(
        "edit_file",
        {
            "path": "pvlib/iam.py",
            "old_str": "OLD_HEAD" + "o" * 5000 + "OLD_TAIL",
            "new_str": "NEW_HEAD" + "n" * 5000 + "NEW_TAIL",
        },
    )

    summary = _observation_summary(tool, action)

    assert len(summary) < 4096
    assert "old_str_truncated=true" in summary
    assert "new_str_truncated=true" in summary
    assert summary.count("...[output omitted]...") == 2
    for marker in ("OLD_HEAD", "OLD_TAIL", "NEW_HEAD", "NEW_TAIL"):
        assert marker in summary


@pytest.mark.parametrize(
    "action",
    [
        None,
        ToolAction(
            "edit_file",
            {"path": "pvlib/iam.py", "old_str": 123, "new_str": "replacement"},
        ),
    ],
)
def test_successful_edit_observation_without_valid_action_uses_generic_summary(
    action: ToolAction | None,
) -> None:
    tool = result("edit_file", {"path": "pvlib/iam.py"})

    assert _observation_summary(tool, action) == (
        "edit_file completed successfully; this exact replacement is complete."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "output", "success", "error_code", "sentinel"),
    [
        ("shell", {"exit_code": 0, "stdout": "nan\n", "stderr": "", "output_truncated": False},
         True, None, "nan"),
        ("shell", {"exit_code": 1, "stdout": "", "stderr": "AssertionError: wrong value",
                   "output_truncated": False},
         False, "COMMAND_EXIT_NONZERO", "AssertionError: wrong value"),
        ("shell", {"exit_code": 0,
                   "stdout": "prefix " * 1500 + "\nTAIL_STDOUT_SENTINEL",
                   "stderr": "", "output_truncated": False},
         True, None, "TAIL_STDOUT_SENTINEL"),
        ("shell", {"exit_code": 1, "stdout": "",
                   "stderr": "prefix " * 1500 + "\nTAIL_ASSERTION_SENTINEL",
                   "output_truncated": False},
         False, "COMMAND_EXIT_NONZERO", "TAIL_ASSERTION_SENTINEL"),
        ("search_files", {"paths": ["src/important.py"], "truncated": False},
         True, None, "src/important.py"),
        ("lexical_search", {"matches": [{"path": "src/a.py", "line": 7, "column": 2,
                                         "text": "unique_symbol = 1"}], "truncated": False},
         True, None, "unique_symbol = 1"),
    ],
)
async def test_tool_evidence_reaches_actual_gateway(
    tool_name: str, output: dict[str, object], success: bool,
    error_code: str | None, sentinel: str,
) -> None:
    tool = result(tool_name, output, success=success, error_code=error_code)
    evidence = _observation_summary(tool)
    observation = Observation(tool.invocation_id, tool_name, success, evidence, error_code, None)
    policy = manager()
    current_plan = plan()
    prepared = await policy.prepare_agent_context(
        working_context=context(), plan=current_plan, observations=(observation,),
        conversation_turns=(),
    )
    gateway = CaptureGateway()
    await JsonAgentDecisionAdapter(
        cast(ModelGateway, gateway), prepare_input=policy.fit_model_input,
    ).decide(AgentDecisionRequest("Inspect evidence", prepared, current_plan, (observation,)))
    messages = gateway.calls[0]
    assert len(gateway.calls) == 1
    assert sentinel in messages[1].content
    assert messages[1].content.count(sentinel) == 1
    assert len(evidence) <= 4096
    assert model_input_tokens(messages) <= 24000


@pytest.mark.asyncio
@pytest.mark.parametrize("window", [0, 8])
async def test_latest_observation_survives_history_pressure_and_zero_window(
    window: int,
) -> None:
    history = tuple(
        Observation(str(uuid4()), "shell", True, f"old-{i} " * 200, None, None)
        for i in range(20)
    )
    latest = Observation(str(uuid4()), "shell", True, "LATEST-UNIQUE-EVIDENCE", None, None)
    policy = manager(2000, observations=window)
    current_plan = plan()
    prepared = await policy.prepare_agent_context(
        working_context=context(), plan=current_plan, observations=(*history, latest),
        conversation_turns=(),
    )
    gateway = CaptureGateway()
    await JsonAgentDecisionAdapter(
        cast(ModelGateway, gateway), prepare_input=policy.fit_model_input,
    ).decide(AgentDecisionRequest("Inspect evidence", prepared, current_plan, (*history, latest)))
    sent = gateway.calls[0]
    assert sum(message.content.count("LATEST-UNIQUE-EVIDENCE") for message in sent) == 1
    assert model_input_tokens(sent) <= 2000


@pytest.mark.asyncio
async def test_unfittable_latest_observation_prevents_gateway_call() -> None:
    latest = Observation(str(uuid4()), "shell", True, "X" * 9000, None, None)
    policy = manager(1000, observations=0)
    gateway = CaptureGateway()
    with pytest.raises(ContextError) as caught:
        prepared = await policy.prepare_agent_context(
            working_context=context(), plan=plan(), observations=(latest,),
            conversation_turns=(),
        )
        await JsonAgentDecisionAdapter(
            cast(ModelGateway, gateway), prepare_input=policy.fit_model_input,
        ).decide(AgentDecisionRequest("Inspect evidence", prepared, plan(), (latest,)))
    assert caught.value.code == "CONTEXT_BUILD_FAILED"
    assert gateway.calls == []


@pytest.mark.asyncio
async def test_validation_failure_reaches_agent_and_repair_gateway() -> None:
    current_plan = plan()
    check_id = str(uuid4())
    check = ValidationCheck(
        check_id, 1, ValidationCheckKind.TEST, "shell",
        {"argv": ["pytest", "-q"], "cwd": "."}, "Required test", True,
    )
    tool = ToolResult(
        check_id, "shell", False,
        {"exit_code": 1,
         "stdout": "stdout head " * 1000 + "\nTAIL_STDOUT_SENTINEL",
         "stderr": "stderr head " * 1000 + "\nAssertionError: TAIL_ASSERTION_SENTINEL",
         "output_truncated": False},
        ToolError("COMMAND_EXIT_NONZERO", "safe error", False),
        RiskLevel.SAFE, PolicyDecision.ALLOWED, None, 1,
    )
    executed = ValidationCheckResult(check, ValidationStatus.FAIL, tool, "Test failed")
    validation = ValidationResult(
        (check,), (executed,), ValidationStatus.FAIL, ValidationConfidence.HIGH,
        True, 0, "Validation failed",
    )
    agent_gateway = CaptureGateway()
    await JsonAgentDecisionAdapter(cast(ModelGateway, agent_gateway)).decide(
        AgentDecisionRequest("Inspect evidence", context(), current_plan, (),
                             validation_result=validation)
    )
    repair_response = json.dumps({
        "failure_summary": "Fix assertion",
        "steps": [{"description": "Inspect failure", "tool_name": None,
                   "target_paths": [], "command_argv": None, "command_cwd": None}],
    })
    repair_gateway = CaptureGateway(repair_response)
    await ModelPlanner(cast(ModelGateway, repair_gateway)).create_repair_guidance(
        RepairPlanningRequest("Inspect evidence", context(), current_plan, validation, 1)
    )
    for gateway in (agent_gateway, repair_gateway):
        payload = json.loads(gateway.calls[0][1].content)
        assert "TAIL_ASSERTION_SENTINEL" in gateway.calls[0][1].content
        assert "TAIL_STDOUT_SENTINEL" in gateway.calls[0][1].content
        assert "COMMAND_EXIT_NONZERO" in gateway.calls[0][1].content
        check_evidence = payload["validation"]["checks"][0]
        assert check_evidence["exit_code"] == 1
        assert check_evidence["observation_truncated"] is True
        assert len(check_evidence["stdout"]) == 800
        assert len(check_evidence["stderr"]) == 800
        assert "...[output omitted]..." in check_evidence["stderr"]


@pytest.mark.asyncio
async def test_replan_prompt_contains_safe_denied_action_and_previous_scope() -> None:
    previous = plan()
    action = ToolAction("write_file", {"path": "src/extra.py", "content": "SECRET_CONTENT"})
    tool = result("write_file", {}, success=False, error_code="PLAN_SCOPE_DENIED")
    latest = Observation(
        tool.invocation_id, tool.tool_name, False, _observation_summary(tool, action),
        "PLAN_SCOPE_DENIED", "Action outside approved scope",
    )
    policy = manager()
    prepared = await policy.prepare_agent_context(
        working_context=context(), plan=previous, observations=(latest,),
        conversation_turns=(),
    )
    response = json.dumps({
        "completion_requirement": "WORKSPACE_CHANGE_NOT_REQUIRED",
        "rationale_summary": "Replan",
        "steps": [{"description": "Review scope", "tool_name": None,
                   "target_paths": [], "command_argv": None, "command_cwd": None}],
    })
    gateway = CaptureGateway(response)
    await ModelPlanner(
        cast(ModelGateway, gateway), prepare_input=policy.fit_model_input,
    ).create_plan(PlanningRequest(
        "Inspect evidence", prepared, PlanKind.REPLAN, previous,
        latest.replan_reason, previous.run_id, previous.session_id,
    ))
    payload = json.loads(gateway.calls[0][1].content)
    assert payload["previous_plan"]["id"] == previous.plan_id
    assert payload["approved_scope"]["allowed_write_actions"] == []
    assert "src/extra.py" in gateway.calls[0][1].content
    assert latest.invocation_id in gateway.calls[0][1].content
    assert "SECRET_CONTENT" not in gateway.calls[0][1].content


@pytest.mark.asyncio
async def test_native_capability_names_come_from_registered_metadata() -> None:
    gateway = CaptureGateway()
    await JsonAgentDecisionAdapter(
        cast(ModelGateway, gateway),
        tool_metadata=(
            {"registry_name": "read_file", "source": "native"},
            {"registry_name": "list_files", "source": "native"},
            {"registry_name": "search_files", "source": "native"},
            {"registry_name": "lexical_search", "source": "native"},
            {"registry_name": "edit_file", "source": "native"},
            {"registry_name": "write_file", "source": "native"},
            {"registry_name": "shell", "source": "native"},
        ),
    ).decide(AgentDecisionRequest("Inspect evidence", context(), plan(), ()))
    system = gateway.calls[0][0].content
    for name in ("read_file", "list_files", "search_files", "lexical_search",
                 "edit_file", "write_file", "shell"):
        assert name in system
    assert "glob and grep are not Tool names" in system


def test_validation_projection_prioritizes_required_failures_and_bounds_streams() -> None:
    executed = []
    for sequence in range(1, 7):
        check_id = str(uuid4())
        check = ValidationCheck(
            check_id, sequence, ValidationCheckKind.TEST, "shell",
            {"argv": ["pytest", "-q"], "cwd": "."}, "Required test", True,
        )
        failed = sequence == 6
        tool = ToolResult(
            check_id, "shell", not failed,
            {"exit_code": 1 if failed else 0,
             "stdout": "S" * 5000 if failed else "",
             "stderr": "E" * 5000 if failed else "",
             "output_truncated": False},
            ToolError("COMMAND_EXIT_NONZERO", "safe error", False) if failed else None,
            RiskLevel.SAFE, PolicyDecision.ALLOWED, None, 1,
        )
        executed.append(ValidationCheckResult(
            check, ValidationStatus.FAIL if failed else ValidationStatus.PASS,
            tool, "Test failed" if failed else "Test passed",
        ))
    validation = ValidationResult(
        tuple(item.check for item in executed), tuple(executed),
        ValidationStatus.FAIL, ValidationConfidence.HIGH, True, 0,
        "Validation failed",
    )
    projection = _validation_evidence(validation)
    checks = cast(list[dict[str, object]], projection["checks"])
    assert len(checks) == 4
    assert checks[0]["check_id"] == executed[-1].check.check_id
    assert checks[0]["status"] == "FAIL"
    assert checks[0]["observation_truncated"] is True
    assert len(cast(str, checks[0]["stdout"])) == 800
    assert len(cast(str, checks[0]["stderr"])) == 800
    assert projection["checks_omitted"] == 2


def test_shell_and_lexical_observations_are_bounded() -> None:
    shell = result(
        "shell",
        {"exit_code": 0, "stdout": "S" * 10000, "stderr": "E" * 10000,
         "output_truncated": False},
    )
    shell_evidence = _observation_summary(shell)
    assert len(shell_evidence) <= 4096
    assert "observation_truncated=true" in shell_evidence
    assert "stdout:" in shell_evidence and "stderr:" in shell_evidence
    lexical = result(
        "lexical_search",
        {"matches": [{"path": "src/a.py", "line": 7, "column": 1,
                      "text": "M" * 10000}], "truncated": False},
    )
    lexical_evidence = _observation_summary(lexical)
    assert len(lexical_evidence) <= 4096
    assert "observation_truncated=true" in lexical_evidence
    assert '"line":7' in lexical_evidence


def test_replan_observations_exclude_prior_raw_tool_content() -> None:
    earlier = Observation(
        str(uuid4()), "shell", True, "stdout: SECRET_API_KEY_VALUE", None, None,
    )
    latest = Observation(
        str(uuid4()), "write_file", False,
        "write_file failed with PLAN_SCOPE_DENIED; target=path=src/extra.py",
        "PLAN_SCOPE_DENIED", "Outside approved scope",
    )
    projected = _safe_replan_observations((earlier, latest))
    assert "SECRET_API_KEY_VALUE" not in projected[0].evidence_summary
    assert projected[0].tool_name == "shell"
    assert projected[-1].evidence_summary == latest.evidence_summary


def test_replan_scope_projection_does_not_copy_command_arguments() -> None:
    step = PlanStep(
        str(uuid4()), 1, "Validate", "shell", (),
        ("pytest", "--token=SECRET_API_KEY_VALUE"), ".",
        PlanStepStatus.PENDING,
    )
    scope = AuthorizationScope((), ((("pytest", "--token=SECRET_API_KEY_VALUE"), "."),))
    plan_id = str(uuid4())
    previous = Plan(
        plan_id, str(uuid4()), str(uuid4()), 1, PlanKind.INITIAL,
        PlanStatus.CREATED, ApprovalDecision.PENDING, (step,), scope,
        "Validate", None, None, compute_scope_digest(plan_id, 1, scope),
        datetime.now(UTC), None,
    )
    payload = _planning_payload(PlanningRequest(
        "Inspect evidence", context(), PlanKind.REPLAN, previous,
        "Action outside approved scope", previous.run_id, previous.session_id,
    ))
    assert "SECRET_API_KEY_VALUE" not in payload
    assert "executable=pytest argv_count=2" in payload


def test_read_observation_is_bounded_with_long_path_and_large_line() -> None:
    tool = result(
        "read_file",
        {"path": "directory/" + "p" * 1200, "start_line": 123456789,
         "end_line": 123456789, "truncated": True,
         "content": "Z" * 10000},
    )
    evidence = _observation_summary(tool)
    assert len(evidence) <= 4096
    assert "line_content_truncated=true" in evidence
    assert "observation_truncated=true" in evidence
    assert "continuation_unavailable=true" in evidence
    assert "next_start_line=None" in evidence


def test_shared_bounded_output_preserves_short_text_and_failure_tail() -> None:
    assert bounded_output("short output\n", 800) == ("short output\n", False)
    assert bounded_output("", 800) == ("", False)
    text = "HEAD_SENTINEL" + "X" * 5000 + "TAIL_ASSERTION_SENTINEL"
    projected, truncated = bounded_output(text, 800)
    assert truncated
    assert len(projected) == 800
    assert projected.startswith("HEAD_SENTINEL")
    assert projected.endswith("TAIL_ASSERTION_SENTINEL")
    head, tail = projected.split("\n...[output omitted]...\n")
    assert len(tail) >= len(head)
