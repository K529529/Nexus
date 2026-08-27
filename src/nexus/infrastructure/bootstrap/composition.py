"""Assemble and dispose the approved Day 1 concrete dependency subset."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from langgraph.checkpoint.base import BaseCheckpointSaver

from nexus.application.runtime import NexusRuntime
from nexus.application.session_service import SessionService
from nexus.config.models import RuntimeConfig
from nexus.domain.ports.checkpoint_provider import CheckpointProvider
from nexus.domain.ports.model_gateway import ModelGateway
from nexus.infrastructure.asyncio_compat import configure_asyncio_policy
from nexus.infrastructure.checkpoint import PostgresCheckpointProvider
from nexus.infrastructure.database import DatabaseBootstrap
from nexus.infrastructure.graph.langgraph_runtime import LangGraphRuntime
from nexus.infrastructure.model_gateway.openai_compatible import OpenAICompatibleModelGateway
from nexus.infrastructure.persistence import SqlAlchemySessionUnitOfWorkFactory

configure_asyncio_policy()


@dataclass(frozen=True, slots=True)
class BootstrappedApplication:
    """Day 1 application dependencies exposed to entry-point adapters."""

    runtime: NexusRuntime
    session_service: SessionService
    database: DatabaseBootstrap


@asynccontextmanager
async def bootstrap_application(
    config: RuntimeConfig,
    *,
    model_gateway: ModelGateway | None = None,
    workspace_path: Path | None = None,
    interrupt_before_model_response: bool = False,
) -> AsyncIterator[BootstrappedApplication]:
    """Build the Day 2 object graph and reliably release infrastructure resources."""

    database = DatabaseBootstrap(config.database_url)
    checkpoint_provider: CheckpointProvider = PostgresCheckpointProvider(config.database_url)
    try:
        await checkpoint_provider.setup()
        gateway = (
            model_gateway if model_gateway is not None else OpenAICompatibleModelGateway(config)
        )
        unit_of_work_factory = SqlAlchemySessionUnitOfWorkFactory(database.session_factory)
        session_service = SessionService(unit_of_work_factory, workspace_path or Path.cwd())
        graph_runtime = LangGraphRuntime(
            gateway,
            checkpointer=cast(
                BaseCheckpointSaver[Any], checkpoint_provider.get_checkpointer()
            ),
            interrupt_before_model_response=interrupt_before_model_response,
        )
        application = BootstrappedApplication(
            runtime=NexusRuntime(
                graph_runtime,
                session_service=session_service,
                model_metadata={
                    "provider": config.model_provider,
                    "model": config.model_name,
                },
            ),
            session_service=session_service,
            database=database,
        )
        yield application
    finally:
        await checkpoint_provider.close()
        await database.close()
