"""Phase 1 safety, progress and profiling regression tests."""

from __future__ import annotations

import asyncio
import io
import json
import sys
import time
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from pytest import CaptureFixture, MonkeyPatch

from nexus.application.event_publisher import PublishedObservation
from nexus.application.planning import ModelPlanner
from nexus.application.telemetry import EventEnricher
from nexus.domain.exploration import WorkingContext
from nexus.domain.model import ModelCallPhase, ModelChunk, ModelMessage, ModelResponse, TokenUsage
from nexus.domain.observability import RunExecutionContext
from nexus.domain.planning import PlanKind
from nexus.domain.ports.planning import PlanningRequest
from nexus.domain.runtime_events import (
    ApprovalRequested,
    ApprovalSubject,
    ContextBuilt,
    ErrorOccurred,
    ExecutionPhase,
    FinalResult,
    ModelCallFinished,
    ModelCallStarted,
    PhaseFinished,
    PlanCreated,
    RunInterrupted,
    TaskStarted,
    ToolFinished,
    ToolStarted,
    ValidationStarted,
)
from nexus.domain.tooling import PolicyDecision, RiskLevel
from nexus.domain.validation import ValidationCheckKind
from nexus.errors import ModelError
from nexus.interfaces.cli.profile import ExecutionProfile
from nexus.interfaces.cli.renderer import ProgressRenderer, render_event


class OneShotGateway:
    def __init__(self, response: str | Exception) -> None:
        self.response = response
        self.calls = 0

    async def complete(self, messages: Sequence[ModelMessage]) -> ModelResponse:
        del messages
        self.calls += 1
        if isinstance(self.response, Exception):
            raise self.response
        return ModelResponse(self.response)

    async def stream(self, messages: Sequence[ModelMessage]) -> AsyncIterator[ModelChunk]:
        del messages
        if False:
            yield ModelChunk("")


class TtyBuffer(io.StringIO):
    def isatty(self) -> bool:
        return True


def _request() -> PlanningRequest:
    return PlanningRequest(
        "edit alpha",
        WorkingContext("edit alpha", (), (), ("alpha.py",), (), False),
        PlanKind.INITIAL,
        None,
        None,
        str(uuid4()),
        str(uuid4()),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "category"),
    [
        ("secret model output not JSON", "JSON_DECODE"),
        ('{"rationale_summary":"ok"}', "TOP_LEVEL_KEYS"),
        ('{"rationale_summary":"ok","steps":[]}', "STEPS_SCHEMA"),
        (
            json.dumps(
                {
                    "rationale_summary": "ok",
                    "steps": [
                        {
                            "description": "edit",
                            "tool_name": "apply_patch",
                            "target_paths": ["alpha.py", "beta.py"],
                            "command_argv": None,
                            "command_cwd": None,
                        }
                    ],
                }
            ),
            "EDIT_TARGET_COUNT",
        ),
    ],
)
async def test_invalid_plan_has_safe_category_after_retry_without_raw_output(
    content: str, category: str
) -> None:
    gateway = OneShotGateway(content)
    with pytest.raises(ModelError) as failure:
        await ModelPlanner(gateway).create_plan(_request())
    assert gateway.calls == 2
    assert failure.value.code == "INVALID_PLAN_OUTPUT"
    assert failure.value.failure_category == category
    assert content not in str(failure.value)


@pytest.mark.asyncio
async def test_unexpected_parser_exception_is_safely_normalized() -> None:
    content = json.dumps(
        {
            "rationale_summary": "ok",
            "steps": [
                {
                    "description": "test",
                    "tool_name": "shell",
                    "target_paths": [],
                    "command_argv": ["pytest"],
                    "command_cwd": ".",
                }
            ],
        }
    )

    def fail_normalization(argv: list[str]) -> list[str]:
        del argv
        raise RuntimeError("private exception text")

    with pytest.raises(ModelError) as failure:
        await ModelPlanner(OneShotGateway(content), normalize_argv=fail_normalization).create_plan(
            _request()
        )
    assert failure.value.failure_category == "UNEXPECTED"
    assert "private exception text" not in str(failure.value)


def test_error_telemetry_retains_allowlisted_category() -> None:
    run_id = str(uuid4())
    context = RunExecutionContext(
        trace_id=run_id,
        execution_id=str(uuid4()),
        run_id=run_id,
        session_id=None,
        is_resume=False,
        started_at=datetime.now(UTC),
    )
    error = ErrorOccurred(
        run_id=run_id,
        session_id=None,
        code="INVALID_PLAN_OUTPUT",
        message="The model returned an invalid Plan.",
        retryable=True,
        failure_category="STEP_SCHEMA",
    )
    telemetry = EventEnricher(Path.cwd()).enrich(
        error, PublishedObservation(context, 1)
    )
    assert telemetry.payload["failure_category"] == "STEP_SCHEMA"
    assert "message" not in telemetry.payload
    with pytest.raises(ValueError, match="Unknown safe failure category"):
        ErrorOccurred(
            run_id=run_id,
            session_id=None,
            code="INVALID_PLAN_OUTPUT",
            message="safe",
            retryable=True,
            failure_category="private exception text",
        )


def test_default_renderer_hides_low_level_events_but_keeps_terminal_output(
    capsys: CaptureFixture[str],
) -> None:
    run_id = str(uuid4())
    call_id = str(uuid4())
    tool_id = str(uuid4())
    events = [
        ModelCallStarted(
            run_id=run_id,
            session_id=None,
            model_call_id=call_id,
            phase=ModelCallPhase.PLAN,
            provider=None,
            model=None,
        ),
        ModelCallFinished(
            run_id=run_id,
            session_id=None,
            model_call_id=call_id,
            phase=ModelCallPhase.PLAN,
            success=True,
            duration_ms=1200,
            usage=TokenUsage.unavailable(),
            error_code=None,
        ),
        ToolStarted(
            run_id=run_id,
            session_id=None,
            invocation_id=tool_id,
            tool_name="read_file",
            risk_level=RiskLevel.SAFE,
        ),
        ToolFinished(
            run_id=run_id,
            session_id=None,
            invocation_id=tool_id,
            tool_name="read_file",
            success=True,
            risk_level=RiskLevel.SAFE,
            policy_decision=PolicyDecision.ALLOWED,
            approval_decision=None,
            duration_ms=25,
            error_code=None,
        ),
    ]
    for event in events:
        assert render_event(event)
    assert capsys.readouterr().out == ""
    assert render_event(TaskStarted(run_id=run_id, session_id=None, task="explain"))
    assert render_event(FinalResult(run_id=run_id, session_id=None, content="answer"))
    output = capsys.readouterr().out
    assert "Task started" not in output
    assert "answer" in output
    error = ErrorOccurred(
        run_id=run_id,
        session_id=None,
        code="INVALID_PLAN_OUTPUT",
        message="safe failure",
        retryable=True,
    )
    assert not render_event(error)
    assert "safe failure" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_progress_heartbeat_updates_during_a_slow_model_call(
    monkeypatch: MonkeyPatch,
) -> None:
    output = TtyBuffer()
    monkeypatch.setattr(sys, "stdout", output)
    renderer = ProgressRenderer()
    renderer.start()
    renderer.render(
        ModelCallStarted(
            run_id=str(uuid4()),
            session_id=None,
            model_call_id=str(uuid4()),
            phase=ModelCallPhase.PLAN,
            provider=None,
            model=None,
        )
    )
    await asyncio.sleep(1.1)
    await renderer.stop()
    assert "Thinking... 1s" in output.getvalue()
    assert renderer._heartbeat is None


@pytest.mark.asyncio
async def test_progress_elapsed_updates_while_file_work_blocks_event_loop(
    monkeypatch: MonkeyPatch,
) -> None:
    output = TtyBuffer()
    monkeypatch.setattr(sys, "stdout", output)
    renderer = ProgressRenderer()
    renderer.start()
    try:
        renderer.render(
            ToolStarted(
                run_id=str(uuid4()), session_id=None,
                invocation_id=str(uuid4()), tool_name="read_file",
                risk_level=RiskLevel.SAFE,
            )
        )
        time.sleep(1.5)  # noqa: ASYNC251 - reproduce synchronous Tool work
        assert "Reading relevant files... 1s" in output.getvalue()
        assert renderer._live_line
    finally:
        await renderer.stop()


@pytest.mark.asyncio
async def test_hidden_events_keep_live_status_visible(monkeypatch: MonkeyPatch) -> None:
    output = TtyBuffer()
    monkeypatch.setattr(sys, "stdout", output)
    run_id = str(uuid4())
    call_id = str(uuid4())
    renderer = ProgressRenderer()
    renderer.start()
    renderer.render(
        ModelCallStarted(
            run_id=run_id, session_id=None, model_call_id=call_id,
            phase=ModelCallPhase.PLAN, provider=None, model=None,
        )
    )
    before = output.getvalue()
    renderer.render(
        ModelCallFinished(
            run_id=run_id, session_id=None, model_call_id=call_id,
            phase=ModelCallPhase.PLAN, success=True, duration_ms=10,
            usage=TokenUsage.unavailable(), error_code=None,
        )
    )
    renderer.render(
        ToolFinished(
            run_id=run_id, session_id=None, invocation_id=str(uuid4()),
            tool_name="read_file", success=True, risk_level=RiskLevel.SAFE,
            policy_decision=PolicyDecision.ALLOWED, approval_decision=None,
            duration_ms=10, error_code=None,
        )
    )
    assert output.getvalue() == before
    assert before.endswith("s\x1b[K")
    assert renderer._live_line
    await renderer.stop()


@pytest.mark.asyncio
async def test_status_transitions_redraw_without_waiting_for_heartbeat(
    monkeypatch: MonkeyPatch,
) -> None:
    output = TtyBuffer()
    monkeypatch.setattr(sys, "stdout", output)
    run_id = str(uuid4())
    renderer = ProgressRenderer()
    renderer.start()
    events = (
        ModelCallStarted(
            run_id=run_id, session_id=None, model_call_id=str(uuid4()),
            phase=ModelCallPhase.PLAN, provider=None, model=None,
        ),
        ToolStarted(
            run_id=run_id, session_id=None, invocation_id=str(uuid4()),
            tool_name="read_file", risk_level=RiskLevel.SAFE,
        ),
        ToolStarted(
            run_id=run_id, session_id=None, invocation_id=str(uuid4()),
            tool_name="apply_patch", risk_level=RiskLevel.WRITE,
        ),
        ValidationStarted(
            run_id=run_id, session_id=None, check_ids=("check-1",),
            check_kinds=(ValidationCheckKind.TEST,),
        ),
    )
    for event, label in zip(
        events, ("Thinking", "Reading relevant files", "Editing files", "Validating"),
        strict=True,
    ):
        renderer.render(event)
        assert output.getvalue().endswith(f"\r{label}... 0s\x1b[K")
    assert "\r\x1b[K" not in output.getvalue()
    await renderer.stop()


@pytest.mark.asyncio
async def test_non_tty_hidden_status_changes_do_not_spam_output(
    capsys: CaptureFixture[str],
) -> None:
    run_id = str(uuid4())
    renderer = ProgressRenderer()
    renderer.start()
    renderer.render(
        ModelCallStarted(
            run_id=run_id, session_id=None, model_call_id=str(uuid4()),
            phase=ModelCallPhase.PLAN, provider=None, model=None,
        )
    )
    renderer.render(
        ToolStarted(
            run_id=run_id, session_id=None, invocation_id=str(uuid4()),
            tool_name="read_file", risk_level=RiskLevel.SAFE,
        )
    )
    assert capsys.readouterr().out == ""
    await renderer.stop()


@pytest.mark.asyncio
async def test_permanent_output_replaces_live_line_cleanly(monkeypatch: MonkeyPatch) -> None:
    output = TtyBuffer()
    monkeypatch.setattr(sys, "stdout", output)
    run_id = str(uuid4())
    renderer = ProgressRenderer()
    renderer.start()
    renderer.render(
        ContextBuilt(
            run_id=run_id, session_id=None, selected_paths=(),
            retained_characters=0, truncated=False,
        )
    )
    assert "\r\x1b[KContext ready\n\rStarting..." in output.getvalue()
    renderer.render(
        PlanCreated(
            run_id=run_id, session_id=None, plan_id=str(uuid4()), plan_version=1,
            plan_kind=PlanKind.INITIAL, step_summaries=("1. Inspect", "2. Report"),
            replan_reason=None,
        )
    )
    assert "\r\x1b[K  1. Inspect\n  2. Report\n\rStarting..." in output.getvalue()
    await renderer.stop()


@pytest.mark.asyncio
async def test_completion_failure_and_approval_clear_live_line(
    monkeypatch: MonkeyPatch,
) -> None:
    output = TtyBuffer()
    monkeypatch.setattr(sys, "stdout", output)
    run_id = str(uuid4())

    for terminal in (
        FinalResult(run_id=run_id, session_id=None, content="done"),
        ErrorOccurred(
            run_id=run_id, session_id=None, code="MODEL_ERROR",
            message="safe failure", retryable=False,
        ),
    ):
        output.seek(0)
        output.truncate(0)
        renderer = ProgressRenderer()
        renderer.start()
        renderer.render(terminal)
        snapshot = output.getvalue()
        assert not renderer._live_line
        assert not renderer._running
        await asyncio.sleep(1.1)
        assert output.getvalue() == snapshot
        await renderer.stop()

    output.seek(0)
    output.truncate(0)
    renderer = ProgressRenderer()
    renderer.start()
    renderer.render(
        ApprovalRequested(
            run_id=run_id, session_id=None, approval_id=str(uuid4()),
            invocation_id=None, operation="approve_plan", risk_level=RiskLevel.WRITE,
            resource_or_command_summary="scope", subject=ApprovalSubject.PLAN,
            plan_id=str(uuid4()), plan_version=1,
        )
    )
    assert renderer._live_line
    await renderer.stop()
    snapshot = output.getvalue()
    assert snapshot.endswith("\r\x1b[K")
    assert not renderer._live_line
    await asyncio.sleep(1.1)
    assert output.getvalue() == snapshot


def test_profile_aggregates_deterministically_and_handles_failure_interrupt() -> None:
    run_id = str(uuid4())
    started = datetime(2026, 1, 1, tzinfo=UTC)
    profile = ExecutionProfile()
    profile.observe(TaskStarted(run_id=run_id, session_id=None, task="work", timestamp=started))
    profile.observe(
        PhaseFinished(
            run_id=run_id,
            session_id=None,
            phase=ExecutionPhase.REPOSITORY,
            duration_ms=250,
            success=True,
        )
    )
    call_id = str(uuid4())
    profile.observe(
        ModelCallStarted(
            run_id=run_id,
            session_id=None,
            model_call_id=call_id,
            phase=ModelCallPhase.AGENT_STEP,
            provider=None,
            model=None,
        )
    )
    profile.observe(
        ModelCallFinished(
            run_id=run_id,
            session_id=None,
            model_call_id=call_id,
            phase=ModelCallPhase.AGENT_STEP,
            success=True,
            duration_ms=600,
            usage=TokenUsage.unavailable(),
            error_code=None,
        )
    )
    tool_id = str(uuid4())
    profile.observe(
        ToolStarted(
            run_id=run_id,
            session_id=None,
            invocation_id=tool_id,
            tool_name="search_files",
            risk_level=RiskLevel.SAFE,
        )
    )
    profile.observe(
        ToolFinished(
            run_id=run_id,
            session_id=None,
            invocation_id=tool_id,
            tool_name="search_files",
            success=True,
            risk_level=RiskLevel.SAFE,
            policy_decision=PolicyDecision.ALLOWED,
            approval_decision=None,
            duration_ms=900,
            error_code=None,
        )
    )
    profile.observe(
        ErrorOccurred(
            run_id=run_id,
            session_id=None,
            code="INVALID_PLAN_OUTPUT",
            message="safe",
            retryable=True,
            failure_category="PLAN_SCHEMA",
            timestamp=started + timedelta(seconds=2),
        )
    )
    lines = profile.lines()
    assert "Total               2000 ms" in lines
    assert "Repository          250 ms" in lines
    assert "LLM calls           1" in lines
    assert "Tool calls          1" in lines
    assert "Agent steps         1" in lines
    assert "Failure category    PLAN_SCHEMA" in lines
    assert lines.index("search_files            900 ms") < lines.index(
        "AGENT_STEP model        600 ms"
    )
    assert lines == profile.lines()

    interrupted = ExecutionProfile()
    interrupted.observe(TaskStarted(run_id=run_id, session_id=None, task="work", timestamp=started))
    interrupted.observe(
        RunInterrupted(run_id=run_id, session_id=None, timestamp=started + timedelta(seconds=1))
    )
    assert "Total               1000 ms" in interrupted.lines()
