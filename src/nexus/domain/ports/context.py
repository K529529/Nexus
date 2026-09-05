"""Nexus-owned Day 5 context boundaries."""

from collections.abc import Sequence
from typing import Protocol

from nexus.domain.agent_decision import Observation
from nexus.domain.context import (
    CodeChunk,
    ContextCandidate,
    ContextRequest,
    EmbeddingConfig,
    IndexCompatibility,
    IndexRequest,
    IndexResult,
    RetrievalQuery,
    RetrievalResult,
)
from nexus.domain.exploration import WorkingContext
from nexus.domain.persistence import SessionTurn
from nexus.domain.planning import Plan


class LexicalSearchProvider(Protocol):
    async def search(self, query: RetrievalQuery) -> tuple[ContextCandidate, ...]: ...


class SemanticSearchProvider(Protocol):
    async def search(self, query: RetrievalQuery) -> tuple[ContextCandidate, ...]: ...

    async def validate_index(self, repository_id: str) -> IndexCompatibility: ...


class ContextProvider(Protocol):
    async def retrieve(self, request: ContextRequest) -> RetrievalResult: ...


class ContextManager(Protocol):
    async def build(self, request: ContextRequest) -> WorkingContext: ...

    async def prepare_agent_context(
        self,
        *,
        working_context: WorkingContext,
        plan: Plan | None,
        observations: Sequence[Observation],
        conversation_turns: Sequence[SessionTurn],
    ) -> WorkingContext: ...


class Chunker(Protocol):
    def chunk(
        self,
        *,
        file_path: str,
        language: str,
        content: str,
        file_hash: str,
    ) -> tuple[CodeChunk, ...]: ...


class EmbeddingGateway(Protocol):
    @property
    def config(self) -> EmbeddingConfig: ...

    async def embed_documents(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]: ...

    async def embed_query(self, text: str) -> tuple[float, ...]: ...


class RepositoryIndexer(Protocol):
    async def index(self, request: IndexRequest) -> IndexResult: ...
