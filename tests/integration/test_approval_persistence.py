from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from nexus.application.approval_service import ApprovalService
from nexus.domain.approvals import ApprovalRequest
from nexus.domain.persistence import NexusSession, Repository, Run, RunStatus
from nexus.domain.tooling import ApprovalDecision, RiskLevel, ToolInvocation
from nexus.errors import PermissionDeniedError, ToolExecutionError
from nexus.infrastructure.database import DatabaseBootstrap
from nexus.infrastructure.persistence import (
    SqlAlchemyApprovalUnitOfWorkFactory,
    SqlAlchemySessionUnitOfWorkFactory,
)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_approval_repository_persists_pending_and_terminal_decisions(
    migrated_database_url: str,
) -> None:
    database = DatabaseBootstrap(migrated_database_url)
    session_factory = SqlAlchemySessionUnitOfWorkFactory(database.session_factory)
    approval_factory = SqlAlchemyApprovalUnitOfWorkFactory(database.session_factory)
    now = datetime.now(UTC)
    repository_id = str(uuid4())
    session_id = str(uuid4())
    run_id = str(uuid4())
    approval_id = str(uuid4())
    try:
        await _persist_run(
            session_factory,
            repository_id=repository_id,
            session_id=session_id,
            run_id=run_id,
            now=now,
        )
        pending = ApprovalRequest(
            approval_id,
            run_id,
            session_id,
            "shell",
            RiskLevel.WRITE,
            "shell:pytest",
            ApprovalDecision.PENDING,
            "runtime",
            None,
            now,
            None,
        )
        async with approval_factory() as unit_of_work:
            await unit_of_work.approvals.add(pending)

        decided_at = datetime.now(UTC)
        approved = replace(
            pending,
            decision=ApprovalDecision.APPROVED,
            actor="user",
            reason="approved",
            decided_at=decided_at,
        )
        async with approval_factory() as unit_of_work:
            await unit_of_work.approvals.update(approved)

        async with approval_factory() as unit_of_work:
            persisted = await unit_of_work.approvals.get(approval_id)
            approvals = await unit_of_work.approvals.list_by_run(run_id)

        assert persisted == approved
        assert approvals == [approved]
        assert persisted is not None and persisted.decided_at == decided_at

        with pytest.raises(ToolExecutionError) as raised:
            await ApprovalService(approval_factory).persist_decision(
                replace(
                    approved,
                    decision=ApprovalDecision.DENIED,
                    actor="user",
                    reason="attempted second decision",
                    decided_at=datetime.now(UTC),
                )
            )
        assert raised.value.code == "INVALID_APPROVAL_TRANSITION"

        denied = await ApprovalService(approval_factory).record_denied(
            ToolInvocation(
                str(uuid4()),
                "shell",
                {"argv": ["git", "push"]},
                run_id,
                session_id,
            ),
            risk_level=RiskLevel.DANGEROUS,
            summary="shell:git",
            reason="dangerous command",
        )
        assert denied.decision is ApprovalDecision.DENIED
        assert denied.actor == "security_policy"
        async with approval_factory() as unit_of_work:
            assert await unit_of_work.approvals.list_by_run(run_id) == [approved, denied]
    finally:
        await database.close()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_approval_service_enforces_run_session_correlation(
    migrated_database_url: str,
) -> None:
    database = DatabaseBootstrap(migrated_database_url)
    session_factory = SqlAlchemySessionUnitOfWorkFactory(database.session_factory)
    approval_factory = SqlAlchemyApprovalUnitOfWorkFactory(database.session_factory)
    now = datetime.now(UTC)
    repository_id = str(uuid4())
    session_id = str(uuid4())
    other_session_id = str(uuid4())
    run_id = str(uuid4())
    try:
        await _persist_run(
            session_factory,
            repository_id=repository_id,
            session_id=session_id,
            run_id=run_id,
            now=now,
        )
        async with session_factory() as unit_of_work:
            await unit_of_work.sessions.add(
                NexusSession(other_session_id, repository_id, now, now, None)
            )
        invocation = ToolInvocation(
            str(uuid4()),
            "shell",
            {"argv": ["uv", "run", "pytest"]},
            run_id,
            other_session_id,
        )

        with pytest.raises(PermissionDeniedError) as raised:
            await ApprovalService(approval_factory).create_pending(
                invocation,
                risk_level=RiskLevel.WRITE,
                summary="shell:uv",
            )
        assert raised.value.code == "APPROVAL_CORRELATION_DENIED"

        async with approval_factory() as unit_of_work:
            assert await unit_of_work.approvals.list_by_run(run_id) == []
    finally:
        await database.close()


async def _persist_run(
    factory: SqlAlchemySessionUnitOfWorkFactory,
    *,
    repository_id: str,
    session_id: str,
    run_id: str,
    now: datetime,
) -> None:
    async with factory() as unit_of_work:
        await unit_of_work.repositories.add(Repository(repository_id, "repo", {}, None, now))
        await unit_of_work.sessions.add(
            NexusSession(session_id, repository_id, now, now, None)
        )
        await unit_of_work.runs.add(
            Run(
                run_id,
                session_id,
                "approval task",
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
