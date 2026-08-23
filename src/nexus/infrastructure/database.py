"""Day 1 PostgreSQL engine bootstrap and connectivity check."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


class DatabaseBootstrap:
    """Own the Day 1 async engine without introducing business persistence."""

    def __init__(self, database_url: str) -> None:
        self._engine: AsyncEngine = create_async_engine(
            database_url,
            connect_args={"timeout": 5},
            hide_parameters=True,
            pool_timeout=5,
        )

    async def check_connection(self) -> None:
        """Open a connection and execute the smallest useful smoke query."""

        async with self._engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    async def close(self) -> None:
        """Dispose the engine pool and any checked-in connections."""

        await self._engine.dispose()
