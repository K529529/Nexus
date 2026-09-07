"""Transactional exact pgvector retrieval and incremental indexing."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from nexus.context.chunking import language_for, text_hash
from nexus.domain.context import (
    CodeChunk,
    ContextCandidate,
    EmbeddingConfig,
    IndexCompatibility,
    IndexCompatibilityStatus,
    IndexRequest,
    IndexResult,
    RetrievalQuery,
    RetrievalSource,
)
from nexus.domain.ports.context import Chunker, EmbeddingGateway
from nexus.errors import ContextError
from nexus.infrastructure.embedding import validate_vectors
from nexus.infrastructure.repository_files import RepositoryFiles
from nexus.infrastructure.semantic_models import SemanticChunkRow, SemanticIndexRow


def _compatibility(row: SemanticIndexRow | None, config: EmbeddingConfig) -> IndexCompatibility:
    if row is None:
        return IndexCompatibility(
            IndexCompatibilityStatus.INDEX_NOT_FOUND,
            "Run nexus index to initialize semantic search.",
        )
    expected = (config.provider, config.model, config.dimension, "v1", "line_window_v1")
    actual = (
        row.embedding_provider,
        row.embedding_model,
        row.embedding_dimension,
        row.index_version,
        row.chunk_strategy_version,
    )
    if expected != actual:
        return IndexCompatibility(
            IndexCompatibilityStatus.INDEX_INCOMPATIBLE,
            "Embedding/index configuration changed; run nexus index --rebuild.",
        )
    return IndexCompatibility(IndexCompatibilityStatus.COMPATIBLE, None)


async def _lock(session: AsyncSession, repository_id: str, *, shared: bool = False) -> None:
    operation = "pg_advisory_xact_lock_shared" if shared else "pg_advisory_xact_lock"
    await session.execute(
        text(f"SELECT {operation}(hashtextextended(:repository_id, 0))"),
        {"repository_id": repository_id},
    )


class PgVectorSemanticSearchProvider:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        embedding: EmbeddingGateway,
    ) -> None:
        self._sessions = sessions
        self._embedding = embedding

    async def validate_index(self, repository_id: str) -> IndexCompatibility:
        try:
            async with self._sessions() as session:
                return _compatibility(
                    await session.get(SemanticIndexRow, UUID(repository_id)),
                    self._embedding.config,
                )
        except Exception as exc:
            raise ContextError(
                "Semantic index validation is unavailable.",
                code="SEMANTIC_SEARCH_FAILED",
                retryable=True,
            ) from exc

    async def search(self, query: RetrievalQuery) -> tuple[ContextCandidate, ...]:
        try:
            async with self._sessions() as session, session.begin():
                await _lock(session, query.repository_id, shared=True)
                compatible = _compatibility(
                    await session.get(SemanticIndexRow, UUID(query.repository_id)),
                    self._embedding.config,
                )
                if compatible.status is not IndexCompatibilityStatus.COMPATIBLE:
                    raise ContextError(
                        compatible.reason or "Index unavailable.", code=compatible.status.value
                    )
                vector = validate_vectors(
                    (await self._embedding.embed_query(query.text),),
                    1,
                    self._embedding.config.dimension,
                )[0]
                distance = SemanticChunkRow.embedding.cosine_distance(list(vector))
                rows = (
                    await session.execute(
                        select(SemanticChunkRow, distance.label("distance"))
                        .where(SemanticChunkRow.repository_id == UUID(query.repository_id))
                        .order_by(
                            distance,
                            SemanticChunkRow.file_path,
                            SemanticChunkRow.start_line,
                            SemanticChunkRow.content_hash,
                        )
                        .limit(min(query.limit, 20)),
                    )
                ).all()
                return tuple(
                    ContextCandidate(
                        CodeChunk(
                            row.file_path,
                            row.language,
                            row.symbol,
                            row.start_line,
                            row.end_line,
                            row.content,
                            row.content_hash,
                            row.file_hash,
                        ),
                        None,
                        rank,
                        float(score),
                        0.0,
                        (RetrievalSource.SEMANTIC,),
                    )
                    for rank, (row, score) in enumerate(rows, 1)
                )
        except ContextError:
            raise
        except Exception as exc:
            raise ContextError(
                "Semantic query failed.", code="SEMANTIC_SEARCH_FAILED", retryable=True
            ) from exc


class PgVectorRepositoryIndexer:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        embedding: EmbeddingGateway,
        chunker: Chunker,
        files: RepositoryFiles,
    ) -> None:
        self._sessions = sessions
        self._embedding = embedding
        self._chunker = chunker
        self._files = files

    async def index(self, request: IndexRequest) -> IndexResult:
        try:
            workspace = await asyncio.to_thread(Path(request.workspace_path).resolve)
            if self._files.guard.root != workspace:
                raise ValueError("Index workspace mismatch.")
            async with self._sessions() as session, session.begin():
                await _lock(session, request.repository_id)
                repository = UUID(request.repository_id)
                meta = await session.get(SemanticIndexRow, repository)
                compatible = _compatibility(meta, self._embedding.config)
                if (
                    compatible.status is IndexCompatibilityStatus.INDEX_INCOMPATIBLE
                    and not request.rebuild
                ):
                    raise ContextError(
                        compatible.reason or "Rebuild required.", code="INDEX_INCOMPATIBLE"
                    )
                scan = await asyncio.to_thread(self._files.scan)
                old = {
                    path: file_hash
                    for path, file_hash in (
                        await session.execute(
                            select(SemanticChunkRow.file_path, SemanticChunkRow.file_hash)
                            .where(SemanticChunkRow.repository_id == repository)
                            .distinct(),
                        )
                    ).all()
                }
                current = dict(scan.files)
                removed = set(old) - current.keys()
                changed = {
                    path: content
                    for path, content in current.items()
                    if request.rebuild or old.get(path) != text_hash(content)
                }
                unchanged = len(current) - len(changed)
                if request.rebuild:
                    await session.execute(
                        delete(SemanticChunkRow).where(
                            SemanticChunkRow.repository_id == repository,
                        )
                    )
                else:
                    await session.execute(
                        delete(SemanticChunkRow).where(
                            SemanticChunkRow.repository_id == repository,
                            SemanticChunkRow.file_path.in_(set(changed) | removed),
                        )
                    )
                now = datetime.now(UTC)
                for path, content in changed.items():
                    chunks = self._chunker.chunk(
                        file_path=path,
                        language=language_for(path),
                        content=content,
                        file_hash=text_hash(content),
                    )
                    for offset in range(0, len(chunks), 32):
                        batch = chunks[offset : offset + 32]
                        vectors = validate_vectors(
                            await self._embedding.embed_documents([c.content for c in batch]),
                            len(batch),
                            self._embedding.config.dimension,
                        )
                        for chunk, vector in zip(batch, vectors, strict=True):
                            session.add(
                                SemanticChunkRow(
                                    id=uuid4(),
                                    repository_id=repository,
                                    file_path=chunk.file_path,
                                    language=chunk.language,
                                    symbol=chunk.symbol,
                                    start_line=chunk.start_line,
                                    end_line=chunk.end_line,
                                    content=chunk.content,
                                    content_hash=chunk.content_hash,
                                    file_hash=chunk.file_hash,
                                    embedding=list(vector),
                                    created_at=now,
                                    updated_at=now,
                                )
                            )
                config = self._embedding.config
                if meta is None:
                    meta = SemanticIndexRow(repository_id=repository)
                    session.add(meta)
                meta.embedding_provider = config.provider
                meta.embedding_model = config.model
                meta.embedding_dimension = config.dimension
                meta.index_version = "v1"
                meta.chunk_strategy_version = "line_window_v1"
                meta.indexed_at = now
                await session.flush()
                count = await session.scalar(
                    select(func.count())
                    .select_from(SemanticChunkRow)
                    .where(SemanticChunkRow.repository_id == repository)
                )
                return IndexResult(
                    request.repository_id,
                    scan.scanned,
                    len(changed),
                    unchanged,
                    len(removed),
                    scan.skipped,
                    int(count or 0),
                    request.rebuild,
                )
        except ContextError as exc:
            if exc.code == "INDEX_INCOMPATIBLE":
                raise
            raise ContextError(
                "Index build failed; previous index transaction is preserved.",
                code="INDEX_BUILD_FAILED",
                retryable=True,
            ) from exc
        except Exception as exc:
            raise ContextError(
                "Index build failed; previous index transaction is preserved.",
                code="INDEX_BUILD_FAILED",
                retryable=True,
            ) from exc
