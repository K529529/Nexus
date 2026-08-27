from __future__ import annotations

import asyncio
from collections.abc import Iterator
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from nexus.config import load_runtime_config


@pytest.fixture
def postgres_database_url() -> Iterator[str]:
    base_url = make_url(load_runtime_config().database_url)
    database_name = f"nexus_day2_test_{uuid4().hex}"
    test_url = base_url.set(database=database_name).render_as_string(hide_password=False)

    try:
        asyncio.run(_create_database(base_url.render_as_string(hide_password=False), database_name))
    except Exception as exc:
        pytest.skip(f"PostgreSQL test database is unavailable: {type(exc).__name__}")

    try:
        yield test_url
    finally:
        asyncio.run(_drop_database(base_url.render_as_string(hide_password=False), database_name))


@pytest.fixture
def migrated_database_url(postgres_database_url: str) -> str:
    _run_alembic(postgres_database_url, "upgrade", "head")
    return postgres_database_url


def _run_alembic(database_url: str, operation: str, revision: str) -> None:
    configuration = Config("alembic.ini")
    configuration.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    getattr(command, operation)(configuration, revision)


async def _create_database(base_url: str, database_name: str) -> None:
    engine = create_async_engine(base_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            await connection.execute(text(f'CREATE DATABASE "{database_name}"'))
    finally:
        await engine.dispose()


async def _drop_database(base_url: str, database_name: str) -> None:
    if not database_name.startswith("nexus_day2_test_"):
        raise RuntimeError("Refusing to drop a non-test database.")
    engine = create_async_engine(base_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            await connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :database_name AND pid <> pg_backend_pid()"
                ),
                {"database_name": database_name},
            )
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}"'))
    finally:
        await engine.dispose()
