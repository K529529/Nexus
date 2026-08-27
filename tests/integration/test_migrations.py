from __future__ import annotations

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine


@pytest.mark.postgres
def test_day2_migration_upgrade_downgrade_upgrade(postgres_database_url: str) -> None:
    configuration = Config("alembic.ini")
    configuration.set_main_option(
        "sqlalchemy.url", postgres_database_url.replace("%", "%%")
    )

    command.upgrade(configuration, "head")
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
