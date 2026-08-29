"""Minimal transaction boundary approved for Day 2 session operations."""

from __future__ import annotations

from types import TracebackType
from typing import Protocol, Self

from nexus.domain.ports.repositories import (
    RepositoryRepository,
    RunRepository,
    SessionRepository,
    SessionTurnRepository,
)


class SessionUnitOfWork(Protocol):
    repositories: RepositoryRepository
    sessions: SessionRepository
    runs: RunRepository
    turns: SessionTurnRepository

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None: ...


class SessionUnitOfWorkFactory(Protocol):
    def __call__(self) -> SessionUnitOfWork: ...
