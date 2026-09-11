import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest

from nexus.application.event_publisher import InProcessEventPublisher, LiveRuntimeEventBridge
from nexus.application.execution_context import current_execution_context
from nexus.application.runtime import NexusRuntime
from nexus.application.session_service import SessionService
from nexus.application.telemetry import EventEnricher
from nexus.application.telemetry_subscriber import TelemetrySubscriber
from nexus.application.tracing_dispatcher import SafeTracerDispatcher, TracerSink
from nexus.domain.agent_state import AgentState
from nexus.domain.model import (
    ModelMessage,
    TokenUsage,
    TokenUsageAggregate,
    UsageAvailability,
)
from nexus.domain.observability import TelemetryEvent, TraceRunFinish, TraceRunStart
from nexus.domain.persistence import Run, RunStatus
from nexus.domain.planning import PlanApprovalResumeInput, TerminalStatus
from nexus.domain.runtime_events import (
    ContextBuilt,
    ErrorOccurred,
    FinalResult,
    ObservabilityWarning,
    RunInterrupted,
    RuntimeStatus,
    TaskStarted,
    TraceFallback,
    TraceOperation,
    TraceSink,
)
from nexus.errors import NexusError


async def _wait_for_telemetry_cleanup(
    subscriber: TelemetrySubscriber,
    dispatcher: SafeTracerDispatcher,
) -> None:
    workers = tuple(
        state.worker for state in subscriber._executions.values()  # noqa: SLF001
    )
    if workers:
        await asyncio.wait_for(asyncio.gather(*workers), timeout=1.0)
    cleanup_tasks = tuple(dispatcher._cleanup_tasks.values())  # noqa: SLF001
    if cleanup_tasks:
        await asyncio.wait_for(asyncio.gather(*cleanup_tasks), timeout=1.0)


class _LiveGraph:
    def __init__(self, bridge: LiveRuntimeEventBridge) -> None:
        self._bridge = bridge
        self.release = asyncio.Event()

    async def run(
        self, state: AgentState, *, thread_id: str | None = None
    ) -> AgentState:
        await self._bridge.emit(
            ContextBuilt(
                run_id=state.run_id,
                session_id=state.session_id,
                selected_paths=(),
                retained_characters=0,
                truncated=False,
            )
        )
        await self.release.wait()
        return replace(
            state,
            messages=[*state.messages],
        )

    async def resume(
        self,
        *,
        thread_id: str,
        resume_input: PlanApprovalResumeInput | None = None,
    ) -> AgentState:
        raise AssertionError("resume is not used")


@pytest.mark.asyncio
async def test_runtime_yields_start_and_graph_progress_before_graph_completion() -> None:
    publisher = InProcessEventPublisher()
    bridge = LiveRuntimeEventBridge(publisher)
    graph = _LiveGraph(bridge)
    runtime = NexusRuntime(graph, event_buffer=bridge, event_publisher=publisher)
    stream = runtime.run("safe task")

    first = await anext(stream)
    second = await anext(stream)
    assert isinstance(first, TaskStarted)
    assert isinstance(second, ContextBuilt)
    assert graph.release.is_set() is False

    graph.release.set()
    terminal = await anext(stream)
    assert isinstance(terminal, ErrorOccurred)
    assert terminal.code == "GRAPH_INCOMPLETE"
    with pytest.raises(StopAsyncIteration):
        await anext(stream)


@pytest.mark.asyncio
async def test_invalid_task_has_one_start_and_one_terminal_error() -> None:
    publisher = InProcessEventPublisher()
    bridge = LiveRuntimeEventBridge(publisher)
    graph = _LiveGraph(bridge)
    runtime = NexusRuntime(graph, event_buffer=bridge, event_publisher=publisher)

    events = [event async for event in runtime.run("   ")]

    assert [type(event) for event in events] == [TaskStarted, ErrorOccurred]
    assert not any(isinstance(event, FinalResult) for event in events)


@pytest.mark.asyncio
async def test_runtime_has_exactly_one_success_terminal_event() -> None:
    publisher = InProcessEventPublisher()
    bridge = LiveRuntimeEventBridge(publisher)

    class SuccessfulGraph(_LiveGraph):
        async def run(
            self, state: AgentState, *, thread_id: str | None = None
        ) -> AgentState:
            return replace(
                state,
                messages=[*state.messages, ModelMessage("assistant", "safe result")],
                status=RuntimeStatus.COMPLETED,
            )

    runtime = NexusRuntime(
        SuccessfulGraph(bridge), event_buffer=bridge, event_publisher=publisher
    )
    events = [event async for event in runtime.run("safe task")]

    assert [type(event) for event in events] == [TaskStarted, FinalResult]
    assert sum(isinstance(event, FinalResult) for event in events) == 1


@pytest.mark.asyncio
async def test_all_enabled_sinks_can_fail_without_changing_business_outcome(
    tmp_path: Path,
) -> None:
    publisher = InProcessEventPublisher()
    bridge = LiveRuntimeEventBridge(publisher)
    warning_seen = asyncio.Event()

    class SuccessfulGraph(_LiveGraph):
        async def run(
            self, state: AgentState, *, thread_id: str | None = None
        ) -> AgentState:
            await warning_seen.wait()
            return replace(
                state,
                messages=[*state.messages, ModelMessage("assistant", "safe result")],
                status=RuntimeStatus.COMPLETED,
            )

    class FailStartTracer:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def start_run(self, start: TraceRunStart) -> None:
            self.calls.append("start")
            raise RuntimeError("RAW_TRACE_FAILURE_MUST_NOT_ESCAPE")

        async def record(self, event: TelemetryEvent) -> None:
            self.calls.append("record")

        async def finish(self, finish: TraceRunFinish) -> None:
            self.calls.append("finish")

    async def warn(
        code: str,
        sink: TraceSink,
        operation: TraceOperation,
        fallback: TraceFallback,
    ) -> None:
        context = current_execution_context()
        await publisher.publish(
            ObservabilityWarning(
                run_id=context.run_id,
                session_id=context.session_id,
                code=code,
                sink=sink,
                operation=operation,
                fallback=fallback,
            )
        )
        warning_seen.set()

    tracer = FailStartTracer()
    dispatcher = SafeTracerDispatcher(
        (TracerSink(TraceSink.CONSOLE, tracer),), warning_emitter=warn
    )
    subscriber = TelemetrySubscriber(
        EventEnricher(tmp_path), dispatcher, warning_emitter=warn
    )
    publisher.subscribe(subscriber)
    runtime = NexusRuntime(
        SuccessfulGraph(bridge),
        event_buffer=bridge,
        event_publisher=publisher,
        telemetry_subscriber=subscriber,
    )

    events = [event async for event in runtime.run("safe task")]
    await subscriber.close()

    assert [type(event).__name__ for event in events] == [
        "TaskStarted",
        "ObservabilityWarning",
        "FinalResult",
    ]
    assert isinstance(events[-1], FinalResult)
    assert events[-1].status is RuntimeStatus.COMPLETED
    assert tracer.calls == ["start"]


@pytest.mark.asyncio
async def test_reused_runtime_releases_each_terminal_execution_lifecycle(
    tmp_path: Path,
) -> None:
    publisher = InProcessEventPublisher()
    bridge = LiveRuntimeEventBridge(publisher)
    session_id = str(uuid4())

    class SequentialGraph(_LiveGraph):
        async def run(
            self, state: AgentState, *, thread_id: str | None = None
        ) -> AgentState:
            if state.task == "completed":
                return replace(
                    state,
                    messages=[*state.messages, ModelMessage("assistant", "done")],
                    status=RuntimeStatus.COMPLETED,
                    terminal_status=TerminalStatus.SUCCEEDED,
                )
            if state.task == "failed":
                raise NexusError("safe failure", code="TEST_FAILURE")
            if state.task == "interrupted":
                return replace(state, status=RuntimeStatus.INTERRUPTED)
            raise AssertionError(f"unexpected task: {state.task}")

    class Sessions:
        def __init__(self) -> None:
            self.runs: dict[str, Run] = {}

        async def start_run(self, **values: Any) -> Run:
            run = Run(
                values["run_id"],
                session_id,
                values["task"],
                RunStatus.RUNNING,
                values["model_metadata"],
                datetime.now(UTC),
                None,
                0,
                0,
                None,
                None,
                f"nexus-run:{values['run_id']}",
            )
            self.runs[run.run_id] = run
            return run

        async def finalize_run(
            self, run_id: str, *args: object, **kwargs: object
        ) -> Run:
            return self.runs[run_id]

        async def fail_run(
            self, run_id: str, *, code: str, message: str
        ) -> Run:
            return self.runs[run_id]

        async def mark_interrupted(self, run_id: str) -> Run:
            return self.runs[run_id]

    class RecordingTracer:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, str | None]] = []

        async def start_run(self, start: TraceRunStart) -> None:
            self.calls.append((start.context.execution_id, "start", None))

        async def record(self, event: TelemetryEvent) -> None:
            self.calls.append((event.execution_id, "record", event.event_type.value))

        async def finish(self, finish: TraceRunFinish) -> None:
            self.calls.append((finish.context.execution_id, "finish", None))

    async def warn(
        code: str,
        sink: TraceSink,
        operation: TraceOperation,
        fallback: TraceFallback,
    ) -> None:
        raise AssertionError(f"unexpected warning: {code}:{sink}:{operation}:{fallback}")

    tracer = RecordingTracer()
    dispatcher = SafeTracerDispatcher(
        (TracerSink(TraceSink.CONSOLE, tracer),), warning_emitter=warn
    )
    subscriber = TelemetrySubscriber(EventEnricher(tmp_path), dispatcher)
    publisher.subscribe(subscriber)
    runtime = NexusRuntime(
        SequentialGraph(bridge),
        session_service=cast(SessionService, Sessions()),
        event_buffer=bridge,
        event_publisher=publisher,
        telemetry_subscriber=subscriber,
    )

    terminal_types = []
    execution_ids: list[str] = []
    for task in ("completed", "failed", "interrupted"):
        events = [event async for event in runtime.run(task, session_id)]
        terminal_types.append(type(events[-1]))
        await _wait_for_telemetry_cleanup(subscriber, dispatcher)
        execution_ids.append(tracer.calls[-1][0])
        assert subscriber._executions == {}  # noqa: SLF001
        assert dispatcher._executions == {}  # noqa: SLF001
        assert dispatcher._cleanup_tasks == {}  # noqa: SLF001

    assert terminal_types == [FinalResult, ErrorOccurred, RunInterrupted]
    assert len(set(execution_ids)) == 3
    for execution_id, terminal_event in zip(
        execution_ids,
        ("run.finished", "error.occurred", "run.interrupted"),
        strict=True,
    ):
        calls = [call[1:] for call in tracer.calls if call[0] == execution_id]
        assert calls[0] == ("start", None)
        assert ("record", terminal_event) in calls
        assert calls[-1] == ("finish", None)

    await subscriber.close()
    await subscriber.close()


@pytest.mark.asyncio
async def test_resume_preserves_trace_and_aggregate_with_fresh_execution() -> None:
    publisher = InProcessEventPublisher()
    session_id = str(uuid4())
    aggregate = TokenUsageAggregate().add(
        TokenUsage(2, 3, 5, UsageAvailability.REPORTED)
    )

    class ResumeGraph(_LiveGraph):
        def __init__(self, bridge: LiveRuntimeEventBridge) -> None:
            super().__init__(bridge)
            self.interrupted: AgentState | None = None

        async def run(
            self, state: AgentState, *, thread_id: str | None = None
        ) -> AgentState:
            self.interrupted = replace(
                state,
                status=RuntimeStatus.INTERRUPTED,
                step_count=2,
                llm_call_count=1,
                token_usage=aggregate,
            )
            return self.interrupted

        async def resume(
            self,
            *,
            thread_id: str,
            resume_input: PlanApprovalResumeInput | None = None,
        ) -> AgentState:
            assert self.interrupted is not None
            return replace(
                self.interrupted,
                status=RuntimeStatus.COMPLETED,
                terminal_status=TerminalStatus.SUCCEEDED,
                messages=[
                    *self.interrupted.messages,
                    ModelMessage("assistant", "safe result"),
                ],
            )

    class Sessions:
        run: Run | None = None

        async def start_run(self, **values: Any) -> Run:
            self.run = Run(
                values["run_id"],
                session_id,
                values["task"],
                RunStatus.RUNNING,
                values["model_metadata"],
                datetime.now(UTC),
                None,
                0,
                0,
                None,
                None,
                f"nexus-run:{values['run_id']}",
            )
            return self.run

        async def mark_interrupted(self, run_id: str) -> Run:
            assert self.run is not None and self.run.run_id == run_id
            return self.run

        async def resolve_resumable_run(self, requested_session_id: str) -> Run:
            assert self.run is not None and requested_session_id == session_id
            return self.run

        async def finalize_run(self, run_id: str, *args: object, **kwargs: object) -> Run:
            assert self.run is not None and self.run.run_id == run_id
            return self.run

    class Telemetry:
        def __init__(self) -> None:
            self.starts: list[TraceRunStart] = []
            self.finishes: list[TraceRunFinish] = []

        def start_execution(self, start: TraceRunStart) -> None:
            self.starts.append(start)

        def finish_execution(self, finish: TraceRunFinish) -> None:
            self.finishes.append(finish)

    bridge = LiveRuntimeEventBridge(publisher)
    graph = ResumeGraph(bridge)
    telemetry = Telemetry()
    runtime = NexusRuntime(
        graph,
        session_service=cast(SessionService, Sessions()),
        event_buffer=bridge,
        event_publisher=publisher,
        telemetry_subscriber=cast(TelemetrySubscriber, telemetry),
    )

    initial = [event async for event in runtime.run("safe task", session_id)]
    resumed = [event async for event in runtime.resume(session_id)]

    assert [type(event).__name__ for event in initial] == [
        "TaskStarted",
        "RunInterrupted",
    ]
    assert [type(event).__name__ for event in resumed] == [
        "TaskStarted",
        "FinalResult",
    ]
    first, second = (item.context for item in telemetry.starts)
    assert first.run_id == second.run_id
    assert first.trace_id == second.trace_id == first.run_id
    assert first.execution_id != second.execution_id
    assert first.is_resume is False and second.is_resume is True
    assert [finish.step_count for finish in telemetry.finishes] == [2, 2]
    assert [finish.llm_call_count for finish in telemetry.finishes] == [1, 1]
    assert [finish.token_usage.total_tokens for finish in telemetry.finishes] == [5, 5]
