import json
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from nexus.application.event_publisher import PublishedObservation
from nexus.application.telemetry import EventEnricher, TelemetryRedactor
from nexus.domain.model import ModelCallPhase, TokenUsage, UsageAvailability
from nexus.domain.observability import (
    ExecutionOutcome,
    RunExecutionContext,
    TraceRunFinish,
    TraceRunStart,
    TraceValidationStatus,
)
from nexus.domain.planning import TerminalStatus
from nexus.domain.runtime_events import (
    ErrorOccurred,
    ModelCallFinished,
    ModelCallStarted,
    RuntimeStatus,
)
from nexus.infrastructure.observability.console import ConsoleTracer
from nexus.infrastructure.observability.langsmith import LangSmithTracer


def _context() -> RunExecutionContext:
    run_id = str(uuid4())
    return RunExecutionContext(
        trace_id=run_id,
        execution_id=str(uuid4()),
        run_id=run_id,
        session_id=None,
        is_resume=False,
        started_at=datetime.now(UTC),
    )


def _start(context: RunExecutionContext) -> TraceRunStart:
    return TraceRunStart(
        context=context,
        task_character_count=999,
        model_provider="safe-provider",
        model_name="safe-model",
    )


def _finish(context: RunExecutionContext) -> TraceRunFinish:
    return TraceRunFinish(
        context=context,
        finished_at=datetime.now(UTC),
        execution_outcome=ExecutionOutcome.COMPLETED,
        runtime_status=RuntimeStatus.COMPLETED,
        terminal_status=TerminalStatus.SUCCEEDED,
        duration_ms=2,
        step_count=1,
        llm_call_count=1,
        tool_call_count=0,
        replan_count=0,
        repair_count=0,
        token_usage=TokenUsage(1, 2, 3, UsageAvailability.REPORTED),
        changed_file_count=0,
        validation_status=TraceValidationStatus.NOT_RUN,
        error_code=None,
    )


@pytest.mark.asyncio
async def test_console_tracer_writes_only_exact_safe_telemetry_jsonl() -> None:
    context = _context()
    sentinel = "PROMPT_SECRET_NEVER_EXPORT"
    runtime_event = ModelCallFinished(
        run_id=context.run_id,
        session_id=None,
        model_call_id=str(uuid4()),
        phase=ModelCallPhase.PLAN,
        success=True,
        duration_ms=4,
        usage=TokenUsage.unavailable(),
        error_code=None,
    )
    telemetry = EventEnricher(Path(".")).enrich(
        runtime_event, PublishedObservation(context, 1)
    )
    secret_event = EventEnricher(Path(".")).enrich(
        ErrorOccurred(
            run_id=context.run_id,
            session_id=None,
            code="SAFE_ERROR",
            message=sentinel,
            retryable=False,
        ),
        PublishedObservation(context, 2),
    )
    stream = StringIO()
    tracer = ConsoleTracer(stream=stream)
    await tracer.start_run(_start(context))
    await tracer.record(telemetry)
    await tracer.record(secret_event)
    await tracer.finish(_finish(context))

    line = stream.getvalue()
    parsed = json.loads(line.splitlines()[0])
    assert parsed["event_type"] == "model_call.finished"
    assert parsed["phase"] == "MODEL"
    assert parsed["payload"]["model_call_phase"] == "PLAN"
    assert sentinel not in line
    assert len(line.splitlines()) == 2


class _Client:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...], dict[str, Any]]] = []

    def create_run(
        self, name: str, inputs: dict[str, Any], run_type: str, **kwargs: Any
    ) -> None:
        self.calls.append(("create", (name, inputs, run_type), kwargs))

    def update_run(self, run_id: object, **kwargs: Any) -> None:
        self.calls.append(("update", (run_id,), kwargs))

    def flush(self, timeout: float | None = None) -> None:
        self.calls.append(("flush", (timeout,), {}))

    def close(self, timeout: float | None = None) -> None:
        self.calls.append(("close", (timeout,), {}))


@pytest.mark.asyncio
async def test_langsmith_tracer_uses_manual_empty_input_and_safe_metadata_only() -> None:
    context = _context()
    client = _Client()
    tracer = LangSmithTracer(client, project="safe-project")
    model_call_id = str(uuid4())
    enricher = EventEnricher(Path("."))
    started = enricher.enrich(
        ModelCallStarted(
            run_id=context.run_id,
            session_id=None,
            model_call_id=model_call_id,
            phase=ModelCallPhase.AGENT_STEP,
            provider="safe-provider",
            model="safe-model",
        ),
        PublishedObservation(context, 1),
    )
    finished = enricher.enrich(
        ModelCallFinished(
            run_id=context.run_id,
            session_id=None,
            model_call_id=model_call_id,
            phase=ModelCallPhase.AGENT_STEP,
            success=True,
            duration_ms=1,
            usage=TokenUsage.unavailable(),
            error_code=None,
        ),
        PublishedObservation(context, 2),
    )
    secret_event = enricher.enrich(
        ErrorOccurred(
            run_id=context.run_id,
            session_id=None,
            code="SAFE_ERROR",
            message="PROMPT_SECRET_NEVER_EXPORT",
            retryable=False,
        ),
        PublishedObservation(context, 3),
    )

    await tracer.start_run(_start(context))
    await tracer.record(started)
    await tracer.record(finished)
    await tracer.record(secret_event)
    await tracer.finish(_finish(context))
    await tracer.close()

    representation = repr(client.calls)
    assert "safe-provider" in representation
    assert "safe-model" in representation
    assert "PROMPT_SECRET_NEVER_EXPORT" not in representation
    create_calls = [call for call in client.calls if call[0] == "create"]
    assert all(call[1][1] == {} for call in create_calls)
    root_create, span_create = create_calls
    root_order = root_create[2]["dotted_order"]
    span_order = span_create[2]["dotted_order"]
    assert root_create[2]["trace_id"] == UUID(context.execution_id)
    assert isinstance(root_order, str)
    assert root_order.endswith(context.execution_id)
    assert span_create[2]["trace_id"] == UUID(context.execution_id)
    assert span_create[2]["parent_run_id"] == context.execution_id
    assert isinstance(span_order, str)
    assert span_order.startswith(f"{root_order}.")
    assert span_order.endswith(model_call_id)
    update_calls = [call for call in client.calls if call[0] == "update"]
    assert update_calls
    assert all(call[2]["trace_id"] == UUID(context.execution_id) for call in update_calls)
    assert all(isinstance(call[2]["dotted_order"], str) for call in update_calls)
    span_updates = [call for call in update_calls if call[1][0] == UUID(model_call_id)]
    assert span_updates
    assert all(
        call[2]["parent_run_id"] == context.execution_id for call in span_updates
    )


def test_defense_in_depth_redactor_rejects_credential_shaped_allowed_identifier() -> None:
    context = _context()
    telemetry = EventEnricher(Path(".")).enrich(
        ModelCallStarted(
            run_id=context.run_id,
            session_id=None,
            model_call_id=str(uuid4()),
            phase=ModelCallPhase.PLAN,
            provider="sk-this-must-never-cross",
            model="safe-model",
        ),
        PublishedObservation(context, 1),
    )
    with pytest.raises(ValueError, match="redaction boundary"):
        TelemetryRedactor().redact(telemetry)
