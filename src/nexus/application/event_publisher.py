"""In-process Day 8 live event publisher with isolated execution streams."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from nexus.application.execution_context import current_execution_context
from nexus.domain.observability import RunExecutionContext
from nexus.domain.ports.events import EventSubscriber, EventSubscription
from nexus.domain.runtime_events import (
    ApprovalRequested,
    ErrorOccurred,
    FinalResult,
    RunInterrupted,
    RuntimeEvent,
)

_END = object()


@dataclass(frozen=True, slots=True)
class PublishedObservation:
    """Private metadata copied at the synchronous publication boundary."""

    context: RunExecutionContext
    sequence: int


_OBSERVATION: ContextVar[PublishedObservation | None] = ContextVar(
    "nexus_published_observation", default=None
)


def current_published_observation() -> PublishedObservation:
    observation = _OBSERVATION.get()
    if observation is None:
        raise RuntimeError("No published RuntimeEvent observation is bound.")
    return observation


@dataclass(slots=True)
class _ExecutionStream:
    context: RunExecutionContext
    queue: asyncio.Queue[RuntimeEvent | object]
    sequence: int = 0
    terminal_published: bool = False


class _Subscription(EventSubscription):
    def __init__(
        self, publisher: InProcessEventPublisher, subscriber: EventSubscriber
    ) -> None:
        self._publisher = publisher
        self._subscriber = subscriber
        self._closed = False

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._publisher._unsubscribe(self._subscriber)


class InProcessEventPublisher:
    """Required lossless live lane plus bounded subscriber handoff callbacks."""

    def __init__(self) -> None:
        self._subscribers: list[EventSubscriber] = []
        self._streams: dict[str, _ExecutionStream] = {}
        self._seen_plan_approvals: dict[str, set[str]] = {}

    def subscribe(self, subscriber: EventSubscriber) -> EventSubscription:
        if subscriber not in self._subscribers:
            self._subscribers.append(subscriber)
        return _Subscription(self, subscriber)

    def open_execution(self, context: RunExecutionContext) -> None:
        if context.execution_id in self._streams:
            raise RuntimeError("Execution stream already exists.")
        self._streams[context.execution_id] = _ExecutionStream(
            context=context,
            queue=asyncio.Queue(),
        )
        self._seen_plan_approvals[context.execution_id] = set()

    async def publish(self, event: RuntimeEvent) -> None:
        context = current_execution_context()
        stream = self._streams.get(context.execution_id)
        if stream is None:
            raise RuntimeError("Execution stream is not open.")
        if stream.terminal_published:
            raise RuntimeError("Cannot publish after a terminal RuntimeEvent.")
        if event.run_id != context.run_id or event.session_id != context.session_id:
            raise ValueError("RuntimeEvent identity does not match execution context.")
        if isinstance(event, ApprovalRequested) and event.plan_id is not None:
            seen = self._seen_plan_approvals[context.execution_id]
            if event.approval_id in seen:
                return
            seen.add(event.approval_id)

        stream.sequence += 1
        observation = PublishedObservation(context=context, sequence=stream.sequence)
        stream.queue.put_nowait(event)
        token = _OBSERVATION.set(observation)
        try:
            for subscriber in tuple(self._subscribers):
                try:
                    await subscriber.on_event(event)
                except Exception:
                    continue
        finally:
            _OBSERVATION.reset(token)

        if _is_terminal(event):
            stream.terminal_published = True
            stream.queue.put_nowait(_END)

    emit = publish

    async def events(self, execution_id: str) -> AsyncIterator[RuntimeEvent]:
        stream = self._streams.get(execution_id)
        if stream is None:
            raise RuntimeError("Execution stream is not open.")
        while True:
            item = await stream.queue.get()
            if item is _END:
                return
            if not isinstance(item, RuntimeEvent):
                raise RuntimeError("Invalid live event queue item.")
            yield item

    def close_execution(self, execution_id: str) -> None:
        stream = self._streams.pop(execution_id, None)
        self._seen_plan_approvals.pop(execution_id, None)
        if stream is not None and not stream.terminal_published:
            stream.queue.put_nowait(_END)

    @asynccontextmanager
    async def execution(
        self, context: RunExecutionContext
    ) -> AsyncIterator[AsyncIterator[RuntimeEvent]]:
        self.open_execution(context)
        try:
            yield self.events(context.execution_id)
        finally:
            self.close_execution(context.execution_id)

    def _unsubscribe(self, subscriber: EventSubscriber) -> None:
        try:
            self._subscribers.remove(subscriber)
        except ValueError:
            pass


def _is_terminal(event: RuntimeEvent) -> bool:
    return isinstance(event, (FinalResult, RunInterrupted, ErrorOccurred))


class LiveRuntimeEventBridge:
    """Publish graph progress live while deferring its terminal result to Runtime."""

    def __init__(self, publisher: InProcessEventPublisher) -> None:
        self._publisher = publisher
        self._terminals: dict[str, FinalResult] = {}

    async def emit(self, event: RuntimeEvent) -> None:
        if isinstance(event, FinalResult):
            if event.run_id in self._terminals:
                raise RuntimeError("Graph emitted multiple final results.")
            self._terminals[event.run_id] = event
            return
        await self._publisher.publish(event)

    def drain(self, run_id: str) -> tuple[RuntimeEvent, ...]:
        terminal = self._terminals.pop(run_id, None)
        return () if terminal is None else (terminal,)
