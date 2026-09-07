"""Real provider semantic acceptance; explicitly separate from known-vector fixtures."""

from pathlib import Path

import pytest

from nexus.config import load_runtime_config
from nexus.domain.context import RetrievalQuery
from nexus.infrastructure.bootstrap.composition import index_repository
from nexus.infrastructure.database import DatabaseBootstrap
from nexus.infrastructure.embedding import OpenAICompatibleEmbeddingGateway
from nexus.infrastructure.semantic import PgVectorSemanticSearchProvider


@pytest.mark.postgres
async def test_live_embedding_paraphrased_concept(
    migrated_database_url: str,
    tmp_path: Path,
) -> None:
    config = load_runtime_config()
    if (
        not config.embedding_model
        or not config.embedding_dimension
        or not config.embedding_base_url
        or config.embedding_api_key is None
    ):
        pytest.skip("Real embedding acceptance needs NEXUS_EMBEDDING_* deployment configuration.")
    config = config.model_copy(update={"database_url": migrated_database_url})
    (tmp_path / "credential.py").write_text(
        "def authenticate_account(username, password, stored_password):\n"
        "    return password == stored_password\n",
        encoding="utf-8",
    )
    (tmp_path / "geometry.py").write_text(
        "def rectangle_area(width, height):\n    return width * height\n",
        encoding="utf-8",
    )
    (tmp_path / "ordering.py").write_text(
        "def sort_numbers(values):\n    return sorted(values)\n",
        encoding="utf-8",
    )
    gateway = OpenAICompatibleEmbeddingGateway(config)
    result = await index_repository(config, workspace_path=tmp_path, embedding_gateway=gateway)
    database = DatabaseBootstrap(migrated_database_url)
    try:
        provider = PgVectorSemanticSearchProvider(database.session_factory, gateway)
        candidates = await provider.search(
            RetrievalQuery(
                result.repository_id,
                str(tmp_path),
                "verify a person's login credentials",
                20,
            )
        )
        assert candidates[0].chunk.file_path == "credential.py"
        assert candidates[0].semantic_rank == 1
    finally:
        await database.close()
