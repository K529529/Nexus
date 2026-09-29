from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import cast
from uuid import uuid4

import pytest

from nexus.application.agent_input_profile import project_agent_model_input
from nexus.application.execution_context import bind_execution_context
from nexus.application.execution_ledger import ToolExecutionLedger
from nexus.application.observed_model_gateway import ObservedModelGateway
from nexus.application.planning import JsonAgentDecisionAdapter
from nexus.application.telemetry import EventEnricher
from nexus.application.telemetry_subscriber import TelemetrySubscriber
from nexus.application.tool_runtime import ToolRuntime
from nexus.application.tracing_dispatcher import SafeTracerDispatcher
from nexus.context.chunking import LineWindowChunker
from nexus.context.manager import BoundedContextManager, model_input_tokens
from nexus.context.retrieval import ToolRepositoryAccess
from nexus.domain.agent_decision import AgentDecisionRequest, Observation
from nexus.domain.context import ContextBudget
from nexus.domain.exploration import SelectedFileContext, WorkingContext
from nexus.domain.model import ModelMessage, ModelResponse
from nexus.domain.observability import RunExecutionContext
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
from nexus.domain.runtime_events import AgentModelInputProfiled, RuntimeEvent
from nexus.domain.tooling import ApprovalDecision
from nexus.interfaces.cli.profile import ExecutionProfile

_SENTINEL = "SUPER_PRIVATE_SENTINEL"
_TOOLS = "read_file search_files lexical_search edit_file write_file shell"


class CaptureGateway:
    def __init__(self, *responses: str) -> None:
        self.responses = list(responses) or [
            '{"kind":"TASK_READY","summary":"ready","action":null}'
        ]
        self.calls: list[tuple[ModelMessage, ...]] = []

    async def complete(self, messages: tuple[ModelMessage, ...]) -> ModelResponse:
        self.calls.append(tuple(messages))
        return ModelResponse(self.responses.pop(0))


def _header(start: int, end: int, *, path: str = "a.py") -> str:
    return (
        f"read_file {path} start_line={start} end_line={end + 100} truncated=true "
        f"visible_start_line={start} visible_end_line={end} "
        f"observation_truncated=true next_start_line={end + 1} "
        "line_content_truncated=false continuation_unavailable=false:\n"
        f"def secret(): return '{_SENTINEL}'\n"
    )


def _observation(start: int, end: int) -> dict[str, object]:
    return {
        "invocation_id": str(uuid4()), "tool_name": "read_file", "success": True,
        "error_code": None, "target_path": "a.py",
        "evidence_summary": _header(start, end),
    }


def _messages(
    observations: list[dict[str, object]], *, compacted: str | None = None,
    selected: list[dict[str, object]] | None = None,
) -> tuple[ModelMessage, ...]:
    payload = {
        "plan": {
            "id": str(uuid4()), "version": 2,
            "completion_requirement": "WORKSPACE_CHANGE_REQUIRED",
            "steps": [
                {"tool_name": "read_file"}, {"tool_name": "edit_file"},
            ],
        },
        "selected_files": selected or [],
        "observations": observations,
        "compacted_observations": compacted,
    }
    return (
        ModelMessage("system", _TOOLS + " " + _SENTINEL),
        ModelMessage("user", json.dumps(payload)),
    )


def _profile_text(messages: tuple[ModelMessage, ...]) -> str:
    return "\n".join(project_agent_model_input(messages, max_model_input_tokens=24000))


def _plan(run_id: str, session_id: str) -> Plan:
    scope = AuthorizationScope((), ())
    step = PlanStep(
        str(uuid4()), 1, "Inspect a.py", "read_file", (), None, None,
        PlanStepStatus.PENDING,
    )
    plan_id = str(uuid4())
    return Plan(
        plan_id, run_id, session_id, 1, PlanKind.INITIAL,
        PlanStatus.CREATED, ApprovalDecision.PENDING, (step,), scope,
        "Inspect a.py", None, None, compute_scope_digest(plan_id, 1, scope),
        datetime.now(UTC), None, CompletionRequirement.WORKSPACE_CHANGE_REQUIRED,
    )


def _execution(run_id: str, session_id: str) -> RunExecutionContext:
    return RunExecutionContext(
        trace_id=run_id, execution_id=str(uuid4()), run_id=run_id,
        session_id=session_id, is_resume=False, started_at=datetime.now(UTC),
    )


@pytest.mark.parametrize(
    ("ranges", "merged"),
    [
        ([(97, 187), (184, 276)], "97-276"),
        ([(1, 99), (100, 187)], "1-187"),
        ([(1, 99), (184, 276)], "1-99,184-276"),
    ],
)
def test_recent_read_coverage_merges_only_overlapping_or_adjacent_intervals(
    ranges: list[tuple[int, int]], merged: str,
) -> None:
    messages = _messages([_observation(start, end) for start, end in ranges])
    text = _profile_text(messages)
    windows = ",".join(f"{start}-{end}" for start, end in ranges)
    assert f"a.py windows={windows} merged={merged}" in text
    assert "recent_reads=2" in text
    assert "target_paths_with_multiple_windows=1" in text
    assert _SENTINEL not in text


def test_selected_and_compacted_coverage_remain_separate() -> None:
    compacted = (
        "read_file: success=True; error=None; evidence="
        + _header(97, 187).splitlines()[0]
        + "; replan=None"
    )
    selected = [{
        "path": "a.py", "content": _SENTINEL + "\nmore code\n",
        "discovery_reason": "selected lines 1-93", "truncated": False,
    }]
    text = _profile_text(
        _messages([_observation(184, 276)], compacted=compacted, selected=selected)
    )
    assert "selected_file_coverage:\n    a.py windows=1-93 merged=1-93" in text
    assert "recent_read_coverage:\n    a.py windows=184-276 merged=184-276" in text
    assert "compacted read_file path=a.py visible=97-187 compacted=true" in text
    assert "compacted_contains_read_count=1" in text
    assert "merged=97-276" not in text
    assert "a.py lines=1-93" in text
    assert _SENTINEL not in text


def test_latest_occurrence_tool_visibility_and_input_budget() -> None:
    messages = _messages([_observation(97, 187)])
    latest_id = json.loads(messages[1].content)["observations"][-1]["invocation_id"]
    text = _profile_text(messages)
    assert f"latest_observation: id={latest_id[:8]} occurrences=1" in text
    assert "latest_observation_present_once=yes" in text
    assert "tool_visible: read_file=yes search_files=yes lexical_search=yes" in text
    assert "edit_file=yes write_file=yes shell=yes" in text
    assert "completion=WORKSPACE_CHANGE_REQUIRED plan_version=2 plan_step_count=2" in text
    assert "plan_tools: read_file, edit_file" in text
    assert f"estimated_tokens={model_input_tokens(messages)}" in text
    assert "max_model_input_tokens=24000" in text


def test_profile_projects_bounded_plan_progress_from_final_payload() -> None:
    messages = list(_messages([]))
    payload = json.loads(messages[1].content)
    payload["plan"]["active_step"] = {"sequence": 2}
    payload["plan"]["steps"] = [
        {"sequence": 1, "status": "COMPLETED", "tool_name": "read_file"},
        {"sequence": 2, "status": "IN_PROGRESS", "tool_name": "edit_file"},
        {"sequence": 3, "status": "PENDING", "tool_name": "shell"},
    ]
    messages[1] = ModelMessage("user", json.dumps(payload))
    profile = _profile_text(tuple(messages))

    assert (
        "plan_progress: active=2 "
        "statuses=1:COMPLETED,2:IN_PROGRESS,3:PENDING omitted=0"
    ) in profile
    assert "Inspect a.py" not in profile


@pytest.mark.asyncio
async def test_observed_gateway_profiles_actual_post_fit_agent_messages() -> None:
    run_id, session_id = str(uuid4()), str(uuid4())
    plan = _plan(run_id, session_id)
    old = Observation(str(uuid4()), "read_file", True, _header(97, 187) + "X" * 4000, None, None)
    latest = Observation(str(uuid4()), "read_file", True, _header(184, 276), None, None)
    context = WorkingContext(
        "Inspect a.py", (), (), (),
        (SelectedFileContext("a.py", "X" * 12000, (), "lines 1-400", False),), False,
    )
    policy = BoundedContextManager(
        cast(ContextProvider, object()),
        ToolRepositoryAccess(cast(ToolRuntime, object())),
        LineWindowChunker(), ContextBudget(12, 6, 1800, 2), 2200,
    )
    prepared = await policy.prepare_agent_context(
        working_context=context, plan=plan, observations=(old, latest),
        conversation_turns=(),
    )
    raw = CaptureGateway()
    events: list[RuntimeEvent] = []

    async def emit(event: RuntimeEvent) -> None:
        events.append(event)

    ledger = ToolExecutionLedger()
    ledger.begin_step(run_id)
    observed = ObservedModelGateway(
        cast(ModelGateway, raw), emit=emit, ledger=ledger,
        provider=None, model=None,
        local_profile_input_diagnostics=True, max_model_input_tokens=2200,
    )
    with bind_execution_context(_execution(run_id, session_id)):
        await JsonAgentDecisionAdapter(
            cast(ModelGateway, observed), prepare_input=policy.fit_model_input,
        ).decide(AgentDecisionRequest("Inspect a.py", prepared, plan, (old, latest)))

    final_messages = raw.calls[0]
    final_payload = json.loads(final_messages[1].content)
    profiled = [event for event in events if isinstance(event, AgentModelInputProfiled)]
    assert len(profiled) == 1
    assert len(final_payload["observations"]) < 2
    assert final_payload["selected_files"] == []
    assert profiled[0].diagnostic_lines == project_agent_model_input(
        final_messages, max_model_input_tokens=2200,
    )
    text = "\n".join(profiled[0].diagnostic_lines)
    assert "recent_read_coverage:" in text
    assert "a.py windows=184-276 merged=184-276" in text
    assert "windows=97-187,184-276" not in text
    assert "compacted_contains_read_count=1" in text
    assert "occurrences=1" in text


@pytest.mark.asyncio
async def test_structured_retry_keeps_step_and_increments_attempt() -> None:
    run_id, session_id = str(uuid4()), str(uuid4())
    ledger = ToolExecutionLedger()
    ledger.begin_step(run_id)
    raw = CaptureGateway(
        "not-json", '{"kind":"TASK_READY","summary":"ready","action":null}',
    )
    events: list[RuntimeEvent] = []

    async def emit(event: RuntimeEvent) -> None:
        events.append(event)

    observed = ObservedModelGateway(
        cast(ModelGateway, raw), emit=emit, ledger=ledger,
        provider=None, model=None, local_profile_input_diagnostics=True,
    )
    plan = _plan(run_id, session_id)
    context = WorkingContext("Inspect a.py", (), (), (), (), False)
    with bind_execution_context(_execution(run_id, session_id)):
        await JsonAgentDecisionAdapter(cast(ModelGateway, observed)).decide(
            AgentDecisionRequest("Inspect a.py", context, plan, ())
        )
    profiled = [event for event in events if isinstance(event, AgentModelInputProfiled)]
    assert len(raw.calls) == len(profiled) == 2
    assert profiled[0].agent_step_count == profiled[1].agent_step_count == 1
    assert profiled[0].model_call_id != profiled[1].model_call_id
    profile = ExecutionProfile()
    for event in events:
        profile.observe(event)
    rendered = "\n".join(profile.lines())
    assert "Step 1 attempt 1" in rendered
    assert "Step 1 attempt 2" in rendered
    assert "Step 2 attempt" not in rendered


@pytest.mark.asyncio
async def test_diagnostics_are_local_only_and_never_serialize_secret() -> None:
    messages = _messages([_observation(97, 187)], selected=[{
        "path": "a.py", "content": _SENTINEL,
        "discovery_reason": "lines 1-10", "truncated": False,
    }])
    payload = json.loads(messages[1].content)
    payload["observations"][0]["error_code"] = _SENTINEL
    payload["observations"][0]["target_path"] = _SENTINEL + ".py"
    messages = (messages[0], ModelMessage("user", json.dumps(payload)))
    lines = project_agent_model_input(messages, max_model_input_tokens=24000)
    event = AgentModelInputProfiled(
        run_id=str(uuid4()), session_id=None, model_call_id=str(uuid4()),
        agent_step_count=1, diagnostic_lines=lines,
    )
    profile = ExecutionProfile()
    profile.observe(event)
    assert _SENTINEL not in "\n".join(profile.lines())
    assert _SENTINEL not in json.dumps(event.to_dict())
    assert "diagnostic_lines" not in event.to_dict()["payload"]
    assert _SENTINEL not in "\n".join(event.diagnostic_lines)

    class FailEnricher:
        def enrich(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("Remote telemetry should not see profile input events.")

    class FailDispatcher:
        def record(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("Remote telemetry should not record profile input events.")

    subscriber = TelemetrySubscriber(
        cast(EventEnricher, FailEnricher()), cast(SafeTracerDispatcher, FailDispatcher()),
    )
    await subscriber.on_event(event)
    assert "Agent model inputs" in profile.lines()


def test_diagnostic_parse_error_is_metadata_only() -> None:
    messages = (ModelMessage("system", _SENTINEL), ModelMessage("user", "{not-json"))
    text = _profile_text(messages)
    assert "diagnostic_parse_error=true" in text
    assert _SENTINEL not in text


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_profile_parse_failure_is_fail_open_and_disabled_by_default(
    monkeypatch: pytest.MonkeyPatch, enabled: bool,
) -> None:
    run_id, session_id = str(uuid4()), str(uuid4())
    ledger = ToolExecutionLedger()
    ledger.begin_step(run_id)
    raw = CaptureGateway()
    events: list[RuntimeEvent] = []

    async def emit(event: RuntimeEvent) -> None:
        events.append(event)

    def broken_projection(*args: object, **kwargs: object) -> tuple[str, ...]:
        raise RuntimeError(_SENTINEL)

    monkeypatch.setattr(
        "nexus.application.observed_model_gateway.project_agent_model_input",
        broken_projection,
    )
    observed = ObservedModelGateway(
        cast(ModelGateway, raw), emit=emit, ledger=ledger,
        provider=None, model=None, local_profile_input_diagnostics=enabled,
    )
    with bind_execution_context(_execution(run_id, session_id)):
        await JsonAgentDecisionAdapter(cast(ModelGateway, observed)).decide(
            AgentDecisionRequest(
                "Inspect a.py", WorkingContext("Inspect a.py", (), (), (), (), False),
                _plan(run_id, session_id), (),
            )
        )

    assert len(raw.calls) == 1
    profiled = [event for event in events if isinstance(event, AgentModelInputProfiled)]
    assert len(profiled) == int(enabled)
    if enabled:
        assert profiled[0].diagnostic_lines == ("  diagnostic_parse_error=true",)
    assert _SENTINEL not in str([event.to_dict() for event in events])


def test_profile_keeps_every_agent_model_input_beyond_timeline_limit() -> None:
    profile = ExecutionProfile()
    run_id = str(uuid4())
    for step in range(1, 32):
        profile.observe(AgentModelInputProfiled(
            run_id=run_id, session_id=None, model_call_id=str(uuid4()),
            agent_step_count=step, diagnostic_lines=("  diagnostic_parse_error=true",),
        ))
    rendered = "\n".join(profile.lines())
    assert "Step 1 attempt 1" in rendered
    assert "Step 31 attempt 1" in rendered
    assert rendered.count("diagnostic_parse_error=true") == 31
