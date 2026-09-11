import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from nexus.application.event_publisher import (
    InProcessEventPublisher,
    current_published_observation,
)
from nexus.application.execution_context import bind_execution_context
from nexus.domain.observability import RunExecutionContext
from nexus.domain.runtime_events import FinalResult, RuntimeEvent, TaskStarted


def _context(*, session_id: str | None = None) -> RunExecutionContext:
    run_id = str(uuid4())
    return RunExecutionContext(
        trace_id=run_id,
        execution_id=str(uuid4()),
        run_id=run_id,
        session_id=session_id,
        is_resume=False,
        started_at=datetime.now(UTC),
    )


class _CapturingSubscriber:
    def __init__(self) -> None:
        self.observations: list[tuple[str, int, str]] = []

    async def on_event(self, event: RuntimeEvent) -> None:
        observation = current_published_observation()
        self.observations.append(
            (observation.context.execution_id, observation.sequence, type(event).__name__)
        )


@pytest.mark.asyncio
async def test_publisher_streams_live_events_in_sequence_and_closes_after_terminal() -> None:
    publisher = InProcessEventPublisher()
    context = _context(session_id=str(uuid4()))
    subscriber = _CapturingSubscriber()
    publisher.subscribe(subscriber)
    publisher.open_execution(context)

    async def produce() -> None:
        with bind_execution_context(context):
            await publisher.publish(
                TaskStarted(
                    run_id=context.run_id,
                    session_id=context.session_id,
                    task="safe task",
                )
            )
            await asyncio.sleep(0)
            await publisher.publish(
                FinalResult(
                    run_id=context.run_id,
                    session_id=context.session_id,
                    content="done",
                )
            )

    producer = asyncio.create_task(produce())
    received = [event async for event in publisher.events(context.execution_id)]
    await producer

    assert [type(event).__name__ for event in received] == ["TaskStarted", "FinalResult"]
    assert subscriber.observations == [
        (context.execution_id, 1, "TaskStarted"),
        (context.execution_id, 2, "FinalResult"),
    ]


@pytest.mark.asyncio
async def test_concurrent_execution_sequences_and_contexts_are_isolated() -> None:
    publisher = InProcessEventPublisher()
    first = _context()
    second = _context()
    subscriber = _CapturingSubscriber()
    publisher.subscribe(subscriber)
    publisher.open_execution(first)
    publisher.open_execution(second)

    async def publish_terminal(context: RunExecutionContext) -> None:
        with bind_execution_context(context):
            await asyncio.sleep(0)
            await publisher.publish(
                FinalResult(run_id=context.run_id, session_id=None, content="done")
            )

    await asyncio.gather(publish_terminal(first), publish_terminal(second))

    assert {(execution_id, sequence) for execution_id, sequence, _ in subscriber.observations} == {
        (first.execution_id, 1),
        (second.execution_id, 1),
    }


@pytest.mark.asyncio
async def test_publisher_rejects_mismatched_or_missing_execution_context() -> None:
    publisher = InProcessEventPublisher()
    context = _context()
    publisher.open_execution(context)
    event = TaskStarted(run_id=context.run_id, session_id=None, task="safe task")

    with pytest.raises(RuntimeError, match="No RunExecutionContext"):
        await publisher.publish(event)

    other = _context()
    with bind_execution_context(other), pytest.raises(RuntimeError, match="not open"):
        await publisher.publish(event)
