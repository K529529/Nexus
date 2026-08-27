"""Lifecycle adapter for the official asynchronous PostgreSQL checkpointer."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy.engine import make_url

from nexus.errors import NexusError


class PostgresCheckpointProvider:
    """Own AsyncPostgresSaver creation, setup, and release."""

    def __init__(self, database_url: str) -> None:
        self._connection_string = _to_psycopg_connection_string(database_url)
        self._context: AbstractAsyncContextManager[AsyncPostgresSaver] | None = None
        self._checkpointer: AsyncPostgresSaver | None = None

    async def setup(self) -> None:
        if self._checkpointer is not None:
            return
        context = AsyncPostgresSaver.from_conn_string(self._connection_string)
        self._context = context
        try:
            checkpointer = await context.__aenter__()
            await checkpointer.setup()
        except Exception as exc:
            self._context = None
            try:
                await context.__aexit__(type(exc), exc, exc.__traceback__)
            except Exception:
                pass
            raise NexusError(
                "Nexus could not initialize durable graph checkpoints.",
                code="CHECKPOINT_SETUP_ERROR",
                retryable=True,
            ) from exc
        self._checkpointer = checkpointer

    def get_checkpointer(self) -> object:
        if self._checkpointer is None:
            raise RuntimeError("CheckpointProvider.setup() must run before use.")
        return self._checkpointer

    async def close(self) -> None:
        context = self._context
        self._context = None
        self._checkpointer = None
        if context is not None:
            await context.__aexit__(None, None, None)


def _to_psycopg_connection_string(database_url: str) -> str:
    url = make_url(database_url)
    if url.get_backend_name() != "postgresql":
        raise NexusError(
            "The checkpoint database must use PostgreSQL.",
            code="CHECKPOINT_CONFIGURATION_ERROR",
        )
    return url.set(drivername="postgresql").render_as_string(hide_password=False)
