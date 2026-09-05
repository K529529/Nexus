from __future__ import annotations

from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from pytest import MonkeyPatch
from sqlalchemy import inspect
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine


@pytest.mark.postgres
def test_day5_migration_upgrade_downgrade_upgrade(
    postgres_database_url: str,
    monkeypatch: MonkeyPatch,
) -> None:
    configuration = Config("alembic.ini")
    configuration.set_main_option(
        "sqlalchemy.url", postgres_database_url.replace("%", "%%")
    )
    unused_database_url = make_url(postgres_database_url).set(
        database=f"nexus_env_must_not_win_{uuid4().hex}"
    )
    monkeypatch.setenv(
        "NEXUS_DATABASE_URL",
        unused_database_url.render_as_string(hide_password=False),
    )

    command.upgrade(configuration, "head")
    assert _business_tables(postgres_database_url) == {
        "repository_semantic_indexes",
        "semantic_code_chunks",
        "approvals",
        "alembic_version",
        "repositories",
        "runs",
        "session_turns",
        "sessions",
    }

    command.downgrade(configuration, "day03_0002")
    assert "semantic_code_chunks" not in _business_tables(postgres_database_url)
    assert "repository_semantic_indexes" not in _business_tables(postgres_database_url)
    assert "approvals" in _business_tables(postgres_database_url)
    command.downgrade(configuration, "day02_0001")
    assert _business_tables(postgres_database_url) == {
        "alembic_version",
        "repositories",
        "runs",
        "session_turns",
        "sessions",
    }

    command.downgrade(configuration, "base")
    assert _business_tables(postgres_database_url) == {"alembic_version"}

    command.upgrade(configuration, "head")
    assert _business_tables(postgres_database_url) == {
        "repository_semantic_indexes",
        "semantic_code_chunks",
        "approvals",
        "alembic_version",
        "repositories",
        "runs",
        "session_turns",
        "sessions",
    }


def _business_tables(database_url: str) -> set[str]:
    import asyncio

    async def inspect_tables() -> set[str]:
        engine = create_async_engine(database_url)
        try:
            async with engine.connect() as connection:
                return set(await connection.run_sync(lambda conn: inspect(conn).get_table_names()))
        finally:
            await engine.dispose()

    return asyncio.run(inspect_tables())
