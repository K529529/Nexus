from datetime import UTC, datetime
from typing import cast
from uuid import uuid4

import pytest

from nexus.application.runtime import NexusRuntime
from nexus.application.session_service import SessionService
from nexus.domain.agent_state import AgentState
from nexus.domain.model import ModelMessage
from nexus.domain.persistence import Run, RunStatus
from nexus.domain.planning import PlanApprovalResumeInput
from nexus.domain.runtime_events import (
    ErrorOccurred,
    FinalResult,
    RunInterrupted,
    RuntimeStatus,
    TaskStarted,
)
from nexus.errors import ModelError, SessionError


def _run() -> Run:
    run_id = str(uuid4())
    return Run(
        run_id=run_id,
        session_id=str(uuid4()),
        task="finish the interrupted task",
        status=RunStatus.INTERRUPTED,
        model_metadata=None,
        started_at=datetime.now(UTC),
        finished_at=None,
        token_count=0,
        tool_call_count=0,
        changed_file_refs=None,
        final_outcome=None,
        graph_thread_id=f"nexus-run:{run_id}",
    )


class _ResumeGraph:
    def __init__(self, result: AgentState | Exception) -> None:
        self._result = result

    async def run(
        self, state: AgentState, *, thread_id: str | None = None
    ) -> AgentState:
        raise AssertionError("run is not used")

    async def resume(
        self,
        *,
        thread_id: str,
        resume_input: PlanApprovalResumeInput | None = None,
    ) -> AgentState:
        del thread_id, resume_input
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class _Sessions:
    def __init__(self, run: Run, *, fail_error: SessionError | None = None) -> None:
        self.run = run
        self.fail_error = fail_error
        self.completed_content: str | None = None
        self.failed: tuple[str, str] | None = None

    async def resolve_resumable_run(self, session_id: str) -> Run:
        assert session_id == self.run.session_id
        return self.run

    async def complete_run(self, run_id: str, content: str) -> Run:
        assert run_id == self.run.run_id
        self.completed_content = content
        return self.run

    async def fail_run(self, run_id: str, *, code: str, message: str) -> Run:
        assert run_id == self.run.run_id
        if self.fail_error is not None:
            raise self.fail_error
        self.failed = (code, message)
        return self.run


async def _events(runtime: NexusRuntime, session_id: str) -> list[object]:
    return [event async for event in runtime.resume(session_id)]


@pytest.mark.asyncio
async def test_legacy_resume_requires_session_persistence() -> None:
    runtime = NexusRuntime(_ResumeGraph(RuntimeError("unused")))

    with pytest.raises(SessionError, match="Session persistence is not configured"):
        await _events(runtime, str(uuid4()))


@pytest.mark.asyncio
async def test_legacy_resume_persists_and_emits_successful_final_result() -> None:
    run = _run()
    state = AgentState(
        task=run.task,
        messages=[ModelMessage("assistant", "completed safely")],
        run_id=run.run_id,
        session_id=run.session_id,
        status=RuntimeStatus.COMPLETED,
    )
    sessions = _Sessions(run)
    runtime = NexusRuntime(
        _ResumeGraph(state), session_service=cast(SessionService, sessions)
    )

    events = await _events(runtime, run.session_id)

    assert [type(event) for event in events] == [TaskStarted, FinalResult]
    final = events[-1]
    assert isinstance(final, FinalResult)
    assert final.content == "completed safely"
    assert sessions.completed_content == "completed safely"


@pytest.mark.asyncio
async def test_legacy_resume_preserves_an_interrupted_result() -> None:
    run = _run()
    state = AgentState(
        task=run.task,
        messages=[],
        run_id=run.run_id,
        session_id=run.session_id,
        status=RuntimeStatus.INTERRUPTED,
    )
    runtime = NexusRuntime(
        _ResumeGraph(state),
        session_service=cast(SessionService, _Sessions(run)),
    )

    events = await _events(runtime, run.session_id)

    assert isinstance(events[-1], RunInterrupted)
    assert not any(isinstance(event, FinalResult) for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("graph_error", "persistence_error", "expected_code", "expected_retryable"),
    [
        (ModelError("model unavailable", retryable=True), None, "MODEL_ERROR", True),
        (
            RuntimeError("private failure"),
            SessionError("persistence unavailable", code="SESSION_FAILURE", retryable=True),
            "SESSION_FAILURE",
            True,
        ),
    ],
)
async def test_legacy_resume_maps_graph_and_persistence_failures(
    graph_error: Exception,
    persistence_error: SessionError | None,
    expected_code: str,
    expected_retryable: bool,
) -> None:
    run = _run()
    sessions = _Sessions(run, fail_error=persistence_error)
    runtime = NexusRuntime(
        _ResumeGraph(graph_error), session_service=cast(SessionService, sessions)
    )

    events = await _events(runtime, run.session_id)

    error = events[-1]
    assert isinstance(error, ErrorOccurred)
    assert error.code == expected_code
    assert error.retryable is expected_retryable
