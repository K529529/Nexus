import json
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from nexus.application.event_publisher import InProcessEventPublisher, PublishedObservation
from nexus.application.execution_context import bind_execution_context
from nexus.application.telemetry import EventEnricher, TelemetryRedactor
from nexus.application.telemetry_subscriber import TelemetrySubscriber
from nexus.application.tracing_dispatcher import SafeTracerDispatcher, TracerSink
from nexus.domain.agent_decision import AgentDecisionKind, AgentRuntimeFeedback
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
    AgentSemanticRetryStarted,
    AgentStepCompleted,
    AgentStepStarted,
    ErrorOccurred,
    ModelCallFinished,
    ModelCallStarted,
    PlanStepCompleted,
    RuntimeStatus,
    TaskStarted,
    ToolFinished,
    ToolStarted,
    TraceFallback,
    TraceOperation,
    TraceSink,
)
from nexus.domain.tooling import PolicyDecision, RiskLevel, ToolResult
from nexus.infrastructure.graph.day4_runtime import _observation_summary
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


@pytest.mark.asyncio
async def test_tool_target_summary_stays_out_of_enricher_and_langsmith() -> None:
    context = _context()
    sentinel = "PRIVATE_LOCAL_TARGET"
    runtime_event = ToolStarted(
        run_id=context.run_id,
        session_id=None,
        invocation_id=str(uuid4()),
        tool_name="read_file",
        risk_level=RiskLevel.SAFE,
        target_summary=f"path=src/{sentinel}.py start_line=1 max_lines=400",
    )
    telemetry = EventEnricher(Path(".")).enrich(
        runtime_event, PublishedObservation(context, 1)
    )
    assert "target_summary" not in telemetry.payload
    assert sentinel not in repr(telemetry)

    client = _Client()
    tracer = LangSmithTracer(client, project="safe-project")
    await tracer.start_run(_start(context))
    await tracer.record(telemetry)
    await tracer.finish(_finish(context))
    assert sentinel not in repr(client.calls)
    assert "target_summary" not in repr(client.calls)
    tool_spans = [
        call for call in client.calls
        if call[0] == "create" and call[1][0] == "nexus.tool.read_file"
    ]
    assert len(tool_spans) == 1
    assert tool_spans[0][2]["extra"]["metadata"] == dict(telemetry.payload)


@pytest.mark.asyncio
async def test_read_file_observation_content_stays_out_of_enricher_and_langsmith() -> None:
    context = _context()
    invocation_id = str(uuid4())
    sentinel = "PRIVATE_FILE_CONTENT_NOT_FOR_TELEMETRY"
    result = ToolResult(
        invocation_id=invocation_id,
        tool_name="read_file",
        success=True,
        output={
            "path": "pvlib/iam.py",
            "start_line": 1,
            "end_line": 400,
            "truncated": True,
            "content": sentinel,
        },
        error=None,
        risk_level=RiskLevel.SAFE,
        policy_decision=PolicyDecision.ALLOWED,
        approval_decision=None,
        duration_ms=3,
    )
    summary = _observation_summary(result)
    assert sentinel in summary
    assert "end_line=400 truncated=true" in summary

    telemetry = EventEnricher(Path(".")).enrich(
        ToolFinished(
            run_id=context.run_id,
            session_id=None,
            invocation_id=invocation_id,
            tool_name="read_file",
            success=True,
            risk_level=result.risk_level,
            policy_decision=result.policy_decision,
            approval_decision=result.approval_decision,
            duration_ms=result.duration_ms,
            error_code=None,
        ),
        PublishedObservation(context, 1),
    )
    assert sentinel not in repr(telemetry)
    assert "end_line" not in telemetry.payload
    assert "truncated" not in telemetry.payload
    assert "content" not in telemetry.payload

    client = _Client()
    tracer = LangSmithTracer(client, project="safe-project")
    await tracer.start_run(_start(context))
    await tracer.record(telemetry)
    await tracer.finish(_finish(context))
    assert sentinel not in repr(client.calls)
    assert "end_line=400" not in repr(client.calls)


@pytest.mark.asyncio
async def test_local_agent_retry_events_do_not_enter_telemetry_or_langsmith() -> None:
    context = _context()
    client = _Client()
    warnings: list[str] = []

    async def warn(
        code: str, sink: TraceSink, operation: TraceOperation, fallback: TraceFallback
    ) -> None:
        del sink, operation, fallback
        warnings.append(code)

    tracer = LangSmithTracer(client, project="safe-project")
    dispatcher = SafeTracerDispatcher(
        (TracerSink(TraceSink.LANGSMITH, tracer),), warning_emitter=warn,
    )
    subscriber = TelemetrySubscriber(
        EventEnricher(Path(".")), dispatcher, warning_emitter=warn,
    )
    publisher = InProcessEventPublisher()
    publisher.subscribe(subscriber)
    subscriber.start_execution(_start(context))
    publisher.open_execution(context)
    started = AgentStepStarted(
        run_id=context.run_id, session_id=None, step_count=1,
    )
    retry = AgentSemanticRetryStarted(
        run_id=context.run_id, session_id=None, step_count=1,
        reason=AgentRuntimeFeedback.REPEATED_SUCCESSFUL_READ,
    )

    progress = PlanStepCompleted(
        run_id=context.run_id, session_id=None, plan_id=str(uuid4()),
        plan_version=1, step_id=str(uuid4()), sequence=1,
    )
    with bind_execution_context(context):
        await publisher.publish(TaskStarted(
            run_id=context.run_id, session_id=None, task="safe task",
        ))
        await publisher.publish(started)
        await publisher.publish(retry)
        await publisher.publish(progress)
    subscriber.finish_execution(_finish(context))
    await subscriber.drain_execution(context.execution_id)
    publisher.close_execution(context.execution_id)
    await subscriber.close()

    rendered = repr(client.calls)
    assert "run.started" in rendered
    assert "AgentStepStarted" not in rendered
    assert "AgentSemanticRetryStarted" not in rendered
    assert "PlanStepCompleted" not in rendered
    assert progress.step_id not in rendered
    assert "REPEATED_SUCCESSFUL_READ" not in rendered
    assert "Agent guard" not in rendered
    assert warnings == []
    with pytest.raises(ValueError, match="not registered"):
        EventEnricher(Path(".")).enrich(retry, PublishedObservation(context, 3))
    with pytest.raises(ValueError, match="not registered"):
        EventEnricher(Path(".")).enrich(progress, PublishedObservation(context, 4))


@pytest.mark.asyncio
async def test_agent_decision_summary_stays_out_of_enricher_and_langsmith() -> None:
    context = _context()
    sentinel = "LOCAL_DECISION_ONLY"
    runtime_event = AgentStepCompleted(
        run_id=context.run_id,
        session_id=None,
        step_count=1,
        decision_kind=AgentDecisionKind.CONTINUE,
        model_call_id=str(uuid4()),
        decision_summary=f"Inspect {sentinel} before proceeding.",
    )
    telemetry = EventEnricher(Path(".")).enrich(
        runtime_event, PublishedObservation(context, 1)
    )
    assert "decision_summary" not in telemetry.payload
    assert sentinel not in repr(telemetry)
    assert set(telemetry.payload) == {
        "step_count", "decision_kind", "model_call_id"
    }

    client = _Client()
    tracer = LangSmithTracer(client, project="safe-project")
    await tracer.start_run(_start(context))
    await tracer.record(telemetry)
    await tracer.finish(_finish(context))
    assert sentinel not in repr(client.calls)
    assert "decision_summary" not in repr(client.calls)


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
