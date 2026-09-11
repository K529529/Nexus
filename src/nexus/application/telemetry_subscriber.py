"""Non-blocking telemetry ingress isolated from the required live event lane."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from nexus.application.event_publisher import (
    PublishedObservation,
    current_published_observation,
)
from nexus.application.execution_context import bind_execution_context
from nexus.application.execution_ledger import ToolExecutionLedger
from nexus.application.telemetry import EventEnricher, TelemetryRedactor
from nexus.application.tracing_dispatcher import SafeTracerDispatcher, WarningEmitter
from nexus.domain.observability import (
    RunExecutionContext,
    TraceRunFinish,
    TraceRunStart,
)
from nexus.domain.runtime_events import (
    RuntimeEvent,
    TraceFallback,
    TraceOperation,
    TraceSink,
)


@dataclass(frozen=True, slots=True)
class _ObservedEvent:
    event: RuntimeEvent
    observation: PublishedObservation
    tool_call_count: int | None


@dataclass(frozen=True, slots=True)
class _FinishExecution:
    finish: TraceRunFinish


_IngressItem = _ObservedEvent | _FinishExecution


@dataclass(slots=True)
class _IngressExecution:
    context: RunExecutionContext
    queue: asyncio.Queue[_IngressItem]
    worker: asyncio.Task[None]
    finish: TraceRunFinish | None = None


class TelemetrySubscriber:
    def __init__(
        self,
        enricher: EventEnricher,
        dispatcher: SafeTracerDispatcher,
        warning_emitter: WarningEmitter | None = None,
        ledger: ToolExecutionLedger | None = None,
        redactor: TelemetryRedactor | None = None,
        flush_timeout_seconds: float = 2.0,
    ) -> None:
        if flush_timeout_seconds <= 0:
            raise ValueError("Telemetry flush timeout must be positive.")
        self._enricher = enricher
        self._dispatcher = dispatcher
        self._warning_emitter = warning_emitter
        self._ledger = ledger
        self._redactor = redactor or TelemetryRedactor()
        self._flush_timeout_seconds = flush_timeout_seconds
        self._executions: dict[str, _IngressExecution] = {}
        self._warned_executions: set[str] = set()

    def start_execution(self, start: TraceRunStart) -> None:
        execution_id = start.context.execution_id
        if execution_id in self._executions:
            raise RuntimeError("Telemetry ingress execution already exists.")
        self._dispatcher.start_execution(start)
        queue: asyncio.Queue[_IngressItem] = asyncio.Queue()
        worker = asyncio.create_task(self._work(execution_id, queue))
        self._executions[execution_id] = _IngressExecution(
            context=start.context,
            queue=queue,
            worker=worker,
        )

    async def on_event(self, event: RuntimeEvent) -> None:
        observation = current_published_observation()
        state = self._executions.get(observation.context.execution_id)
        if state is None:
            return
        state.queue.put_nowait(
            _ObservedEvent(
                event,
                observation,
                None if self._ledger is None else self._ledger.count(event.run_id),
            )
        )

    def finish_execution(self, finish: TraceRunFinish) -> None:
        state = self._executions.get(finish.context.execution_id)
        if state is None:
            return
        state.finish = finish
        state.queue.put_nowait(_FinishExecution(finish))

    async def drain_execution(self, execution_id: str) -> None:
        state = self._executions.pop(execution_id, None)
        if state is None:
            return
        try:
            await asyncio.wait_for(
                asyncio.shield(state.worker), timeout=self._flush_timeout_seconds
            )
        except TimeoutError:
            state.worker.cancel()
            await asyncio.gather(state.worker, return_exceptions=True)
            await self._warn_once(
                execution_id,
                PublishedObservation(state.context, 1),
                "TRACE_FLUSH_FAILED",
                TraceOperation.FLUSH,
            )
            if state.finish is not None:
                self._dispatcher.finish_execution(state.finish)
        await self._dispatcher.drain_execution(execution_id)
        self._warned_executions.discard(execution_id)

    async def close(self) -> None:
        for execution_id in tuple(self._executions):
            await self.drain_execution(execution_id)
        await self._dispatcher.close()

    async def _work(
        self, execution_id: str, queue: asyncio.Queue[_IngressItem]
    ) -> None:
        while True:
            item = await queue.get()
            if isinstance(item, _FinishExecution):
                self._dispatcher.finish_execution(item.finish)
                return
            try:
                telemetry = self._enricher.enrich(
                    item.event,
                    item.observation,
                    tool_call_count=item.tool_call_count,
                )
            except Exception:
                await self._warn_once(
                    execution_id,
                    item.observation,
                    "TELEMETRY_EVENT_INVALID",
                    TraceOperation.ENRICH,
                )
                continue
            try:
                telemetry = self._redactor.redact(telemetry)
            except Exception:
                await self._warn_once(
                    execution_id,
                    item.observation,
                    "TRACE_REDACTION_FAILED",
                    TraceOperation.REDACT,
                )
                continue
            self._dispatcher.record(telemetry)

    async def _warn_once(
        self,
        execution_id: str,
        observation: PublishedObservation,
        code: str,
        operation: TraceOperation,
    ) -> None:
        if (
            self._warning_emitter is None
            or execution_id in self._warned_executions
        ):
            return
        self._warned_executions.add(execution_id)
        try:
            with bind_execution_context(observation.context):
                await self._warning_emitter(
                    code,
                    TraceSink.TELEMETRY,
                    operation,
                    TraceFallback.NONE,
                )
        except Exception:
            pass
