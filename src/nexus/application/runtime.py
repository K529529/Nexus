"""Application-level orchestration for the Day 1 runtime lifecycle."""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

from nexus.domain.agent_state import AgentState
from nexus.domain.model import ModelMessage
from nexus.domain.ports.graph_runtime import GraphRuntime
from nexus.domain.runtime_events import (
    ErrorOccurred,
    FinalResult,
    RuntimeEvent,
    RuntimeStatus,
    TaskStarted,
)
from nexus.errors import NexusError


class NexusRuntime:
    """Stable, async-first entry point shared by all future adapters."""

    def __init__(self, graph_runtime: GraphRuntime) -> None:
        self._graph_runtime = graph_runtime

    async def run(
        self,
        task: str,
        session_id: str | None = None,
    ) -> AsyncIterator[RuntimeEvent]:
        """Emit the minimal model-backed Day 1 lifecycle."""

        run_id = str(uuid4())
        normalized_task = task.strip()
        if not normalized_task:
            yield TaskStarted(run_id=run_id, session_id=session_id, task=task)
            yield ErrorOccurred(
                run_id=run_id,
                session_id=session_id,
                code="INVALID_TASK",
                message="Task must be a non-empty string.",
                retryable=False,
            )
            return

        yield TaskStarted(run_id=run_id, session_id=session_id, task=normalized_task)
        state = AgentState(
            task=normalized_task,
            messages=[ModelMessage(role="user", content=normalized_task)],
            run_id=run_id,
            session_id=session_id,
            status=RuntimeStatus.STARTED,
        )

        try:
            result = await self._graph_runtime.run(state)
            content = self._final_content(result)
        except NexusError as exc:
            yield ErrorOccurred(
                run_id=run_id,
                session_id=session_id,
                code=exc.code,
                message=str(exc),
                retryable=exc.retryable,
            )
            return
        except Exception:
            yield ErrorOccurred(
                run_id=run_id,
                session_id=session_id,
                code="RUNTIME_ERROR",
                message="Nexus could not complete the task.",
                retryable=False,
            )
            return

        yield FinalResult(run_id=run_id, session_id=session_id, content=content)

    @staticmethod
    def _final_content(state: AgentState) -> str:
        if state.status is not RuntimeStatus.COMPLETED:
            raise NexusError("The graph did not complete successfully.", code="GRAPH_INCOMPLETE")
        if not state.messages or state.messages[-1].role != "assistant":
            raise NexusError("The graph returned no assistant response.", code="GRAPH_NO_RESULT")
        content = state.messages[-1].content.strip()
        if not content:
            raise NexusError(
                "The graph returned an empty assistant response.",
                code="EMPTY_MODEL_OUTPUT",
            )
        return content
