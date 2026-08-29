"""Day 2 session business rules and transaction orchestration."""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from nexus.domain.persistence import (
    NexusSession,
    Repository,
    Run,
    RunStatus,
    SessionSummary,
    SessionTurn,
)
from nexus.domain.ports.session_unit_of_work import SessionUnitOfWork, SessionUnitOfWorkFactory
from nexus.errors import SessionError


class SessionService:
    """Own Day 2 session, run, history, and repository-safety decisions."""

    def __init__(
        self,
        unit_of_work_factory: SessionUnitOfWorkFactory,
        workspace_path: Path,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._canonical_repository_path = canonicalize_repository_path(workspace_path)

    async def start_run(
        self,
        *,
        run_id: str,
        task: str,
        session_id: str | None,
        model_metadata: dict[str, object] | None,
    ) -> Run:
        """Atomically establish repository, session, run, and initiating turn."""

        canonical_run_id = _canonical_uuid(run_id, "run")
        requested_session_id = (
            None if session_id is None else _canonical_uuid(session_id, "session")
        )
        now = datetime.now(UTC)
        try:
            async with self._unit_of_work_factory() as unit_of_work:
                repository = await self._get_or_create_repository(unit_of_work, now)
                session = await self._get_or_create_owned_session(
                    unit_of_work,
                    repository,
                    requested_session_id,
                    now,
                )
                run = Run(
                    run_id=canonical_run_id,
                    session_id=session.session_id,
                    task=task,
                    status=RunStatus.RUNNING,
                    model_metadata=None if model_metadata is None else dict(model_metadata),
                    started_at=now,
                    finished_at=None,
                    token_count=0,
                    tool_call_count=0,
                    changed_file_refs=None,
                    final_outcome=None,
                    graph_thread_id=f"nexus-run:{canonical_run_id}",
                )
                await unit_of_work.runs.add(run)
                sequence = await unit_of_work.turns.next_sequence(session.session_id)
                await unit_of_work.turns.add(
                    SessionTurn(
                        id=str(uuid4()),
                        session_id=session.session_id,
                        run_id=run.run_id,
                        sequence=sequence,
                        role="user",
                        content=task,
                        metadata=None,
                        created_at=now,
                    )
                )
                await unit_of_work.sessions.update(replace(session, last_active_at=now))
                return run
        except SessionError:
            raise
        except Exception as exc:
            raise SessionError(
                "Nexus could not persist the new session run.",
                code="SESSION_PERSISTENCE_ERROR",
                retryable=True,
            ) from exc

    async def list_sessions(self) -> list[SessionSummary]:
        """List current-repository sessions in deterministic recency order."""

        try:
            async with self._unit_of_work_factory() as unit_of_work:
                repository = await unit_of_work.repositories.get_by_canonical_path(
                    self._canonical_repository_path
                )
                if repository is None:
                    return []
                sessions = await unit_of_work.sessions.list_by_repository(
                    repository.repository_id
                )
                return [
                    SessionSummary(
                        session_id=session.session_id,
                        last_active_at=session.last_active_at,
                        resumable=await unit_of_work.runs.has_interrupted(session.session_id),
                    )
                    for session in sessions
                ]
        except Exception as exc:
            raise SessionError(
                "Nexus could not list persisted sessions.",
                code="SESSION_PERSISTENCE_ERROR",
                retryable=True,
            ) from exc

    async def resolve_resumable_run(self, session_id: str) -> Run:
        """Validate ownership before resolving the latest interrupted run."""

        canonical_session_id = _canonical_uuid(session_id, "session")
        try:
            async with self._unit_of_work_factory() as unit_of_work:
                session = await self._get_owned_session(unit_of_work, canonical_session_id)
                run = await unit_of_work.runs.get_latest_interrupted(session.session_id)
                if run is None:
                    raise SessionError(
                        "The session has no interrupted run to resume.",
                        code="SESSION_NOT_RESUMABLE",
                    )
                return run
        except SessionError:
            raise
        except Exception as exc:
            raise SessionError(
                "Nexus could not resolve the resumable session run.",
                code="SESSION_PERSISTENCE_ERROR",
                retryable=True,
            ) from exc

    async def mark_interrupted(self, run_id: str) -> Run:
        """Record interruption only after the graph checkpoint is durable."""

        return await self._transition_run(
            run_id,
            status=RunStatus.INTERRUPTED,
            final_outcome=None,
            finished_at=None,
            assistant_content=None,
        )

    async def complete_run(self, run_id: str, content: str) -> Run:
        """Atomically persist a terminal outcome and assistant history turn."""

        return await self._transition_run(
            run_id,
            status=RunStatus.COMPLETED,
            final_outcome={"content": content},
            finished_at=datetime.now(UTC),
            assistant_content=content,
        )

    async def fail_run(self, run_id: str, *, code: str, message: str) -> Run:
        """Persist a safe terminal failure without raw infrastructure details."""

        return await self._transition_run(
            run_id,
            status=RunStatus.FAILED,
            final_outcome={"error": {"code": code, "message": message}},
            finished_at=datetime.now(UTC),
            assistant_content=None,
        )

    async def _transition_run(
        self,
        run_id: str,
        *,
        status: RunStatus,
        final_outcome: dict[str, object] | None,
        finished_at: datetime | None,
        assistant_content: str | None,
    ) -> Run:
        canonical_run_id = _canonical_uuid(run_id, "run")
        now = datetime.now(UTC)
        try:
            async with self._unit_of_work_factory() as unit_of_work:
                run = await unit_of_work.runs.get(canonical_run_id)
                if run is None:
                    raise SessionError("The run does not exist.", code="RUN_NOT_FOUND")
                if status is RunStatus.INTERRUPTED and run.status is not RunStatus.RUNNING:
                    raise SessionError(
                        "Only a running run may become interrupted.",
                        code="INVALID_RUN_TRANSITION",
                    )
                if status is RunStatus.COMPLETED and run.status not in {
                    RunStatus.RUNNING,
                    RunStatus.INTERRUPTED,
                }:
                    raise SessionError(
                        "Only a running or interrupted run may complete.",
                        code="INVALID_RUN_TRANSITION",
                    )
                if status is RunStatus.FAILED and run.status not in {
                    RunStatus.RUNNING,
                    RunStatus.INTERRUPTED,
                }:
                    raise SessionError(
                        "Only a running or interrupted run may fail.",
                        code="INVALID_RUN_TRANSITION",
                    )
                updated = replace(
                    run,
                    status=status,
                    final_outcome=final_outcome,
                    finished_at=finished_at,
                )
                await unit_of_work.runs.update(updated)
                session = await unit_of_work.sessions.get(run.session_id)
                if session is None:
                    raise SessionError("The run session does not exist.", code="SESSION_NOT_FOUND")
                await unit_of_work.sessions.update(replace(session, last_active_at=now))
                if assistant_content is not None:
                    sequence = await unit_of_work.turns.next_sequence(session.session_id)
                    await unit_of_work.turns.add(
                        SessionTurn(
                            id=str(uuid4()),
                            session_id=session.session_id,
                            run_id=run.run_id,
                            sequence=sequence,
                            role="assistant",
                            content=assistant_content,
                            metadata=None,
                            created_at=now,
                        )
                    )
                return updated
        except SessionError:
            raise
        except Exception as exc:
            raise SessionError(
                "Nexus could not persist the run state.",
                code="SESSION_PERSISTENCE_ERROR",
                retryable=True,
            ) from exc

    async def _get_or_create_repository(
        self,
        unit_of_work: SessionUnitOfWork,
        now: datetime,
    ) -> Repository:
        repository = await unit_of_work.repositories.get_by_canonical_path(
            self._canonical_repository_path
        )
        if repository is not None:
            return repository
        repository = Repository(
            repository_id=str(uuid4()),
            canonical_path=self._canonical_repository_path,
            metadata={},
            configuration_reference=None,
            created_at=now,
        )
        await unit_of_work.repositories.add(repository)
        return repository

    async def _get_or_create_owned_session(
        self,
        unit_of_work: SessionUnitOfWork,
        repository: Repository,
        session_id: str | None,
        now: datetime,
    ) -> NexusSession:
        if session_id is not None:
            session = await self._get_owned_session(unit_of_work, session_id)
            if session.repository_id != repository.repository_id:
                raise SessionError(
                    "The session belongs to a different repository.",
                    code="SESSION_REPOSITORY_MISMATCH",
                )
            return session
        session = NexusSession(
            session_id=str(uuid4()),
            repository_id=repository.repository_id,
            created_at=now,
            last_active_at=now,
            configuration_reference=None,
        )
        await unit_of_work.sessions.add(session)
        return session

    async def _get_owned_session(
        self,
        unit_of_work: SessionUnitOfWork,
        session_id: str,
    ) -> NexusSession:
        session = await unit_of_work.sessions.get(session_id)
        if session is None:
            raise SessionError("The session does not exist.", code="SESSION_NOT_FOUND")
        repository = await unit_of_work.repositories.get(session.repository_id)
        if repository is None:
            raise SessionError(
                "The session repository no longer exists.",
                code="SESSION_REPOSITORY_MISSING",
            )
        if repository.canonical_path != self._canonical_repository_path:
            raise SessionError(
                "The session belongs to a different repository.",
                code="SESSION_REPOSITORY_MISMATCH",
            )
        return session


def canonicalize_repository_path(path: Path) -> str:
    """Resolve an existing workspace using platform-native case normalization."""

    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise SessionError(
            "The active repository path does not exist.",
            code="REPOSITORY_PATH_INVALID",
        ) from exc
    if not resolved.is_dir():
        raise SessionError(
            "The active repository path is not a directory.",
            code="REPOSITORY_PATH_INVALID",
        )
    return os.path.normcase(str(resolved))


def _canonical_uuid(value: str, label: str) -> str:
    try:
        return str(UUID(value))
    except ValueError as exc:
        raise SessionError(
            f"The {label} identifier is invalid.", code="INVALID_SESSION_ID"
        ) from exc
