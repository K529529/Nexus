"""Independent transaction boundary for Day 3 approvals."""

from __future__ import annotations

from types import TracebackType
from typing import Protocol, Self

from nexus.domain.ports.repositories import ApprovalRepository, RunRepository, SessionRepository


class ApprovalUnitOfWork(Protocol):
    approvals: ApprovalRepository
    runs: RunRepository
    sessions: SessionRepository

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None: ...


class ApprovalUnitOfWorkFactory(Protocol):
    def __call__(self) -> ApprovalUnitOfWork: ...
