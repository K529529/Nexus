from collections.abc import AsyncIterator

import pytest

from nexus.application.runtime import NexusRuntime
from nexus.domain.agent_state import AgentState
from nexus.domain.model import ModelMessage
from nexus.domain.runtime_events import ErrorOccurred, FinalResult, RuntimeStatus, TaskStarted
from nexus.errors import ModelError


class SuccessfulGraph:
    def __init__(self) -> None:
        self.called = False

    async def run(
        self, state: AgentState, *, thread_id: str | None = None
    ) -> AgentState:
        self.called = True
        return AgentState(
            task=state.task,
            messages=[*state.messages, ModelMessage(role="assistant", content="Hello!")],
            run_id=state.run_id,
            session_id=state.session_id,
            status=RuntimeStatus.COMPLETED,
        )

    async def resume(self, *, thread_id: str) -> AgentState:
        raise NotImplementedError


class FailingGraph:
    async def run(
        self, state: AgentState, *, thread_id: str | None = None
    ) -> AgentState:
        raise ModelError("Model is temporarily unavailable.", retryable=True)

    async def resume(self, *, thread_id: str) -> AgentState:
        raise NotImplementedError


async def _collect(events: AsyncIterator[object]) -> list[object]:
    return [event async for event in events]


@pytest.mark.asyncio
async def test_runtime_emits_started_before_invoking_graph_then_final() -> None:
    graph = SuccessfulGraph()
    runtime = NexusRuntime(graph)
    events = runtime.run("Say hello", session_id="opaque-session")

    first = await anext(events)
    assert isinstance(first, TaskStarted)
    assert not graph.called

    remaining = await _collect(events)
    assert graph.called
    assert len(remaining) == 1
    assert isinstance(remaining[0], FinalResult)
    final = remaining[0]
    assert final.content == "Hello!"
    assert final.run_id == first.run_id
    assert final.session_id == "opaque-session"
    assert first.timestamp.tzinfo is not None
    assert final.to_dict()["type"] == "FinalResult"


@pytest.mark.asyncio
async def test_runtime_emits_structured_terminal_error() -> None:
    events = await _collect(NexusRuntime(FailingGraph()).run("Say hello"))

    assert [type(event) for event in events] == [TaskStarted, ErrorOccurred]
    error = events[1]
    assert isinstance(error, ErrorOccurred)
    assert error.code == "MODEL_ERROR"
    assert error.message == "Model is temporarily unavailable."
    assert error.retryable is True
