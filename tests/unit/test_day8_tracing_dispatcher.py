import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from nexus.application.event_publisher import InProcessEventPublisher, PublishedObservation
from nexus.application.execution_context import bind_execution_context
from nexus.application.telemetry import EventEnricher
from nexus.application.telemetry_subscriber import TelemetrySubscriber
from nexus.application.tracing_dispatcher import (
    SafeTracerDispatcher,
    TracerSink,
)
from nexus.domain.model import TokenUsage
from nexus.domain.observability import (
    ExecutionOutcome,
    RunExecutionContext,
    TelemetryEvent,
    TraceRunFinish,
    TraceRunStart,
    TraceValidationStatus,
)
from nexus.domain.planning import TerminalStatus
from nexus.domain.runtime_events import (
    ErrorOccurred,
    FinalResult,
    ObservabilityWarning,
    RuntimeStatus,
    TaskStarted,
    TraceFallback,
    TraceOperation,
    TraceSink,
)


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
        task_character_count=4,
        model_provider="test",
        model_name="test-model",
    )


def _finish(context: RunExecutionContext) -> TraceRunFinish:
    return TraceRunFinish(
        context=context,
        finished_at=datetime.now(UTC),
        execution_outcome=ExecutionOutcome.COMPLETED,
        runtime_status=RuntimeStatus.COMPLETED,
        terminal_status=TerminalStatus.SUCCEEDED,
        duration_ms=1,
        step_count=0,
        llm_call_count=0,
        tool_call_count=0,
        replan_count=0,
        repair_count=0,
        token_usage=TokenUsage.unavailable(),
        changed_file_count=0,
        validation_status=TraceValidationStatus.NOT_RUN,
        error_code=None,
    )


class _Tracer:
    def __init__(self, *, fail_at: str | None = None) -> None:
        self.calls: list[str] = []
        self.fail_at = fail_at
        self.start_gate: asyncio.Event | None = None

    async def start_run(self, start: TraceRunStart) -> None:
        self.calls.append("start")
        if self.start_gate is not None:
            await self.start_gate.wait()
        if self.fail_at == "start":
            raise RuntimeError("forbidden raw failure")

    async def record(self, event: TelemetryEvent) -> None:
        self.calls.append(f"record:{event.event_type.value}")
        if self.fail_at == "record":
            raise RuntimeError("forbidden raw failure")

    async def finish(self, finish: TraceRunFinish) -> None:
        self.calls.append("finish")
        if self.fail_at == "finish":
            raise RuntimeError("forbidden raw failure")


@pytest.mark.asyncio
async def test_one_worker_preserves_start_records_finish_fifo() -> None:
    context = _context()
    tracer = _Tracer()
    tracer.start_gate = asyncio.Event()
    warnings: list[tuple[str, TraceSink, TraceOperation, TraceFallback]] = []

    async def warn(
        code: str, sink: TraceSink, operation: TraceOperation, fallback: TraceFallback
    ) -> None:
        warnings.append((code, sink, operation, fallback))

    dispatcher = SafeTracerDispatcher(
        (TracerSink(TraceSink.CONSOLE, tracer),), warning_emitter=warn
    )
    event = EventEnricher(Path(".")).enrich(
        TaskStarted(run_id=context.run_id, session_id=None, task="safe"),
        PublishedObservation(context, 1),
    )
    dispatcher.start_execution(_start(context))
    dispatcher.record(event)
    dispatcher.finish_execution(_finish(context))
    await asyncio.sleep(0)
    assert tracer.calls == ["start"]
    tracer.start_gate.set()
    await dispatcher.drain_execution(context.execution_id)
    assert tracer.calls == ["start", "record:run.started", "finish"]
    assert warnings == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fail_at", "expected"),
    (("start", ["start"]), ("record", ["start", "record:error.occurred"])),
)
async def test_start_or_record_failure_disables_sink_without_normal_finish(
    fail_at: str, expected: list[str]
) -> None:
    context = _context()
    tracer = _Tracer(fail_at=fail_at)
    warnings: list[tuple[str, TraceOperation, TraceFallback]] = []

    async def warn(
        code: str, sink: TraceSink, operation: TraceOperation, fallback: TraceFallback
    ) -> None:
        warnings.append((code, operation, fallback))

    dispatcher = SafeTracerDispatcher(
        (TracerSink(TraceSink.LANGSMITH, tracer),), warning_emitter=warn
    )
    event = EventEnricher(Path(".")).enrich(
        ErrorOccurred(
            run_id=context.run_id,
            session_id=None,
            code="SAFE_ERROR",
            message="forbidden secret",
            retryable=False,
        ),
        PublishedObservation(context, 1),
    )
    dispatcher.start_execution(_start(context))
    dispatcher.record(event)
    await asyncio.sleep(0)
    dispatcher.finish_execution(_finish(context))
    await dispatcher.drain_execution(context.execution_id)
    assert tracer.calls == expected
    assert warnings == [
        (
            "TRACE_SINK_FAILED",
            TraceOperation.START if fail_at == "start" else TraceOperation.RECORD,
            TraceFallback.NOOP,
        )
    ]


@pytest.mark.asyncio
async def test_finish_failure_warns_once_and_preserves_prior_records() -> None:
    context = _context()
    tracer = _Tracer(fail_at="finish")
    warnings: list[str] = []

    async def warn(
        code: str, sink: TraceSink, operation: TraceOperation, fallback: TraceFallback
    ) -> None:
        warnings.append(f"{code}:{operation.value}")

    dispatcher = SafeTracerDispatcher(
        (TracerSink(TraceSink.CONSOLE, tracer),), warning_emitter=warn
    )
    event = EventEnricher(Path(".")).enrich(
        TaskStarted(run_id=context.run_id, session_id=None, task="safe"),
        PublishedObservation(context, 1),
    )
    dispatcher.start_execution(_start(context))
    dispatcher.record(event)
    dispatcher.finish_execution(_finish(context))
    await dispatcher.drain_execution(context.execution_id)
    assert tracer.calls == ["start", "record:run.started", "finish"]
    assert warnings == ["TRACE_SINK_FAILED:FINISH"]


@pytest.mark.asyncio
async def test_finish_failure_uses_other_healthy_sink_as_fallback() -> None:
    context = _context()
    console = _Tracer(fail_at="finish")
    langsmith = _Tracer()
    warnings: list[tuple[TraceSink, TraceOperation, TraceFallback]] = []

    async def warn(
        code: str, sink: TraceSink, operation: TraceOperation, fallback: TraceFallback
    ) -> None:
        assert code == "TRACE_SINK_FAILED"
        warnings.append((sink, operation, fallback))

    dispatcher = SafeTracerDispatcher(
        (
            TracerSink(TraceSink.CONSOLE, console),
            TracerSink(TraceSink.LANGSMITH, langsmith),
        ),
        warning_emitter=warn,
    )
    dispatcher.start_execution(_start(context))
    dispatcher.finish_execution(_finish(context))
    await dispatcher.drain_execution(context.execution_id)

    assert console.calls == ["start", "finish"]
    assert langsmith.calls == ["start", "finish"]
    assert warnings == [
        (TraceSink.CONSOLE, TraceOperation.FINISH, TraceFallback.LANGSMITH)
    ]


def test_enricher_omits_adversarial_business_content() -> None:
    context = _context()
    sentinel = "SECRET_PASSWORD_AND_TRACEBACK"
    event = ErrorOccurred(
        run_id=context.run_id,
        session_id=None,
        code="SAFE_ERROR",
        message=sentinel,
        retryable=False,
    )
    telemetry = EventEnricher(Path(".")).enrich(
        event, PublishedObservation(context, 1)
    )
    assert sentinel not in repr(telemetry)
    assert telemetry.payload == {"error_code": "SAFE_ERROR", "retryable": False}
    assert telemetry.redaction.omitted_fields == ("error_message",)


@pytest.mark.asyncio
async def test_terminal_telemetry_is_accepted_before_finish_control() -> None:
    context = _context()
    tracer = _Tracer()
    publisher = InProcessEventPublisher()

    async def warn(
        code: str, sink: TraceSink, operation: TraceOperation, fallback: TraceFallback
    ) -> None:
        raise AssertionError(f"unexpected warning: {code}:{sink}:{operation}:{fallback}")

    dispatcher = SafeTracerDispatcher(
        (TracerSink(TraceSink.CONSOLE, tracer),), warning_emitter=warn
    )
    subscriber = TelemetrySubscriber(EventEnricher(Path(".")), dispatcher)
    publisher.subscribe(subscriber)
    publisher.open_execution(context)
    subscriber.start_execution(_start(context))
    with bind_execution_context(context):
        await publisher.publish(
            TaskStarted(run_id=context.run_id, session_id=None, task="safe")
        )
        await publisher.publish(
            FinalResult(run_id=context.run_id, session_id=None, content="done")
        )
    subscriber.finish_execution(_finish(context))
    await subscriber.drain_execution(context.execution_id)

    assert tracer.calls == [
        "start",
        "record:run.started",
        "record:run.finished",
        "finish",
    ]


@pytest.mark.asyncio
async def test_queue_overflow_disables_only_sink_without_backpressure() -> None:
    context = _context()
    tracer = _Tracer()
    tracer.start_gate = asyncio.Event()
    warnings: list[str] = []

    async def warn(
        code: str, sink: TraceSink, operation: TraceOperation, fallback: TraceFallback
    ) -> None:
        warnings.append(code)

    dispatcher = SafeTracerDispatcher(
        (TracerSink(TraceSink.CONSOLE, tracer),),
        warning_emitter=warn,
        queue_size=1,
    )
    event = EventEnricher(Path(".")).enrich(
        TaskStarted(run_id=context.run_id, session_id=None, task="safe"),
        PublishedObservation(context, 1),
    )
    dispatcher.start_execution(_start(context))
    await asyncio.sleep(0)
    dispatcher.record(event)
    dispatcher.record(event)
    dispatcher.finish_execution(_finish(context))
    tracer.start_gate.set()
    await dispatcher.drain_execution(context.execution_id)

    assert tracer.calls == ["start"]
    assert warnings == ["TRACE_SINK_OVERFLOW"]


@pytest.mark.asyncio
async def test_dispatcher_flush_timeout_is_bounded_and_warns_safely() -> None:
    context = _context()
    tracer = _Tracer()
    tracer.start_gate = asyncio.Event()
    warnings: list[tuple[str, TraceOperation, TraceFallback]] = []

    async def warn(
        code: str, sink: TraceSink, operation: TraceOperation, fallback: TraceFallback
    ) -> None:
        assert sink is TraceSink.CONSOLE
        warnings.append((code, operation, fallback))

    dispatcher = SafeTracerDispatcher(
        (TracerSink(TraceSink.CONSOLE, tracer),),
        warning_emitter=warn,
        flush_timeout_seconds=0.01,
    )
    dispatcher.start_execution(_start(context))
    await asyncio.sleep(0)
    await asyncio.wait_for(dispatcher.drain_execution(context.execution_id), 0.5)

    assert tracer.calls == ["start"]
    assert warnings == [
        ("TRACE_FLUSH_FAILED", TraceOperation.FLUSH, TraceFallback.NOOP)
    ]


@pytest.mark.asyncio
async def test_warning_record_is_never_sent_to_its_named_sink() -> None:
    context = _context()
    console = _Tracer()
    langsmith = _Tracer()

    async def warn(
        code: str, sink: TraceSink, operation: TraceOperation, fallback: TraceFallback
    ) -> None:
        raise AssertionError(f"unexpected warning: {code}:{sink}:{operation}:{fallback}")

    dispatcher = SafeTracerDispatcher(
        (
            TracerSink(TraceSink.CONSOLE, console),
            TracerSink(TraceSink.LANGSMITH, langsmith),
        ),
        warning_emitter=warn,
    )
    warning = EventEnricher(Path(".")).enrich(
        ObservabilityWarning(
            run_id=context.run_id,
            session_id=None,
            code="TRACE_SINK_FAILED",
            sink=TraceSink.CONSOLE,
            operation=TraceOperation.RECORD,
            fallback=TraceFallback.LANGSMITH,
        ),
        PublishedObservation(context, 1),
    )
    dispatcher.start_execution(_start(context))
    dispatcher.record(warning)
    dispatcher.finish_execution(_finish(context))
    await dispatcher.drain_execution(context.execution_id)

    assert console.calls == ["start", "finish"]
    assert langsmith.calls == ["start", "record:observability.warning", "finish"]


@pytest.mark.asyncio
async def test_resume_uses_fresh_queue_for_new_execution_id() -> None:
    initial = _context()
    resumed = replace(
        initial,
        execution_id=str(uuid4()),
        is_resume=True,
        started_at=datetime.now(UTC),
    )
    tracer = _Tracer()

    async def warn(
        code: str, sink: TraceSink, operation: TraceOperation, fallback: TraceFallback
    ) -> None:
        raise AssertionError(f"unexpected warning: {code}:{sink}:{operation}:{fallback}")

    dispatcher = SafeTracerDispatcher(
        (TracerSink(TraceSink.CONSOLE, tracer),), warning_emitter=warn
    )
    dispatcher.start_execution(_start(initial))
    dispatcher.finish_execution(_finish(initial))
    await dispatcher.drain_execution(initial.execution_id)

    event = EventEnricher(Path(".")).enrich(
        TaskStarted(run_id=resumed.run_id, session_id=None, task="safe"),
        PublishedObservation(resumed, 1),
    )
    dispatcher.start_execution(_start(resumed))
    dispatcher.record(event)
    dispatcher.finish_execution(_finish(resumed))
    await dispatcher.drain_execution(resumed.execution_id)

    assert tracer.calls == [
        "start",
        "finish",
        "start",
        "record:run.started",
        "finish",
    ]


@pytest.mark.asyncio
async def test_telemetry_ingress_drain_is_bounded() -> None:
    context = _context()
    tracer = _Tracer()
    warning_started = asyncio.Event()
    release_warning = asyncio.Event()
    warnings: list[str] = []

    class _FailingEnricher(EventEnricher):
        def enrich(
            self,
            event: object,
            observation: PublishedObservation,
            *,
            tool_call_count: int | None = None,
        ) -> TelemetryEvent:
            raise ValueError("invalid telemetry")

    async def warn(
        code: str, sink: TraceSink, operation: TraceOperation, fallback: TraceFallback
    ) -> None:
        warnings.append(code)
        warning_started.set()
        await release_warning.wait()

    dispatcher = SafeTracerDispatcher(
        (TracerSink(TraceSink.CONSOLE, tracer),), warning_emitter=warn
    )
    subscriber = TelemetrySubscriber(
        _FailingEnricher(Path(".")),
        dispatcher,
        warning_emitter=warn,
        flush_timeout_seconds=0.01,
    )
    publisher = InProcessEventPublisher()
    publisher.subscribe(subscriber)
    publisher.open_execution(context)
    subscriber.start_execution(_start(context))
    with bind_execution_context(context):
        await publisher.publish(
            TaskStarted(run_id=context.run_id, session_id=None, task="safe")
        )
    await warning_started.wait()
    subscriber.finish_execution(_finish(context))
    await asyncio.wait_for(subscriber.drain_execution(context.execution_id), 0.5)

    assert warnings == ["TELEMETRY_EVENT_INVALID"]
