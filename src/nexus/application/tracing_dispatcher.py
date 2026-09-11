"""Failure-isolated per-execution FIFO lifecycle dispatcher for Tracer sinks."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from nexus.application.execution_context import bind_execution_context
from nexus.domain.observability import (
    RunExecutionContext,
    TelemetryEvent,
    TraceRunFinish,
    TraceRunStart,
)
from nexus.domain.ports.tracer import Tracer
from nexus.domain.runtime_events import TraceFallback, TraceOperation, TraceSink

WarningEmitter = Callable[
    [str, TraceSink, TraceOperation, TraceFallback], Awaitable[None]
]


@dataclass(frozen=True, slots=True)
class TracerSink:
    kind: TraceSink
    tracer: Tracer


@dataclass(frozen=True, slots=True)
class _Start:
    value: TraceRunStart


@dataclass(frozen=True, slots=True)
class _Record:
    value: TelemetryEvent


@dataclass(frozen=True, slots=True)
class _Finish:
    value: TraceRunFinish


_Control = _Start | _Record | _Finish


@dataclass(slots=True)
class _SinkExecution:
    sink: TracerSink
    context: RunExecutionContext
    queue: asyncio.Queue[_Control]
    worker: asyncio.Task[None] | None = None
    healthy: bool = True
    warned: bool = False


class SafeTracerDispatcher:
    """Give every enabled sink one FIFO worker and contain every adapter failure."""

    def __init__(
        self,
        sinks: tuple[TracerSink, ...],
        *,
        warning_emitter: WarningEmitter,
        queue_size: int = 256,
        flush_timeout_seconds: float = 2.0,
    ) -> None:
        if queue_size < 1 or flush_timeout_seconds <= 0:
            raise ValueError("Tracer queue and flush timeout must be positive.")
        if len({sink.kind for sink in sinks}) != len(sinks):
            raise ValueError("Tracer sink kinds must be unique.")
        self._sinks = sinks
        self._warning_emitter = warning_emitter
        self._queue_size = queue_size
        self._flush_timeout_seconds = flush_timeout_seconds
        self._executions: dict[str, dict[TraceSink, _SinkExecution]] = {}

    def start_execution(self, start: TraceRunStart) -> None:
        execution_id = start.context.execution_id
        if execution_id in self._executions:
            raise RuntimeError("Tracer execution already exists.")
        states: dict[TraceSink, _SinkExecution] = {}
        self._executions[execution_id] = states
        for sink in self._sinks:
            queue: asyncio.Queue[_Control] = asyncio.Queue(self._queue_size)
            state = _SinkExecution(sink=sink, context=start.context, queue=queue)
            state.worker = asyncio.create_task(self._work(execution_id, state))
            states[sink.kind] = state
            queue.put_nowait(_Start(start))

    def record(self, event: TelemetryEvent) -> None:
        states = self._executions.get(event.execution_id)
        if states is None:
            return
        excluded = _warning_sink(event)
        for kind, state in states.items():
            if not state.healthy or kind is excluded:
                continue
            self._offer(execution_id=event.execution_id, state=state, item=_Record(event))

    def finish_execution(self, finish: TraceRunFinish) -> None:
        states = self._executions.get(finish.context.execution_id)
        if states is None:
            return
        for state in states.values():
            if state.healthy:
                self._offer(
                    execution_id=finish.context.execution_id,
                    state=state,
                    item=_Finish(finish),
                )
            else:
                _discard_queued(state.queue)
                state.queue.put_nowait(_Finish(finish))

    async def drain_execution(self, execution_id: str) -> None:
        states = self._executions.get(execution_id)
        if not states:
            self._executions.pop(execution_id, None)
            return
        workers = tuple(
            state.worker for state in states.values() if state.worker is not None
        )
        if not workers:
            self._executions.pop(execution_id, None)
            return
        _, pending = await asyncio.wait(
            workers, timeout=self._flush_timeout_seconds
        )
        if pending:
            for state in states.values():
                if state.worker is not None and state.worker in pending:
                    state.worker.cancel()
                    await self._warn(
                        execution_id,
                        state,
                        "TRACE_FLUSH_FAILED",
                        TraceOperation.FLUSH,
                    )
        await asyncio.gather(*workers, return_exceptions=True)
        self._executions.pop(execution_id, None)

    async def close(self) -> None:
        for execution_id in tuple(self._executions):
            await self.drain_execution(execution_id)

    def _offer(
        self,
        *,
        execution_id: str,
        state: _SinkExecution,
        item: _Control,
    ) -> None:
        try:
            state.queue.put_nowait(item)
        except asyncio.QueueFull:
            state.healthy = False
            asyncio.create_task(
                self._warn(
                    execution_id,
                    state,
                    "TRACE_SINK_OVERFLOW",
                    TraceOperation.RECORD,
                )
            )

    async def _work(self, execution_id: str, state: _SinkExecution) -> None:
        while True:
            item = await state.queue.get()
            if not state.healthy:
                if isinstance(item, _Finish):
                    return
                continue
            try:
                if isinstance(item, _Start):
                    await state.sink.tracer.start_run(item.value)
                elif isinstance(item, _Record):
                    await state.sink.tracer.record(item.value)
                else:
                    await state.sink.tracer.finish(item.value)
                    return
            except asyncio.CancelledError:
                raise
            except Exception:
                state.healthy = False
                operation = (
                    TraceOperation.START
                    if isinstance(item, _Start)
                    else TraceOperation.RECORD
                    if isinstance(item, _Record)
                    else TraceOperation.FINISH
                )
                await self._warn(
                    execution_id,
                    state,
                    "TRACE_SINK_FAILED",
                    operation,
                )
                if isinstance(item, _Finish):
                    return

    async def _warn(
        self,
        execution_id: str,
        state: _SinkExecution,
        code: str,
        operation: TraceOperation,
    ) -> None:
        if state.warned:
            return
        state.warned = True
        try:
            with bind_execution_context(state.context):
                await self._warning_emitter(
                    code,
                    state.sink.kind,
                    operation,
                    self._fallback(execution_id, state.sink.kind),
                )
        except Exception:
            return

    def _fallback(self, execution_id: str, failed: TraceSink) -> TraceFallback:
        states = self._executions.get(execution_id, {})
        if failed is not TraceSink.CONSOLE:
            console = states.get(TraceSink.CONSOLE)
            if console is not None and console.healthy:
                return TraceFallback.CONSOLE
        if failed is not TraceSink.LANGSMITH:
            langsmith = states.get(TraceSink.LANGSMITH)
            if langsmith is not None and langsmith.healthy:
                return TraceFallback.LANGSMITH
        return TraceFallback.NOOP


def _warning_sink(event: TelemetryEvent) -> TraceSink | None:
    if event.event_type.value != "observability.warning":
        return None
    value = event.payload.get("sink")
    if not isinstance(value, str):
        return None
    try:
        return TraceSink(value)
    except ValueError:
        return None


def _discard_queued(queue: asyncio.Queue[_Control]) -> None:
    while True:
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            return
