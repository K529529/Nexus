"""Assemble and dispose approved concrete dependencies through Day 3."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from langgraph.checkpoint.base import BaseCheckpointSaver

from nexus.application.approval_service import ApprovalService
from nexus.application.diff_service import FinalDiffCollector
from nexus.application.execution_ledger import RuntimeEventBuffer, ToolExecutionLedger
from nexus.application.plan_approval_service import PlanApprovalService
from nexus.application.planning import JsonAgentDecisionAdapter, ModelPlanner
from nexus.application.runtime import NexusRuntime
from nexus.application.session_service import SessionService
from nexus.application.tool_runtime import RuntimeEventEmitter, ToolRuntime
from nexus.application.validation import (
    DeterministicValidationPlanner,
    ToolValidationRunner,
)
from nexus.config.models import RuntimeConfig
from nexus.context import SelectiveRepositoryExplorer
from nexus.context.chunking import LineWindowChunker
from nexus.context.integration import ManagedContextBuilder
from nexus.context.manager import BoundedContextManager
from nexus.context.retrieval import (
    HybridContextProvider,
    ToolLexicalSearchProvider,
    ToolRepositoryAccess,
)
from nexus.domain.context import ContextBudget, IndexRequest, IndexResult
from nexus.domain.persistence import SessionSummary
from nexus.domain.ports.checkpoint_provider import CheckpointProvider
from nexus.domain.ports.context import EmbeddingGateway
from nexus.domain.ports.graph_runtime import GraphRuntime
from nexus.domain.ports.model_gateway import ModelGateway
from nexus.domain.ports.tooling import ApprovalPolicy
from nexus.domain.runtime_events import RuntimeEvent
from nexus.errors import ContextError
from nexus.infrastructure.asyncio_compat import configure_asyncio_policy
from nexus.infrastructure.checkpoint import PostgresCheckpointProvider
from nexus.infrastructure.database import DatabaseBootstrap
from nexus.infrastructure.embedding import OpenAICompatibleEmbeddingGateway
from nexus.infrastructure.graph.day4_runtime import Day4LangGraphRuntime
from nexus.infrastructure.graph.langgraph_runtime import LangGraphRuntime
from nexus.infrastructure.model_gateway.openai_compatible import OpenAICompatibleModelGateway
from nexus.infrastructure.persistence import (
    SqlAlchemyApprovalUnitOfWorkFactory,
    SqlAlchemySessionUnitOfWorkFactory,
)
from nexus.infrastructure.repository_files import RepositoryFiles
from nexus.infrastructure.sandbox import LocalProcessSandbox
from nexus.infrastructure.semantic import PgVectorRepositoryIndexer, PgVectorSemanticSearchProvider
from nexus.security import (
    AutoApprovalPolicy,
    DefaultCommandPolicy,
    TrustedExecutables,
    WorkspaceGuard,
)
from nexus.tools import ToolRegistry
from nexus.tools.native import build_native_tools

configure_asyncio_policy()


@dataclass(frozen=True, slots=True)
class BootstrappedApplication:
    """Application dependencies exposed to entry-point adapters through Day 3."""

    runtime: NexusRuntime
    session_service: SessionService
    database: DatabaseBootstrap
    tool_runtime: ToolRuntime


@dataclass(frozen=True, slots=True)
class BootstrappedToolApplication:
    """Day 3 Tool Runtime dependencies without model/graph construction."""

    tool_runtime: ToolRuntime
    database: DatabaseBootstrap


@asynccontextmanager
async def bootstrap_application(
    config: RuntimeConfig,
    *,
    model_gateway: ModelGateway | None = None,
    workspace_path: Path | None = None,
    interrupt_before_model_response: bool = False,
    approval_policy: ApprovalPolicy | None = None,
    tool_event_emitter: RuntimeEventEmitter | None = None,
    embedding_gateway: EmbeddingGateway | None = None,
) -> AsyncIterator[BootstrappedApplication]:
    """Build the approved object graph and reliably release infrastructure resources."""

    database = DatabaseBootstrap(config.database_url)
    checkpoint_provider: CheckpointProvider = PostgresCheckpointProvider(config.database_url)
    try:
        await checkpoint_provider.setup()
        gateway = (
            model_gateway if model_gateway is not None else OpenAICompatibleModelGateway(config)
        )
        unit_of_work_factory = SqlAlchemySessionUnitOfWorkFactory(database.session_factory)
        workspace = workspace_path or Path.cwd()
        session_service = SessionService(unit_of_work_factory, workspace)
        event_buffer = RuntimeEventBuffer()
        ledger = ToolExecutionLedger()
        approval_factory = SqlAlchemyApprovalUnitOfWorkFactory(database.session_factory)
        plan_approval_service = PlanApprovalService(approval_factory)
        combined_emitter = _combined_emitter(event_buffer, tool_event_emitter)
        tool_runtime, executables = _build_tool_runtime(
            database,
            workspace,
            approval_policy or AutoApprovalPolicy(),
            combined_emitter,
            plan_approval_service=plan_approval_service,
            ledger=ledger,
            allow_lexical_path=RepositoryFiles(
                workspace,
                config.max_file_size_bytes,
            ).allowed,
        )
        checkpointer = cast(BaseCheckpointSaver[Any], checkpoint_provider.get_checkpointer())
        legacy_runtime = LangGraphRuntime(
            gateway,
            checkpointer=checkpointer,
            interrupt_before_model_response=interrupt_before_model_response,
        )
        graph_runtime: GraphRuntime
        if interrupt_before_model_response:
            graph_runtime = legacy_runtime
        else:
            access = ToolRepositoryAccess(tool_runtime)
            chunker = LineWindowChunker()
            semantic = None
            if config.semantic_enabled:
                embedding = embedding_gateway or OpenAICompatibleEmbeddingGateway(config)
                semantic = PgVectorSemanticSearchProvider(database.session_factory, embedding)
            context_manager = BoundedContextManager(
                HybridContextProvider(ToolLexicalSearchProvider(access, chunker), semantic),
                access,
                chunker,
                ContextBudget(
                    max_retrieved_chunks=config.max_retrieved_chunks,
                    max_exploration_seed_chunks=config.max_exploration_seed_chunks,
                    max_code_context_tokens=config.max_code_context_tokens,
                    max_recent_observations=config.max_recent_observations,
                ),
                config.max_model_input_tokens,
            )
            graph_runtime = Day4LangGraphRuntime(
                explorer=SelectiveRepositoryExplorer(tool_runtime),
                context_builder=ManagedContextBuilder(
                    context_manager,
                    session_service.context_repository_id,
                    str(workspace.resolve()),
                    session_service.context_turns,
                ),
                planner=ModelPlanner(
                    gateway,
                    normalize_argv=executables.normalize_argv,
                    ledger=ledger,
                    prepare_input=context_manager.fit_model_input,
                ),
                agent=JsonAgentDecisionAdapter(
                    gateway,
                    ledger=ledger,
                    prepare_input=context_manager.fit_model_input,
                ),
                tool_runtime=tool_runtime,
                validation_planner=DeterministicValidationPlanner(),
                validation_runner=ToolValidationRunner(
                    tool_runtime,
                    emit=combined_emitter,
                ),
                plan_approval_service=plan_approval_service,
                diff_collector=FinalDiffCollector(tool_runtime),
                event_buffer=event_buffer,
                ledger=ledger,
                approval_mode=config.approval_mode,
                max_steps=config.max_steps,
                max_repair_attempts=config.max_repair_attempts,
                max_replans=config.max_replans,
                checkpointer=checkpointer,
                legacy_runtime=legacy_runtime,
                context_manager=context_manager,
                conversation_turns=session_service.context_turns,
            )
        application = BootstrappedApplication(
            runtime=NexusRuntime(
                graph_runtime,
                session_service=session_service,
                model_metadata={
                    "provider": config.model_provider,
                    "model": config.model_name,
                },
                event_buffer=event_buffer,
            ),
            session_service=session_service,
            database=database,
            tool_runtime=tool_runtime,
        )
        yield application
    finally:
        await checkpoint_provider.close()
        await database.close()


@asynccontextmanager
async def bootstrap_tool_application(
    config: RuntimeConfig,
    *,
    workspace_path: Path | None = None,
    approval_policy: ApprovalPolicy | None = None,
    tool_event_emitter: RuntimeEventEmitter | None = None,
    day5_file_filtering: bool = False,
) -> AsyncIterator[BootstrappedToolApplication]:
    """Build only the approved Day 3 Tool Runtime dependency graph."""

    database = DatabaseBootstrap(config.database_url)
    try:
        tool_runtime, _ = _build_tool_runtime(
            database,
            workspace := workspace_path or Path.cwd(),
            approval_policy or AutoApprovalPolicy(),
            tool_event_emitter,
            allow_lexical_path=(
                RepositoryFiles(workspace, config.max_file_size_bytes).allowed
                if day5_file_filtering
                else None
            ),
        )
        yield BootstrappedToolApplication(tool_runtime=tool_runtime, database=database)
    finally:
        await database.close()


def _build_tool_runtime(
    database: DatabaseBootstrap,
    workspace: Path,
    approval_policy: ApprovalPolicy,
    event_emitter: RuntimeEventEmitter | None,
    *,
    plan_approval_service: PlanApprovalService | None = None,
    ledger: ToolExecutionLedger | None = None,
    allow_lexical_path: Callable[[str], bool] | None = None,
) -> tuple[ToolRuntime, TrustedExecutables]:
    guard = WorkspaceGuard(workspace)
    executables = TrustedExecutables.resolve(guard.root)
    command_policy = DefaultCommandPolicy(executables)
    sandbox = LocalProcessSandbox(guard, command_policy, executables)
    registry = ToolRegistry(
        build_native_tools(
            guard,
            sandbox,
            executables,
            allow_lexical_path=allow_lexical_path,
        )
    )
    approval_factory = SqlAlchemyApprovalUnitOfWorkFactory(database.session_factory)
    return ToolRuntime(
        registry,
        command_policy,
        approval_policy,
        ApprovalService(approval_factory),
        emit=event_emitter,
        plan_approval_service=plan_approval_service,
        normalize_argv=executables.normalize_argv,
        ledger=ledger,
    ), executables


async def index_repository(
    config: RuntimeConfig,
    *,
    rebuild: bool = False,
    workspace_path: Path | None = None,
    embedding_gateway: EmbeddingGateway | None = None,
) -> IndexResult:
    """Standalone index entry: no chat model, checkpointer, or synthetic coding Run."""
    workspace = workspace_path or Path.cwd()
    embedding = embedding_gateway or OpenAICompatibleEmbeddingGateway(config)
    database = DatabaseBootstrap(config.database_url)
    try:
        service = SessionService(
            SqlAlchemySessionUnitOfWorkFactory(database.session_factory),
            workspace,
        )
        indexer = PgVectorRepositoryIndexer(
            database.session_factory,
            embedding,
            LineWindowChunker(),
            RepositoryFiles(workspace, config.max_file_size_bytes),
        )
        return await indexer.index(
            IndexRequest(
                await service.context_repository_id(),
                str(workspace.resolve()),
                rebuild,
            )
        )
    except ContextError:
        raise
    except Exception as exc:
        raise ContextError(
            "Repository index initialization failed.", code="INDEX_BUILD_FAILED", retryable=True
        ) from exc
    finally:
        await database.close()


async def list_repository_sessions(config: RuntimeConfig) -> list[SessionSummary]:
    """Business history listing does not request a model or semantic capability."""
    database = DatabaseBootstrap(config.database_url)
    try:
        service = SessionService(
            SqlAlchemySessionUnitOfWorkFactory(database.session_factory),
            Path.cwd(),
        )
        return await service.list_sessions()
    finally:
        await database.close()


def _combined_emitter(
    buffer: RuntimeEventBuffer,
    external: RuntimeEventEmitter | None,
) -> RuntimeEventEmitter:
    async def emit(event: RuntimeEvent) -> None:
        await buffer.emit(event)
        if external is not None:
            await external(event)

    return emit
