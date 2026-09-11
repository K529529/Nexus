"""Application-level orchestration for the Day 1 runtime lifecycle."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from uuid import uuid4

from nexus.application.event_publisher import InProcessEventPublisher, LiveRuntimeEventBridge
from nexus.application.execution_context import bind_execution_context
from nexus.application.execution_ledger import RuntimeEventBuffer, ToolExecutionLedger
from nexus.application.session_service import SessionService
from nexus.application.telemetry_subscriber import TelemetrySubscriber
from nexus.domain.agent_state import AgentState
from nexus.domain.model import ModelMessage, TokenUsageAggregate
from nexus.domain.observability import (
    ExecutionOutcome,
    RunExecutionContext,
    TraceRunFinish,
    TraceRunStart,
    TraceValidationStatus,
)
from nexus.domain.persistence import Run
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

_LiveOperation = Callable[[], Awaitable[tuple[RuntimeEvent, AgentState | None]]]


class NexusRuntime:
    """Stable, async-first entry point shared by all future adapters."""

    def __init__(
        self,
        graph_runtime: GraphRuntime,
        *,
        session_service: SessionService | None = None,
        model_metadata: dict[str, object] | None = None,
        event_buffer: RuntimeEventBuffer | LiveRuntimeEventBridge | None = None,
        event_publisher: InProcessEventPublisher | None = None,
        telemetry_subscriber: TelemetrySubscriber | None = None,
        ledger: ToolExecutionLedger | None = None,
    ) -> None:
        self._graph_runtime = graph_runtime
        self._session_service = session_service
        self._model_metadata = model_metadata
        self._event_buffer = event_buffer
        self._event_publisher = event_publisher
        self._telemetry_subscriber = telemetry_subscriber
        self._ledger = ledger

    async def run(
        self,
        task: str,
        session_id: str | None = None,
    ) -> AsyncIterator[RuntimeEvent]:
        """Emit the minimal model-backed Day 1 lifecycle."""

        if self._event_publisher is not None:
            async for event in self._run_live(task, session_id):
                yield event
            return

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

        if self._event_publisher is not None:
            async for event in self._resume_live(session_id, resume_input=resume_input):
                yield event
            return

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

    async def _run_live(
        self, task: str, session_id: str | None
    ) -> AsyncIterator[RuntimeEvent]:
        run_id = str(uuid4())
        execution_id = str(uuid4())
        normalized_task = task.strip()
        persisted_run: Run | None = None
        start_error: NexusError | None = None
        if normalized_task and self._session_service is not None:
            try:
                persisted_run = await self._session_service.start_run(
                    run_id=run_id,
                    task=normalized_task,
                    session_id=session_id,
                    model_metadata=self._model_metadata,
                )
                session_id = persisted_run.session_id
            except NexusError as exc:
                start_error = exc

        context = RunExecutionContext(
            trace_id=run_id,
            execution_id=execution_id,
            run_id=run_id,
            session_id=session_id,
            is_resume=False,
            started_at=datetime.now(UTC),
        )

        async def operation() -> tuple[RuntimeEvent, AgentState | None]:
            if not normalized_task:
                return (
                    ErrorOccurred(
                        run_id=run_id,
                        session_id=session_id,
                        code="INVALID_TASK",
                        message="Task must be a non-empty string.",
                        retryable=False,
                    ),
                    None,
                )
            if start_error is not None:
                return (
                    ErrorOccurred(
                        run_id=run_id,
                        session_id=session_id,
                        code=start_error.code,
                        message=str(start_error),
                        retryable=start_error.retryable,
                    ),
                    None,
                )
            state = AgentState(
                task=normalized_task,
                messages=[ModelMessage(role="user", content=normalized_task)],
                run_id=run_id,
                session_id=session_id,
                status=RuntimeStatus.STARTED,
            )
            return await self._execute_live_run(state, persisted_run)

        display_task = normalized_task if normalized_task else task
        async for event in self._stream_live(context, display_task, operation):
            yield event

    async def _resume_live(
        self,
        session_id: str,
        *,
        resume_input: PlanApprovalResumeInput | None,
    ) -> AsyncIterator[RuntimeEvent]:
        if self._session_service is None:
            raise SessionError(
                "Session persistence is not configured.",
                code="SESSION_PERSISTENCE_REQUIRED",
            )
        run = await self._session_service.resolve_resumable_run(session_id)
        context = RunExecutionContext(
            trace_id=run.run_id,
            execution_id=str(uuid4()),
            run_id=run.run_id,
            session_id=run.session_id,
            is_resume=True,
            started_at=datetime.now(UTC),
        )

        async def operation() -> tuple[RuntimeEvent, AgentState | None]:
            return await self._execute_live_resume(run, resume_input)

        async for event in self._stream_live(context, run.task, operation):
            yield event

    async def _stream_live(
        self,
        context: RunExecutionContext,
        task: str,
        operation: _LiveOperation,
    ) -> AsyncIterator[RuntimeEvent]:
        publisher = self._event_publisher
        if publisher is None:
            raise RuntimeError("Live event publisher is not configured.")
        publisher.open_execution(context)
        if self._telemetry_subscriber is not None:
            self._telemetry_subscriber.start_execution(
                TraceRunStart(
                    context=context,
                    task_character_count=len(task),
                    model_provider=_metadata_text(self._model_metadata, "provider"),
                    model_name=_metadata_text(self._model_metadata, "model"),
                )
            )

        started = time.perf_counter()
        with bind_execution_context(context):
            await publisher.publish(
                TaskStarted(
                    run_id=context.run_id,
                    session_id=context.session_id,
                    task=task,
                )
            )

        async def produce() -> None:
            with bind_execution_context(context):
                terminal, state = await operation()
                await publisher.publish(terminal)
                if self._telemetry_subscriber is not None:
                    self._telemetry_subscriber.finish_execution(
                        self._trace_finish(context, terminal, state, started)
                    )

        producer = asyncio.create_task(produce())
        try:
            async for event in publisher.events(context.execution_id):
                yield event
            await producer
        finally:
            if not producer.done():
                producer.cancel()
                await asyncio.gather(producer, return_exceptions=True)
            publisher.close_execution(context.execution_id)

    async def _execute_live_run(
        self, state: AgentState, persisted_run: Run | None
    ) -> tuple[RuntimeEvent, AgentState | None]:
        try:
            if persisted_run is None:
                result = await self._graph_runtime.run(state)
            else:
                result = await self._graph_runtime.run(
                    state, thread_id=persisted_run.graph_thread_id
                )
            if result.status is RuntimeStatus.INTERRUPTED:
                self._drain(state.run_id)
                if self._session_service is None:
                    raise SessionError(
                        "Interrupted execution requires session persistence.",
                        code="SESSION_PERSISTENCE_REQUIRED",
                    )
                await self._session_service.mark_interrupted(state.run_id)
                return (
                    RunInterrupted(run_id=state.run_id, session_id=state.session_id),
                    result,
                )
            buffered = self._drain(state.run_id)
            final = _one_final_result(buffered)
            if final is None:
                final = FinalResult(
                    run_id=state.run_id,
                    session_id=state.session_id,
                    content=self._final_content(result),
                )
            await self._persist_final(state.run_id, result, final)
            return final, result
        except NexusError as exc:
            return await self._live_failure(state.run_id, state.session_id, persisted_run, exc)
        except Exception:
            error = NexusError(
                "Nexus could not complete the task.",
                code="RUNTIME_ERROR",
            )
            return await self._live_failure(
                state.run_id, state.session_id, persisted_run, error
            )

    async def _execute_live_resume(
        self, run: Run, resume_input: PlanApprovalResumeInput | None
    ) -> tuple[RuntimeEvent, AgentState | None]:
        try:
            result = await self._graph_runtime.resume(
                thread_id=run.graph_thread_id,
                resume_input=resume_input,
            )
            if result.status is RuntimeStatus.INTERRUPTED:
                self._drain(run.run_id)
                return RunInterrupted(run_id=run.run_id, session_id=run.session_id), result
            buffered = self._drain(run.run_id)
            final = _one_final_result(buffered)
            if final is None:
                final = FinalResult(
                    run_id=run.run_id,
                    session_id=run.session_id,
                    content=self._final_content(result),
                )
            await self._persist_final(run.run_id, result, final)
            return final, result
        except NexusError as exc:
            return await self._live_failure(run.run_id, run.session_id, run, exc)
        except Exception:
            error = NexusError(
                "Nexus could not resume the task.", code="RUNTIME_ERROR"
            )
            return await self._live_failure(run.run_id, run.session_id, run, error)

    async def _live_failure(
        self,
        run_id: str,
        session_id: str | None,
        persisted_run: Run | None,
        error: NexusError,
    ) -> tuple[RuntimeEvent, None]:
        if self._session_service is not None and persisted_run is not None:
            try:
                await self._session_service.fail_run(
                    run_id, code=error.code, message=str(error)
                )
            except NexusError as persistence_error:
                error = persistence_error
        return (
            ErrorOccurred(
                run_id=run_id,
                session_id=session_id,
                code=error.code,
                message=str(error),
                retryable=error.retryable,
            ),
            None,
        )

    def _trace_finish(
        self,
        context: RunExecutionContext,
        terminal: RuntimeEvent,
        state: AgentState | None,
        started: float,
    ) -> TraceRunFinish:
        if isinstance(terminal, FinalResult):
            outcome = (
                ExecutionOutcome.COMPLETED
                if terminal.status is RuntimeStatus.COMPLETED
                else ExecutionOutcome.FAILED
            )
            terminal_status = terminal.terminal_status
        elif isinstance(terminal, RunInterrupted):
            outcome = ExecutionOutcome.INTERRUPTED
            terminal_status = None
        else:
            outcome = ExecutionOutcome.FAILED
            terminal_status = None
        aggregate = TokenUsageAggregate() if state is None else state.token_usage
        if self._ledger is not None:
            ledger_usage = self._ledger.token_usage(context.run_id)
            if ledger_usage.call_count:
                aggregate = ledger_usage
        validation = TraceValidationStatus.NOT_RUN
        if state is not None and state.validation_result is not None:
            validation = TraceValidationStatus(state.validation_result.status.value)
        llm_call_count = 0 if state is None else state.llm_call_count
        if self._ledger is not None:
            llm_call_count = self._ledger.model_count(context.run_id)
        return TraceRunFinish(
            context=context,
            finished_at=datetime.now(UTC),
            execution_outcome=outcome,
            runtime_status=terminal.status,
            terminal_status=terminal_status,
            duration_ms=max(0, int((time.perf_counter() - started) * 1000)),
            step_count=0 if state is None else state.step_count,
            llm_call_count=llm_call_count,
            tool_call_count=0 if state is None else state.tool_call_count,
            replan_count=0 if state is None else state.replan_count,
            repair_count=0 if state is None else state.repair_count,
            token_usage=aggregate.to_usage(),
            changed_file_count=0 if state is None else len(state.changed_files),
            validation_status=validation,
            error_code=terminal.code if isinstance(terminal, ErrorOccurred) else None,
        )

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
        if state.terminal_status is None and self._event_publisher is None:
            await self._session_service.complete_run(run_id, event.content)
            return
        aggregate = state.token_usage
        if self._ledger is not None:
            ledger_usage = self._ledger.token_usage(run_id)
            if ledger_usage.call_count:
                aggregate = ledger_usage
        llm_call_count = state.llm_call_count
        if self._ledger is not None:
            llm_call_count = self._ledger.model_count(run_id)
        await self._session_service.finalize_run(
            run_id,
            event,
            tool_call_count=state.tool_call_count,
            step_count=state.step_count,
            llm_call_count=llm_call_count,
            replan_count=state.replan_count,
            repair_count=state.repair_count,
            token_usage=aggregate,
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


def _metadata_text(metadata: dict[str, object] | None, key: str) -> str | None:
    if metadata is None:
        return None
    value = metadata.get(key)
    return value if isinstance(value, str) else None
