"""Phase 1 safety, progress and profiling regression tests."""

from __future__ import annotations

import asyncio
import io
import json
import sys
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
    ErrorOccurred,
    ExecutionPhase,
    FinalResult,
    ModelCallFinished,
    ModelCallStarted,
    PhaseFinished,
    RunInterrupted,
    TaskStarted,
    ToolFinished,
    ToolStarted,
)
from nexus.domain.tooling import PolicyDecision, RiskLevel
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
    assert "Task started" in output
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
    class TtyBuffer(io.StringIO):
        def isatty(self) -> bool:
            return True

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
