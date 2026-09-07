from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from nexus.config.models import RuntimeConfig
from nexus.context.chunking import LineWindowChunker, estimated_tokens, text_hash
from nexus.context.manager import BoundedContextManager
from nexus.context.retrieval import (
    HybridContextProvider,
    ToolLexicalSearchProvider,
    ToolRepositoryAccess,
)
from nexus.domain.context import (
    ContextBudget,
    ContextCandidate,
    ContextRequest,
    IndexCompatibilityStatus,
    RetrievalQuery,
    RetrievalResult,
    RetrievalSource,
)
from nexus.domain.exploration import ExplorationResult, RepositoryFileEvidence
from nexus.domain.tooling import ApprovalDecision, PolicyDecision, RiskLevel, ToolResult
from nexus.errors import ConfigurationError, ContextError
from nexus.infrastructure.bootstrap.composition import bootstrap_tool_application, index_repository
from nexus.infrastructure.database import DatabaseBootstrap
from nexus.infrastructure.semantic import PgVectorSemanticSearchProvider
from nexus.infrastructure.semantic_models import SemanticChunkRow, SemanticIndexRow
from nexus.interfaces.cli.app import app
from tests.fixtures.embedding import FixtureEmbedding


class _StaticSemanticResult:
    def __init__(self, candidates: tuple[ContextCandidate, ...]) -> None:
        self._candidates = candidates

    async def retrieve(self, request: ContextRequest) -> RetrievalResult:
        del request
        return RetrievalResult(self._candidates, True, None)


def exploration(paths: tuple[str, ...] = ()) -> ExplorationResult:
    tool = ToolResult(
        str(uuid4()),
        "git_status",
        True,
        {},
        None,
        RiskLevel.SAFE,
        PolicyDecision.ALLOWED,
        ApprovalDecision.APPROVED,
        0,
    )
    return ExplorationResult(
        (),
        (),
        (),
        tuple(
            RepositoryFileEvidence(path, "task_relevant", "Explicit task path", path)
            for path in paths
        ),
        tool,
        (tool,),
        False,
    )


@pytest.mark.asyncio
async def test_index_requires_embedding_configuration_when_chat_semantic_is_disabled(
    tmp_path: Path,
) -> None:
    with pytest.raises(ConfigurationError):
        await index_repository(
            RuntimeConfig(semantic_enabled=False),
            workspace_path=tmp_path,
        )


@pytest.mark.postgres
async def test_transactional_index_query_incremental_and_repository_isolation(
    migrated_database_url: str,
    tmp_path: Path,
) -> None:
    config = RuntimeConfig(database_url=migrated_database_url)
    embedding = FixtureEmbedding()
    (tmp_path / "auth.py").write_text("def authenticate():\n    return True\n", encoding="utf-8")
    (tmp_path / "alpha.py").write_text("VALUE = 1\n", encoding="utf-8")
    first = await index_repository(config, workspace_path=tmp_path, embedding_gateway=embedding)
    assert first.indexed_files == first.chunk_count == embedding.document_calls == 2
    second = await index_repository(config, workspace_path=tmp_path, embedding_gateway=embedding)
    assert second.unchanged_files == 2 and second.indexed_files == 0
    assert embedding.document_calls == 2
    db = DatabaseBootstrap(migrated_database_url)
    provider = PgVectorSemanticSearchProvider(db.session_factory, embedding)
    try:
        query = RetrievalQuery(first.repository_id, str(tmp_path), "check credentials", 20)
        result = await provider.search(query)
        assert result[0].chunk.file_path == "auth.py"
        assert result[0].semantic_rank == 1
        assert (await provider.validate_index(first.repository_id)).status == (
            IndexCompatibilityStatus.COMPATIBLE
        )
        missing = await provider.validate_index(str(uuid4()))
        assert missing.status is IndexCompatibilityStatus.INDEX_NOT_FOUND
        other = tmp_path / "other"
        other.mkdir()
        (other / "auth.py").write_text("authenticate alien_repository", encoding="utf-8")
        other_index = await index_repository(
            config, workspace_path=other, embedding_gateway=embedding
        )
        assert other_index.repository_id != first.repository_id
        assert all("alien_repository" not in c.chunk.content for c in await provider.search(query))
        (tmp_path / "alpha.py").write_text("VALUE = 2\n", encoding="utf-8")
        (tmp_path / "auth.py").unlink()
        # Exclude the nested fixture before re-indexing the parent.
        (tmp_path / ".gitignore").write_text("other/\n", encoding="utf-8")
        changed = await index_repository(
            config, workspace_path=tmp_path, embedding_gateway=embedding
        )
        assert changed.removed_files == 1
        async with db.session_factory() as session:
            rows = list(
                await session.scalars(
                    select(SemanticChunkRow).where(
                        SemanticChunkRow.repository_id == UUID(first.repository_id),
                    )
                )
            )
            assert all("VALUE = 1" not in row.content for row in rows)
            assert not any(row.file_path == "auth.py" for row in rows)
        embedding.fail = True
        (tmp_path / "alpha.py").write_text("VALUE = 3\n", encoding="utf-8")
        with pytest.raises(ContextError) as failed:
            await index_repository(config, workspace_path=tmp_path, embedding_gateway=embedding)
        assert failed.value.code == "INDEX_BUILD_FAILED"
        embedding.fail = False
        persisted = await provider.search(replace(query, text="alpha"))
        assert any("VALUE = 2" in c.chunk.content for c in persisted)
        assert all("VALUE = 3" not in c.chunk.content for c in persisted)
        mismatch = FixtureEmbedding(model="changed")
        incompatible = PgVectorSemanticSearchProvider(db.session_factory, mismatch)
        with pytest.raises(ContextError) as caught:
            await incompatible.search(query)
        assert caught.value.code == "INDEX_INCOMPATIBLE"
        assert "nexus index --rebuild" in str(caught.value)
        assert mismatch.query_calls == 0
        with pytest.raises(ContextError):
            await index_repository(config, workspace_path=tmp_path, embedding_gateway=mismatch)
        rebuilt = await index_repository(
            config, workspace_path=tmp_path, embedding_gateway=mismatch, rebuild=True
        )
        assert rebuilt.rebuilt
        assert (await incompatible.validate_index(first.repository_id)).status == (
            IndexCompatibilityStatus.COMPATIBLE
        )
    finally:
        await db.close()


@pytest.mark.postgres
@pytest.mark.parametrize(
    "semantic_mode", ["enabled", "disabled", "missing", "incompatible", "failed"]
)
async def test_context_retrieval_budget_instructions_and_fallback(
    migrated_database_url: str,
    tmp_path: Path,
    semantic_mode: str,
) -> None:
    (tmp_path / "nested").mkdir()
    content = "".join(f"exact_symbol_{i} = {i}\n" for i in range(120))
    (tmp_path / "nested" / "auth.py").write_text(content, encoding="utf-8")
    (tmp_path / "nested" / "AGENTS.md").write_text(
        "Keep functions deterministic.", encoding="utf-8"
    )
    embedding = FixtureEmbedding()
    config = RuntimeConfig(database_url=migrated_database_url, semantic_enabled=False)
    indexed = await index_repository(config, workspace_path=tmp_path, embedding_gateway=embedding)
    assert indexed.chunk_count == 1
    db = DatabaseBootstrap(migrated_database_url)
    try:
        async with bootstrap_tool_application(
            config,
            workspace_path=tmp_path,
            day5_file_filtering=True,
        ) as application:
            access = ToolRepositoryAccess(application.tool_runtime)
            chunker = LineWindowChunker()
            lexical = ToolLexicalSearchProvider(access, chunker)
            if semantic_mode == "incompatible":
                embedding = FixtureEmbedding(model="changed")
            embedding.fail = semantic_mode == "failed"
            semantic = (
                None
                if semantic_mode == "disabled"
                else PgVectorSemanticSearchProvider(db.session_factory, embedding)
            )
            manager = BoundedContextManager(
                HybridContextProvider(lexical, semantic),
                access,
                chunker,
                ContextBudget(
                    max_retrieved_chunks=12,
                    max_exploration_seed_chunks=6,
                    max_code_context_tokens=12000,
                    max_recent_observations=8,
                ),
                24000,
            )
            repository_id = str(uuid4()) if semantic_mode == "missing" else indexed.repository_id
            request = ContextRequest(
                "exact_symbol_110",
                repository_id,
                str(tmp_path),
                exploration(),
                str(uuid4()),
                str(uuid4()),
            )
            context = await manager.build(request)
            assert context.selected_files
            target = next(
                c for c in context.retrieved_candidates if c.chunk.file_path == "nested/auth.py"
            )
            assert (target.chunk.start_line, target.chunk.end_line) == (1, 120)
            assert any(i.path == "nested/AGENTS.md" for i in context.repository_instructions)
            assert not any(
                candidate.chunk.file_path.endswith("AGENTS.md")
                for candidate in context.retrieved_candidates
            )
            assert len(context.selected_files) <= 12
            assert sum(estimated_tokens(c.content) for c in context.selected_files) <= 12000
            expected = {
                "enabled": None,
                "disabled": None,
                "missing": "INDEX_NOT_FOUND",
                "incompatible": "INDEX_INCOMPATIBLE",
                "failed": "SEMANTIC_SEARCH_FAILED",
            }
            assert context.semantic_retrieval_status == expected[semantic_mode]
            assert context.semantic_retrieval_used == (semantic_mode == "enabled")
            with access.scope(request.run_id, request.session_id):
                results = await lexical.search(
                    RetrievalQuery(repository_id, str(tmp_path), "exact_symbol", 20)
                )
                assert len(results) == 1
                assert not await lexical.search(
                    RetrievalQuery(repository_id, str(tmp_path), "deterministic", 20)
                )
            # Stale/deleted source can never be accepted into model context.
            (tmp_path / "nested" / "auth.py").unlink()
            stale = await manager.build(request)
            assert not any(c.path == "nested/auth.py" for c in stale.selected_files)
    finally:
        await db.close()


@pytest.mark.postgres
@pytest.mark.parametrize(
    "field,value",
    [
        ("embedding_provider", "other"),
        ("embedding_model", "other"),
        ("embedding_dimension", 99),
        ("index_version", "v2"),
        ("chunk_strategy_version", "future"),
    ],
)
async def test_compatibility_all_metadata_fields(
    migrated_database_url: str,
    tmp_path: Path,
    field: str,
    value: str | int,
) -> None:
    (tmp_path / "x.py").write_text("VALUE = 1\n", encoding="utf-8")
    embedding = FixtureEmbedding()
    indexed = await index_repository(
        RuntimeConfig(database_url=migrated_database_url),
        workspace_path=tmp_path,
        embedding_gateway=embedding,
    )
    db = DatabaseBootstrap(migrated_database_url)
    try:
        async with db.session_factory() as session, session.begin():
            meta = await session.get(SemanticIndexRow, UUID(indexed.repository_id))
            assert meta is not None
            setattr(meta, field, value)
        provider = PgVectorSemanticSearchProvider(db.session_factory, embedding)
        assert (await provider.validate_index(indexed.repository_id)).status == (
            IndexCompatibilityStatus.INDEX_INCOMPATIBLE
        )
    finally:
        await db.close()


@pytest.mark.postgres
def test_index_cli_e2e_three_runs(
    migrated_database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import nexus.interfaces.cli.app as cli_module

    config = RuntimeConfig(database_url=migrated_database_url, embedding_model="fixture")
    embedding = FixtureEmbedding()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli_module, "load_runtime_config", lambda: config)

    async def index_fixture(config: RuntimeConfig, *, rebuild: bool = False) -> object:
        return await index_repository(config, rebuild=rebuild, embedding_gateway=embedding)

    monkeypatch.setattr(cli_module, "index_repository", index_fixture)
    (tmp_path / "alpha.py").write_text("VALUE = 1\n", encoding="utf-8")
    runner = CliRunner()
    first = runner.invoke(app, ["index"])
    assert first.exit_code == 0, first.output
    assert "indexed/changed files: 1" in first.output
    calls = embedding.document_calls
    second = runner.invoke(app, ["index"])
    assert second.exit_code == 0 and "unchanged files: 1" in second.output
    assert embedding.document_calls == calls
    (tmp_path / "alpha.py").write_text("VALUE = 2\n", encoding="utf-8")
    third = runner.invoke(app, ["index"])
    assert third.exit_code == 0 and "indexed/changed files: 1" in third.output
    assert embedding.document_calls == calls + 1

    async def verify() -> None:
        from nexus.application.session_service import SessionService
        from nexus.infrastructure.persistence import SqlAlchemySessionUnitOfWorkFactory

        db = DatabaseBootstrap(migrated_database_url)
        try:
            service = SessionService(
                SqlAlchemySessionUnitOfWorkFactory(db.session_factory), tmp_path
            )
            repository_id = await service.context_repository_id()
            results = await PgVectorSemanticSearchProvider(db.session_factory, embedding).search(
                RetrievalQuery(repository_id, str(tmp_path), "alpha", 20),
            )
            assert results[0].chunk.content == "VALUE = 2\n"
        finally:
            await db.close()

    asyncio.run(verify())
    rebuilt = runner.invoke(app, ["index", "--rebuild"])
    assert rebuilt.exit_code == 0 and "indexed/changed files: 1" in rebuilt.output
    (tmp_path / "alpha.py").write_text("VALUE = 3\n", encoding="utf-8")
    embedding.fail = True
    failed = runner.invoke(app, ["index"])
    assert failed.exit_code == 1
    assert "INDEX_BUILD_FAILED" in failed.output


@pytest.mark.postgres
async def test_lexical_top20_and_shared_budget_overflow_skip(
    migrated_database_url: str,
    tmp_path: Path,
) -> None:
    config = RuntimeConfig(database_url=migrated_database_url, semantic_enabled=False)
    (tmp_path / "a_big.py").write_text("needle = '" + "x" * 1000 + "'\n", encoding="utf-8")
    for index in range(25):
        (tmp_path / f"small_{index:02}.py").write_text("needle = 1\n", encoding="utf-8")
    # Ignored hits must not exhaust the native lexical result limit before real code.
    (tmp_path / "aaa_ignored").mkdir()
    (tmp_path / "aaa_ignored" / "noise.py").write_text("needle\n" * 500, encoding="utf-8")
    (tmp_path / ".gitignore").write_text("aaa_ignored/\n", encoding="utf-8")
    async with bootstrap_tool_application(
        config,
        workspace_path=tmp_path,
        day5_file_filtering=True,
    ) as application:
        access = ToolRepositoryAccess(application.tool_runtime)
        lexical = ToolLexicalSearchProvider(access, LineWindowChunker())
        request = ContextRequest(
            "needle", str(uuid4()), str(tmp_path), exploration(), str(uuid4()), str(uuid4())
        )
        with access.scope(request.run_id, request.session_id):
            candidates = await lexical.search(
                RetrievalQuery(
                    request.repository_id,
                    str(tmp_path),
                    "needle",
                    20,
                )
            )
        assert len(candidates) == 20
        assert [c.lexical_rank for c in candidates] == list(range(1, 21))
        provider = HybridContextProvider(lexical, None)
        with access.scope(request.run_id, request.session_id):
            assert len((await provider.retrieve(request)).candidates) == 12
        manager = BoundedContextManager(
            provider,
            access,
            LineWindowChunker(),
            ContextBudget(
                max_retrieved_chunks=2,
                max_exploration_seed_chunks=6,
                max_code_context_tokens=50,
                max_recent_observations=8,
            ),
            24000,
        )
        result = await manager.build(request)
        assert len(result.selected_files) == 2
        assert all(c.path != "a_big.py" for c in result.selected_files)
        assert result.truncated
        assert sum(estimated_tokens(c.content) for c in result.selected_files) <= 50
        assert all(c.content == "needle = 1\n" for c in result.selected_files)


@pytest.mark.postgres
async def test_seed_cap_admits_hybrid_candidate_and_deduplicates_shared_chunks(
    migrated_database_url: str,
    tmp_path: Path,
) -> None:
    seed_paths = tuple(f"seed_{index}.py" for index in range(3))
    seed_contents: dict[str, str] = {}
    for index, path in enumerate(seed_paths):
        content = "".join(f"seed_{index}_{line} = {line}\n" for line in range(320))
        seed_contents[path] = content
        (tmp_path / path).write_text(content, encoding="utf-8")
    chunker = LineWindowChunker()
    raw_seed_chunks = tuple(
        chunk
        for path in seed_paths
        for chunk in chunker.chunk(
            file_path=path,
            language="python",
            content=seed_contents[path],
            file_hash=text_hash(seed_contents[path]),
        )
        if chunk.start_line <= 400
    )
    assert len(raw_seed_chunks) > 6
    semantic_content = "def uniquely_relevant_paraphrase():\n    return 'only semantic match'\n"
    semantic_path = "semantic_unique.py"
    (tmp_path / semantic_path).write_text(semantic_content, encoding="utf-8")
    semantic_chunk = chunker.chunk(
        file_path=semantic_path,
        language="python",
        content=semantic_content,
        file_hash=text_hash(semantic_content),
    )[0]
    duplicate_candidate = ContextCandidate(
        raw_seed_chunks[0],
        None,
        1,
        1.0,
        1 / 61,
        (RetrievalSource.SEMANTIC,),
    )
    semantic_candidate = ContextCandidate(
        semantic_chunk,
        None,
        2,
        0.999,
        1 / 62,
        (RetrievalSource.SEMANTIC,),
    )
    config = RuntimeConfig(database_url=migrated_database_url)
    async with bootstrap_tool_application(
        config,
        workspace_path=tmp_path,
        day5_file_filtering=True,
    ) as application:
        access = ToolRepositoryAccess(application.tool_runtime)
        manager = BoundedContextManager(
            _StaticSemanticResult((duplicate_candidate, semantic_candidate)),
            access,
            chunker,
            ContextBudget(
                max_retrieved_chunks=12,
                max_exploration_seed_chunks=6,
                max_code_context_tokens=12000,
                max_recent_observations=8,
            ),
            24000,
        )
        result = await manager.build(
            ContextRequest(
                "implement the paraphrased concept",
                str(uuid4()),
                str(tmp_path),
                exploration(seed_paths),
                str(uuid4()),
                str(uuid4()),
            )
        )

    assert len(result.selected_files) == 7 <= 12
    assert result.retrieved_candidates == (duplicate_candidate, semantic_candidate)
    seeds = [
        item for item in result.selected_files if "exploration seed" in item.discovery_reason
    ]
    assert len(seeds) == 6
    assert semantic_path in {item.path for item in result.selected_files}
    assert sum(item.path == seed_paths[0] for item in result.selected_files) == 4
    assert sum(estimated_tokens(item.content) for item in result.selected_files) <= 12000
    assert result.truncated


@pytest.mark.postgres
async def test_now_ignored_file_cleanup_and_safe_empty_index(
    migrated_database_url: str,
    tmp_path: Path,
) -> None:
    config = RuntimeConfig(database_url=migrated_database_url)
    gateway = FixtureEmbedding()
    (tmp_path / "alpha.py").write_text("VALUE = 1\n", encoding="utf-8")
    first = await index_repository(config, workspace_path=tmp_path, embedding_gateway=gateway)
    (tmp_path / ".gitignore").write_text("*\n", encoding="utf-8")
    result = await index_repository(config, workspace_path=tmp_path, embedding_gateway=gateway)
    assert result.removed_files == 1 and result.chunk_count == 0
    db = DatabaseBootstrap(migrated_database_url)
    try:
        provider = PgVectorSemanticSearchProvider(db.session_factory, gateway)
        assert (await provider.validate_index(first.repository_id)).status == (
            IndexCompatibilityStatus.COMPATIBLE
        )
        assert (
            await provider.search(RetrievalQuery(first.repository_id, str(tmp_path), "alpha", 20))
            == ()
        )
    finally:
        await db.close()
