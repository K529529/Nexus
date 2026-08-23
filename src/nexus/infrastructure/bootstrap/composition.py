"""Assemble and dispose the approved Day 1 concrete dependency subset."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from nexus.application.runtime import NexusRuntime
from nexus.config.models import RuntimeConfig
from nexus.domain.ports.model_gateway import ModelGateway
from nexus.infrastructure.database import DatabaseBootstrap
from nexus.infrastructure.graph.langgraph_runtime import LangGraphRuntime
from nexus.infrastructure.model_gateway.openai_compatible import OpenAICompatibleModelGateway


@dataclass(frozen=True, slots=True)
class BootstrappedApplication:
    """Day 1 application dependencies exposed to entry-point adapters."""

    runtime: NexusRuntime
    database: DatabaseBootstrap


@asynccontextmanager
async def bootstrap_application(
    config: RuntimeConfig,
    *,
    model_gateway: ModelGateway | None = None,
) -> AsyncIterator[BootstrappedApplication]:
    """Build the Day 1 object graph and reliably release infrastructure resources."""

    database = DatabaseBootstrap(config.database_url)
    gateway = (
        model_gateway if model_gateway is not None else OpenAICompatibleModelGateway(config)
    )
    graph_runtime = LangGraphRuntime(gateway)
    application = BootstrappedApplication(
        runtime=NexusRuntime(graph_runtime),
        database=database,
    )
    try:
        yield application
    finally:
        await database.close()
