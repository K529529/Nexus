import pytest
from sqlalchemy.exc import SQLAlchemyError

from nexus.config import load_runtime_config
from nexus.infrastructure.database import DatabaseBootstrap


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_postgresql_connectivity_smoke() -> None:
    config = load_runtime_config()
    database = DatabaseBootstrap(config.database_url)
    try:
        try:
            await database.check_connection()
        except (OSError, SQLAlchemyError) as exc:
            pytest.skip(f"PostgreSQL is unavailable: {type(exc).__name__}")
        except Exception as exc:
            if type(exc).__module__.startswith("asyncpg."):
                pytest.skip(f"PostgreSQL is unavailable: {type(exc).__name__}")
            raise
    finally:
        await database.close()
