"""Application-level orchestration for the Day 1 runtime lifecycle."""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

from nexus.application.session_service import SessionService
from nexus.domain.agent_state import AgentState
from nexus.domain.model import ModelMessage
from nexus.domain.ports.graph_runtime import GraphRuntime
from nexus.domain.runtime_events import (
    ErrorOccurred,
    FinalResult,
    RunInterrupted,
    RuntimeEvent,
    RuntimeStatus,
    TaskStarted,
)
from nexus.errors import NexusError, SessionError


class NexusRuntime:
    """Stable, async-first entry point shared by all future adapters."""

    def __init__(
        self,
        graph_runtime: GraphRuntime,
        *,
        session_service: SessionService | None = None,
        model_metadata: dict[str, object] | None = None,
    ) -> None:
        self._graph_runtime = graph_runtime
        self._session_service = session_service
        self._model_metadata = model_metadata

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

        persisted_run = None
        if self._session_service is not None:
            try:
                persisted_run = await self._session_service.start_run(
                    run_id=run_id,
                    task=normalized_task,
                    session_id=session_id,
                    model_metadata=self._model_metadata,
                )
                session_id = persisted_run.session_id
            except NexusError as exc:
                yield TaskStarted(run_id=run_id, session_id=session_id, task=normalized_task)
                yield ErrorOccurred(
                    run_id=run_id,
                    session_id=session_id,
                    code=exc.code,
                    message=str(exc),
                    retryable=exc.retryable,
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
            if persisted_run is None:
                result = await self._graph_runtime.run(state)
            else:
                result = await self._graph_runtime.run(
                    state,
                    thread_id=persisted_run.graph_thread_id,
                )
            if result.status is RuntimeStatus.INTERRUPTED:
                if self._session_service is None:
                    raise SessionError(
                        "Interrupted execution requires session persistence.",
                        code="SESSION_PERSISTENCE_REQUIRED",
                    )
                await self._session_service.mark_interrupted(run_id)
                yield RunInterrupted(run_id=run_id, session_id=session_id)
                return
            content = self._final_content(result)
            if self._session_service is not None:
                await self._session_service.complete_run(run_id, content)
        except NexusError as exc:
            if self._session_service is not None and persisted_run is not None:
                try:
                    await self._session_service.fail_run(
                        run_id,
                        code=exc.code,
                        message=str(exc),
                    )
                except NexusError as persistence_exc:
                    exc = persistence_exc
            yield ErrorOccurred(
                run_id=run_id,
                session_id=session_id,
                code=exc.code,
                message=str(exc),
                retryable=exc.retryable,
            )
            return
        except Exception:
            if self._session_service is not None and persisted_run is not None:
                try:
                    await self._session_service.fail_run(
                        run_id,
                        code="RUNTIME_ERROR",
                        message="Nexus could not complete the task.",
                    )
                except NexusError:
                    pass
            yield ErrorOccurred(
                run_id=run_id,
                session_id=session_id,
                code="RUNTIME_ERROR",
                message="Nexus could not complete the task.",
                retryable=False,
            )
            return

        yield FinalResult(run_id=run_id, session_id=session_id, content=content)

    async def resume(self, session_id: str) -> AsyncIterator[RuntimeEvent]:
        """Resolve and resume the latest durable interrupted run for a session."""

        if self._session_service is None:
            raise SessionError(
                "Session persistence is not configured.",
                code="SESSION_PERSISTENCE_REQUIRED",
            )
        run = await self._session_service.resolve_resumable_run(session_id)
        yield TaskStarted(run_id=run.run_id, session_id=run.session_id, task=run.task)
        try:
            result = await self._graph_runtime.resume(thread_id=run.graph_thread_id)
            content = self._final_content(result)
            await self._session_service.complete_run(run.run_id, content)
        except NexusError as exc:
            yield ErrorOccurred(
                run_id=run.run_id,
                session_id=run.session_id,
                code=exc.code,
                message=str(exc),
                retryable=exc.retryable,
            )
            return
        except Exception:
            yield ErrorOccurred(
                run_id=run.run_id,
                session_id=run.session_id,
                code="RUNTIME_ERROR",
                message="Nexus could not resume the task.",
                retryable=False,
            )
            return
        yield FinalResult(run_id=run.run_id, session_id=run.session_id, content=content)

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
