"""Application-level orchestration for the Day 1 runtime lifecycle."""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

from nexus.application.execution_ledger import RuntimeEventBuffer
from nexus.application.session_service import SessionService
from nexus.domain.agent_state import AgentState
from nexus.domain.model import ModelMessage
from nexus.domain.planning import PlanApprovalResumeInput
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
        event_buffer: RuntimeEventBuffer | None = None,
    ) -> None:
        self._graph_runtime = graph_runtime
        self._session_service = session_service
        self._model_metadata = model_metadata
        self._event_buffer = event_buffer

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
                interrupted_events = self._drain(run_id)
                if self._session_service is None:
                    raise SessionError(
                        "Interrupted execution requires session persistence.",
                        code="SESSION_PERSISTENCE_REQUIRED",
                    )
                await self._session_service.mark_interrupted(run_id)
                for event in interrupted_events:
                    yield event
                yield RunInterrupted(run_id=run_id, session_id=session_id)
                return
            buffered = self._drain(run_id)
            final = _one_final_result(buffered)
            if final is None:
                content = self._final_content(result)
                final = FinalResult(run_id=run_id, session_id=session_id, content=content)
            if self._session_service is not None:
                await self._persist_final(run_id, result, final)
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

        for event in buffered:
            yield event
        if final not in buffered:
            yield final

    async def resume(
        self,
        session_id: str,
        *,
        resume_input: PlanApprovalResumeInput | None = None,
    ) -> AsyncIterator[RuntimeEvent]:
        """Resolve and resume the latest durable interrupted run for a session."""

        if self._session_service is None:
            raise SessionError(
                "Session persistence is not configured.",
                code="SESSION_PERSISTENCE_REQUIRED",
            )
        run = await self._session_service.resolve_resumable_run(session_id)
        yield TaskStarted(run_id=run.run_id, session_id=run.session_id, task=run.task)
        try:
            result = await self._graph_runtime.resume(
                thread_id=run.graph_thread_id,
                resume_input=resume_input,
            )
            if result.status is RuntimeStatus.INTERRUPTED:
                for event in self._drain(run.run_id):
                    yield event
                yield RunInterrupted(run_id=run.run_id, session_id=run.session_id)
                return
            buffered = self._drain(run.run_id)
            final = _one_final_result(buffered)
            if final is None:
                content = self._final_content(result)
                final = FinalResult(
                    run_id=run.run_id,
                    session_id=run.session_id,
                    content=content,
                )
            await self._persist_final(run.run_id, result, final)
        except NexusError as exc:
            try:
                await self._session_service.fail_run(
                    run.run_id,
                    code=exc.code,
                    message=str(exc),
                )
            except NexusError as persistence_exc:
                exc = persistence_exc
            yield ErrorOccurred(
                run_id=run.run_id,
                session_id=run.session_id,
                code=exc.code,
                message=str(exc),
                retryable=exc.retryable,
            )
            return
        except Exception:
            code = "RUNTIME_ERROR"
            message = "Nexus could not resume the task."
            retryable = False
            try:
                await self._session_service.fail_run(
                    run.run_id,
                    code=code,
                    message=message,
                )
            except NexusError as persistence_exc:
                code = persistence_exc.code
                message = str(persistence_exc)
                retryable = persistence_exc.retryable
            yield ErrorOccurred(
                run_id=run.run_id,
                session_id=run.session_id,
                code=code,
                message=message,
                retryable=retryable,
            )
            return
        for event in buffered:
            yield event
        if final not in buffered:
            yield final

    def _drain(self, run_id: str) -> tuple[RuntimeEvent, ...]:
        return () if self._event_buffer is None else self._event_buffer.drain(run_id)

    async def _persist_final(
        self,
        run_id: str,
        state: AgentState,
        event: FinalResult,
    ) -> None:
        if self._session_service is None:
            return
        if state.terminal_status is None:
            await self._session_service.complete_run(run_id, event.content)
            return
        await self._session_service.finalize_run(
            run_id,
            event,
            tool_call_count=state.tool_call_count,
            step_count=state.step_count,
            llm_call_count=state.llm_call_count,
            replan_count=state.replan_count,
            repair_count=state.repair_count,
        )

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


def _one_final_result(events: tuple[RuntimeEvent, ...]) -> FinalResult | None:
    finals = tuple(event for event in events if isinstance(event, FinalResult))
    if len(finals) > 1:
        raise NexusError("The graph emitted multiple final results.", code="GRAPH_INVALID_STATE")
    return None if not finals else finals[0]
