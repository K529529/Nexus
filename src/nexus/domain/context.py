"""Frozen Day 5 retrieval, indexing and embedding values."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nexus.domain.exploration import ExplorationResult


class RetrievalSource(StrEnum):
    LEXICAL = "LEXICAL"
    SEMANTIC = "SEMANTIC"


@dataclass(frozen=True, slots=True)
class CodeChunk:
    file_path: str
    language: str
    symbol: str | None
    start_line: int
    end_line: int
    content: str
    content_hash: str
    file_hash: str


@dataclass(frozen=True, slots=True)
class ContextCandidate:
    chunk: CodeChunk
    lexical_rank: int | None
    semantic_rank: int | None
    semantic_score: float | None
    rrf_score: float
    sources: tuple[RetrievalSource, ...]


@dataclass(frozen=True, slots=True)
class ContextBudget:
    max_retrieved_chunks: int
    max_code_context_tokens: int
    max_recent_observations: int


@dataclass(frozen=True, slots=True)
class RetrievalQuery:
    repository_id: str
    workspace_path: str
    text: str
    limit: int


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    candidates: tuple[ContextCandidate, ...]
    semantic_used: bool
    semantic_status: str | None


@dataclass(frozen=True, slots=True)
class ContextRequest:
    task: str
    repository_id: str
    workspace_path: str
    exploration: ExplorationResult
    run_id: str
    session_id: str


@dataclass(frozen=True, slots=True)
class EmbeddingConfig:
    provider: str
    model: str
    dimension: int


@dataclass(frozen=True, slots=True)
class IndexRequest:
    repository_id: str
    workspace_path: str
    rebuild: bool


@dataclass(frozen=True, slots=True)
class IndexResult:
    repository_id: str
    scanned_files: int
    indexed_files: int
    unchanged_files: int
    removed_files: int
    skipped_files: int
    chunk_count: int
    rebuilt: bool


class IndexCompatibilityStatus(StrEnum):
    COMPATIBLE = "COMPATIBLE"
    INDEX_NOT_FOUND = "INDEX_NOT_FOUND"
    INDEX_INCOMPATIBLE = "INDEX_INCOMPATIBLE"


@dataclass(frozen=True, slots=True)
class IndexCompatibility:
    status: IndexCompatibilityStatus
    reason: str | None
