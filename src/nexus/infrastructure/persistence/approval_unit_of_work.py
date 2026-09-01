"""Independent async SQLAlchemy transaction boundary for Day 3 approvals."""

from __future__ import annotations

from types import TracebackType

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from nexus.domain.ports.approval_unit_of_work import ApprovalUnitOfWork
from nexus.domain.ports.repositories import ApprovalRepository, RunRepository, SessionRepository
from nexus.infrastructure.persistence.repositories import (
    SqlAlchemyApprovalRepository,
    SqlAlchemyRunRepository,
    SqlAlchemySessionRepository,
)


class SqlAlchemyApprovalUnitOfWork:
    approvals: ApprovalRepository
    runs: RunRepository
    sessions: SessionRepository

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._session: AsyncSession | None = None

    async def __aenter__(self) -> SqlAlchemyApprovalUnitOfWork:
        if self._session is not None:
            raise RuntimeError("ApprovalUnitOfWork instances cannot be reused.")
        session = self._session_factory()
        try:
            await session.begin()
        except Exception:
            await session.close()
            raise
        self._session = session
        self.approvals = SqlAlchemyApprovalRepository(session)
        self.runs = SqlAlchemyRunRepository(session)
        self.sessions = SqlAlchemySessionRepository(session)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        session = self._require_session()
        try:
            if exc_type is None:
                try:
                    await session.commit()
                except Exception:
                    await session.rollback()
                    raise
            else:
                await session.rollback()
        finally:
            await session.close()
            self._session = None
        return None

    def _require_session(self) -> AsyncSession:
        if self._session is None:
            raise RuntimeError("ApprovalUnitOfWork has not been entered.")
        return self._session


class SqlAlchemyApprovalUnitOfWorkFactory:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    def __call__(self) -> ApprovalUnitOfWork:
        return SqlAlchemyApprovalUnitOfWork(self._session_factory)
