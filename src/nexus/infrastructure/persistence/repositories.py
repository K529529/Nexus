"""Async SQLAlchemy implementations of repository ports through Day 3."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from nexus.domain.approvals import ApprovalRequest
from nexus.domain.persistence import NexusSession, Repository, Run, RunStatus, SessionTurn
from nexus.domain.tooling import ApprovalDecision, RiskLevel
from nexus.infrastructure.persistence.models import (
    ApprovalRow,
    RepositoryRow,
    RunRow,
    SessionRow,
    SessionTurnRow,
)


class SqlAlchemyApprovalRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, approval: ApprovalRequest) -> None:
        self._session.add(_approval_to_row(approval))
        await self._session.flush()

    async def get(self, approval_id: str) -> ApprovalRequest | None:
        row = await self._session.get(ApprovalRow, approval_id)
        return None if row is None else _approval_from_row(row)

    async def list_by_run(self, run_id: str) -> list[ApprovalRequest]:
        rows = (
            await self._session.scalars(
                select(ApprovalRow)
                .where(ApprovalRow.run_id == run_id)
                .order_by(ApprovalRow.created_at.asc(), ApprovalRow.approval_id.asc())
            )
        ).all()
        return [_approval_from_row(row) for row in rows]

    async def update(self, approval: ApprovalRequest) -> None:
        row = await self._session.get(ApprovalRow, approval.approval_id)
        if row is None:
            raise LookupError(f"Approval {approval.approval_id} no longer exists.")
        if row.decision != ApprovalDecision.PENDING.value:
            raise ValueError("A terminal approval decision cannot be changed.")
        if approval.decision is ApprovalDecision.PENDING:
            raise ValueError("Approval update requires a terminal decision.")
        if (
            row.run_id != approval.run_id
            or row.session_id != approval.session_id
            or row.operation != approval.operation
            or row.risk_level != approval.risk_level.value
            or row.resource_or_command_summary != approval.resource_or_command_summary
            or row.created_at != approval.created_at
        ):
            raise ValueError("Immutable approval fields cannot be changed.")
        row.decision = approval.decision.value
        row.actor = approval.actor
        row.reason = approval.reason
        row.decided_at = approval.decided_at
        await self._session.flush()


class SqlAlchemyRepositoryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, repository: Repository) -> None:
        self._session.add(_repository_to_row(repository))
        await self._session.flush()

    async def get(self, repository_id: str) -> Repository | None:
        row = await self._session.get(RepositoryRow, repository_id)
        return None if row is None else _repository_from_row(row)

    async def get_by_canonical_path(self, canonical_path: str) -> Repository | None:
        row = await self._session.scalar(
            select(RepositoryRow).where(RepositoryRow.canonical_path == canonical_path)
        )
        return None if row is None else _repository_from_row(row)


class SqlAlchemySessionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, session: NexusSession) -> None:
        self._session.add(_session_to_row(session))
        await self._session.flush()

    async def get(self, session_id: str) -> NexusSession | None:
        row = await self._session.get(SessionRow, session_id)
        return None if row is None else _session_from_row(row)

    async def list_by_repository(self, repository_id: str) -> list[NexusSession]:
        rows = (
            await self._session.scalars(
                select(SessionRow)
                .where(SessionRow.repository_id == repository_id)
                .order_by(SessionRow.last_active_at.desc(), SessionRow.session_id.asc())
            )
        ).all()
        return [_session_from_row(row) for row in rows]

    async def update(self, session: NexusSession) -> None:
        row = await self._session.get(SessionRow, session.session_id)
        if row is None:
            raise LookupError(f"Session {session.session_id} no longer exists.")
        row.repository_id = session.repository_id
        row.created_at = session.created_at
        row.last_active_at = session.last_active_at
        row.configuration_reference = session.configuration_reference
        await self._session.flush()


class SqlAlchemyRunRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, run: Run) -> None:
        self._session.add(_run_to_row(run))
        await self._session.flush()

    async def get(self, run_id: str) -> Run | None:
        row = await self._session.get(RunRow, run_id)
        return None if row is None else _run_from_row(row)

    async def get_latest_interrupted(self, session_id: str) -> Run | None:
        row = await self._session.scalar(
            select(RunRow)
            .where(
                RunRow.session_id == session_id,
                RunRow.status == RunStatus.INTERRUPTED.value,
            )
            .order_by(RunRow.started_at.desc(), RunRow.run_id.desc())
            .limit(1)
        )
        return None if row is None else _run_from_row(row)

    async def has_interrupted(self, session_id: str) -> bool:
        result = await self._session.scalar(
            select(RunRow.run_id)
            .where(
                RunRow.session_id == session_id,
                RunRow.status == RunStatus.INTERRUPTED.value,
            )
            .limit(1)
        )
        return result is not None

    async def update(self, run: Run) -> None:
        row = await self._session.get(RunRow, run.run_id)
        if row is None:
            raise LookupError(f"Run {run.run_id} no longer exists.")
        _apply_run(row, run)
        await self._session.flush()


class SqlAlchemySessionTurnRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, turn: SessionTurn) -> None:
        self._session.add(_turn_to_row(turn))
        await self._session.flush()

    async def list_by_session(self, session_id: str) -> list[SessionTurn]:
        rows = (
            await self._session.scalars(
                select(SessionTurnRow)
                .where(SessionTurnRow.session_id == session_id)
                .order_by(SessionTurnRow.sequence.asc())
            )
        ).all()
        return [_turn_from_row(row) for row in rows]

    async def next_sequence(self, session_id: str) -> int:
        current = await self._session.scalar(
            select(func.max(SessionTurnRow.sequence)).where(
                SessionTurnRow.session_id == session_id
            )
        )
        return 1 if current is None else int(current) + 1


def _repository_to_row(entity: Repository) -> RepositoryRow:
    return RepositoryRow(
        id=entity.repository_id,
        canonical_path=entity.canonical_path,
        metadata_payload=entity.metadata,
        configuration_reference=entity.configuration_reference,
        created_at=entity.created_at,
    )


def _repository_from_row(row: RepositoryRow) -> Repository:
    return Repository(
        repository_id=row.id,
        canonical_path=row.canonical_path,
        metadata=dict(row.metadata_payload),
        configuration_reference=row.configuration_reference,
        created_at=row.created_at,
    )


def _session_to_row(entity: NexusSession) -> SessionRow:
    return SessionRow(
        session_id=entity.session_id,
        repository_id=entity.repository_id,
        created_at=entity.created_at,
        last_active_at=entity.last_active_at,
        configuration_reference=entity.configuration_reference,
    )


def _session_from_row(row: SessionRow) -> NexusSession:
    return NexusSession(
        session_id=row.session_id,
        repository_id=row.repository_id,
        created_at=row.created_at,
        last_active_at=row.last_active_at,
        configuration_reference=row.configuration_reference,
    )


def _run_to_row(entity: Run) -> RunRow:
    row = RunRow(run_id=entity.run_id)
    _apply_run(row, entity)
    return row


def _apply_run(row: RunRow, entity: Run) -> None:
    row.session_id = entity.session_id
    row.task = entity.task
    row.status = entity.status.value
    row.model_metadata = entity.model_metadata
    row.started_at = entity.started_at
    row.finished_at = entity.finished_at
    row.token_count = entity.token_count
    row.tool_call_count = entity.tool_call_count
    row.changed_file_refs = entity.changed_file_refs
    row.final_outcome = entity.final_outcome
    row.graph_thread_id = entity.graph_thread_id


def _run_from_row(row: RunRow) -> Run:
    return Run(
        run_id=row.run_id,
        session_id=row.session_id,
        task=row.task,
        status=RunStatus(row.status),
        model_metadata=_optional_dict(row.model_metadata),
        started_at=row.started_at,
        finished_at=row.finished_at,
        token_count=row.token_count,
        tool_call_count=row.tool_call_count,
        changed_file_refs=None if row.changed_file_refs is None else list(row.changed_file_refs),
        final_outcome=_optional_dict(row.final_outcome),
        graph_thread_id=row.graph_thread_id,
    )


def _turn_to_row(entity: SessionTurn) -> SessionTurnRow:
    return SessionTurnRow(
        id=entity.id,
        session_id=entity.session_id,
        run_id=entity.run_id,
        sequence=entity.sequence,
        role=entity.role,
        content=entity.content,
        metadata_payload=entity.metadata,
        created_at=entity.created_at,
    )


def _turn_from_row(row: SessionTurnRow) -> SessionTurn:
    return SessionTurn(
        id=row.id,
        session_id=row.session_id,
        run_id=row.run_id,
        sequence=row.sequence,
        role=row.role,
        content=row.content,
        metadata=_optional_dict(row.metadata_payload),
        created_at=row.created_at,
    )


def _optional_dict(value: dict[str, Any] | None) -> dict[str, object] | None:
    return None if value is None else dict(value)


def _approval_to_row(entity: ApprovalRequest) -> ApprovalRow:
    return ApprovalRow(
        approval_id=entity.approval_id,
        run_id=entity.run_id,
        session_id=entity.session_id,
        operation=entity.operation,
        risk_level=entity.risk_level.value,
        resource_or_command_summary=entity.resource_or_command_summary,
        decision=entity.decision.value,
        actor=entity.actor,
        reason=entity.reason,
        created_at=entity.created_at,
        decided_at=entity.decided_at,
    )


def _approval_from_row(row: ApprovalRow) -> ApprovalRequest:
    return ApprovalRequest(
        approval_id=row.approval_id,
        run_id=row.run_id,
        session_id=row.session_id,
        operation=row.operation,
        risk_level=RiskLevel(row.risk_level),
        resource_or_command_summary=row.resource_or_command_summary,
        decision=ApprovalDecision(row.decision),
        actor=row.actor,
        reason=row.reason,
        created_at=row.created_at,
        decided_at=row.decided_at,
    )
