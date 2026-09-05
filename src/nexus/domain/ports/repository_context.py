"""Day 4 selective repository exploration and context-build ports."""

from typing import Protocol

from nexus.domain.exploration import (
    ContextBuildRequest,
    ExplorationRequest,
    ExplorationResult,
    WorkingContext,
)


class RepositoryExplorer(Protocol):
    async def explore(self, request: ExplorationRequest) -> ExplorationResult: ...


class ContextBuilder(Protocol):
    async def build(self, request: ContextBuildRequest) -> WorkingContext: ...
