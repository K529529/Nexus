from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from nexus.application.session_service import SessionService
from nexus.domain.persistence import NexusSession, Repository, Run, RunStatus, SessionTurn
from nexus.infrastructure.database import DatabaseBootstrap
from nexus.infrastructure.persistence import SqlAlchemySessionUnitOfWorkFactory


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_repository_session_run_and_turn_persist_across_transactions(
    migrated_database_url: str,
) -> None:
    database = DatabaseBootstrap(migrated_database_url)
    factory = SqlAlchemySessionUnitOfWorkFactory(database.session_factory)
    now = datetime.now(UTC)
    repository_id = str(uuid4())
    session_id = str(uuid4())
    run_id = str(uuid4())
    try:
        async with factory() as unit_of_work:
            await unit_of_work.repositories.add(
                Repository(repository_id, "repo", {"language": "python"}, None, now)
            )
            await unit_of_work.sessions.add(
                NexusSession(session_id, repository_id, now, now, None)
            )
            await unit_of_work.runs.add(
                Run(
                    run_id,
                    session_id,
                    "test task",
                    RunStatus.RUNNING,
                    {"model": "mock"},
                    now,
                    None,
                    0,
                    0,
                    None,
                    None,
                    f"nexus-run:{run_id}",
                )
            )
            await unit_of_work.turns.add(
                SessionTurn(
                    str(uuid4()),
                    session_id,
                    run_id,
                    2,
                    "assistant",
                    "second",
                    {"kind": "result"},
                    now,
                )
            )
            await unit_of_work.turns.add(
                SessionTurn(
                    str(uuid4()), session_id, None, 1, "user", "first", None, now
                )
            )

        async with factory() as unit_of_work:
            repository = await unit_of_work.repositories.get(repository_id)
            session = await unit_of_work.sessions.get(session_id)
            run = await unit_of_work.runs.get(run_id)
            turns = await unit_of_work.turns.list_by_session(session_id)

        assert repository is not None and repository.metadata == {"language": "python"}
        assert session is not None and session.repository_id == repository_id
        assert run is not None and run.model_metadata == {"model": "mock"}
        assert [turn.sequence for turn in turns] == [1, 2]
        assert turns[0].run_id is None
        assert turns[1].metadata == {"kind": "result"}
    finally:
        await database.close()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_unit_of_work_rolls_back_all_writes(migrated_database_url: str) -> None:
    database = DatabaseBootstrap(migrated_database_url)
    factory = SqlAlchemySessionUnitOfWorkFactory(database.session_factory)
    repository_id = str(uuid4())
    session_id = str(uuid4())
    run_id = str(uuid4())
    now = datetime.now(UTC)
    try:
        with pytest.raises(RuntimeError, match="force rollback"):
            async with factory() as unit_of_work:
                await unit_of_work.repositories.add(
                    Repository(repository_id, "rollback-repo", {}, None, now)
                )
                await unit_of_work.sessions.add(
                    NexusSession(session_id, repository_id, now, now, None)
                )
                await unit_of_work.runs.add(
                    Run(
                        run_id,
                        session_id,
                        "rollback task",
                        RunStatus.RUNNING,
                        None,
                        now,
                        None,
                        0,
                        0,
                        None,
                        None,
                        f"nexus-run:{run_id}",
                    )
                )
                await unit_of_work.turns.add(
                    SessionTurn(
                        str(uuid4()),
                        session_id,
                        run_id,
                        1,
                        "user",
                        "rollback task",
                        None,
                        now,
                    )
                )
                raise RuntimeError("force rollback")

        async with factory() as unit_of_work:
            assert await unit_of_work.repositories.get(repository_id) is None
            assert await unit_of_work.sessions.get(session_id) is None
            assert await unit_of_work.runs.get(run_id) is None
            assert await unit_of_work.turns.list_by_session(session_id) == []
    finally:
        await database.close()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_session_service_persists_lifecycle_and_history(
    migrated_database_url: str,
    tmp_path: Path,
) -> None:
    database = DatabaseBootstrap(migrated_database_url)
    factory = SqlAlchemySessionUnitOfWorkFactory(database.session_factory)
    service = SessionService(factory, tmp_path)
    run_id = str(uuid4())
    try:
        run = await service.start_run(
            run_id=run_id,
            task="durable task",
            session_id=None,
            model_metadata={"model": "mock"},
        )
        summaries = await service.list_sessions()
        assert len(summaries) == 1
        assert summaries[0].session_id == run.session_id
        assert summaries[0].resumable is False
        await service.mark_interrupted(run_id)
        assert (await service.list_sessions())[0].resumable is True
        resolved = await service.resolve_resumable_run(run.session_id)
        assert resolved.run_id == run_id
        await service.complete_run(run_id, "done")

        async with factory() as unit_of_work:
            persisted = await unit_of_work.runs.get(run_id)
            turns = await unit_of_work.turns.list_by_session(run.session_id)
        assert persisted is not None
        assert persisted.status is RunStatus.COMPLETED
        assert persisted.final_outcome == {"content": "done"}
        assert [(turn.sequence, turn.role, turn.content) for turn in turns] == [
            (1, "user", "durable task"),
            (2, "assistant", "done"),
        ]
    finally:
        await database.close()
