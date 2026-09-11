"""Assemble and dispose approved concrete application dependencies."""

from __future__ import annotations

import json
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from langgraph.checkpoint.base import BaseCheckpointSaver
from langsmith import Client

from nexus.application.approval_service import ApprovalService
from nexus.application.diff_service import FinalDiffCollector
from nexus.application.event_publisher import (
    InProcessEventPublisher,
    LiveRuntimeEventBridge,
)
from nexus.application.execution_context import current_execution_context
from nexus.application.execution_ledger import RuntimeEventBuffer, ToolExecutionLedger
from nexus.application.observed_model_gateway import ObservedModelGateway
from nexus.application.plan_approval_service import PlanApprovalService
from nexus.application.planning import JsonAgentDecisionAdapter, ModelPlanner
from nexus.application.runtime import NexusRuntime
from nexus.application.session_service import SessionService
from nexus.application.telemetry import EventEnricher, TelemetryRedactor
from nexus.application.telemetry_subscriber import TelemetrySubscriber
from nexus.application.tool_runtime import RuntimeEventEmitter, ToolRuntime
from nexus.application.tracing_dispatcher import (
    SafeTracerDispatcher,
    TracerSink,
)
from nexus.application.validation import (
    DeterministicValidationPlanner,
    ToolValidationRunner,
)
from nexus.config.models import MCPServerConfig, RuntimeConfig
from nexus.context import SelectiveRepositoryExplorer
from nexus.context.budget import Day5ModelInputBudgetGuard
from nexus.context.chunking import LineWindowChunker
from nexus.context.integration import ManagedContextBuilder
from nexus.context.manager import BoundedContextManager
from nexus.context.retrieval import (
    HybridContextProvider,
    ToolLexicalSearchProvider,
    ToolRepositoryAccess,
)
from nexus.domain.context import ContextBudget, IndexRequest, IndexResult
from nexus.domain.mcp import MCPToolDescriptor
from nexus.domain.persistence import SessionSummary
from nexus.domain.ports.checkpoint_provider import CheckpointProvider
from nexus.domain.ports.context import ContextProvider, EmbeddingGateway
from nexus.domain.ports.graph_runtime import GraphRuntime
from nexus.domain.ports.mcp import MCPManager
from nexus.domain.ports.model_gateway import ModelGateway
from nexus.domain.ports.tooling import ApprovalPolicy
from nexus.domain.runtime_events import (
    ObservabilityWarning,
    RuntimeEvent,
    TraceFallback,
    TraceOperation,
    TraceSink,
)
from nexus.domain.tooling import JsonObject, RiskLevel
from nexus.errors import ConfigurationError, ContextError
from nexus.infrastructure.asyncio_compat import configure_asyncio_policy
from nexus.infrastructure.checkpoint import PostgresCheckpointProvider
from nexus.infrastructure.database import DatabaseBootstrap
from nexus.infrastructure.embedding import OpenAICompatibleEmbeddingGateway
from nexus.infrastructure.graph.day4_runtime import Day4LangGraphRuntime
from nexus.infrastructure.graph.langgraph_runtime import LangGraphRuntime
from nexus.infrastructure.mcp import SDKMCPManager
from nexus.infrastructure.model_gateway.openai_compatible import OpenAICompatibleModelGateway
from nexus.infrastructure.observability import (
    ConsoleTracer,
    LangSmithTracer,
    StructuredLogger,
)
from nexus.infrastructure.observability.langsmith import LangSmithClient
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
from nexus.skills import DefaultSkillRegistry, FileSkillLoader, ModelSkillSelector
from nexus.tools import MCPToolAdapter, ToolRegistry
from nexus.tools.native import build_native_tools

configure_asyncio_policy()


@dataclass(frozen=True, slots=True)
class BootstrappedApplication:
    """Application dependencies exposed to entry-point adapters."""

    runtime: NexusRuntime
    session_service: SessionService
    database: DatabaseBootstrap
    tool_runtime: ToolRuntime


@dataclass(frozen=True, slots=True)
class BootstrappedToolApplication:
    """Day 3 Tool Runtime dependencies without model/graph construction."""

    tool_runtime: ToolRuntime
    database: DatabaseBootstrap


@dataclass(frozen=True, slots=True)
class _Day7ContextServices:
    context_manager: BoundedContextManager
    budget_guard: Day5ModelInputBudgetGuard
    skill_registry: DefaultSkillRegistry
    skill_selector: ModelSkillSelector


def _build_day7_context_services(
    *,
    config: RuntimeConfig,
    gateway: ModelGateway,
    workspace: Path,
    provider: ContextProvider,
    access: ToolRepositoryAccess,
    chunker: LineWindowChunker,
) -> _Day7ContextServices:
    context_manager = BoundedContextManager(
        provider,
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
    repository_skill_root = workspace / ".nexus" / "skills"
    user_skill_root = Path.home() / ".nexus" / "skills"
    skill_loader = FileSkillLoader(repository_skill_root, user_skill_root)
    skill_registry = DefaultSkillRegistry(
        skill_loader,
        repository_skill_root,
        user_skill_root,
    )
    budget_guard = Day5ModelInputBudgetGuard(config.max_model_input_tokens)
    skill_selector = ModelSkillSelector(
        gateway,
        config.max_selected_skills,
        budget_guard,
    )
    return _Day7ContextServices(
        context_manager,
        budget_guard,
        skill_registry,
        skill_selector,
    )


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
    mcp_manager: MCPManager | None = None
    telemetry_subscriber: TelemetrySubscriber | None = None
    telemetry_subscription: object | None = None
    langsmith_tracer: LangSmithTracer | None = None
    try:
        await checkpoint_provider.setup()
        raw_gateway = (
            model_gateway if model_gateway is not None else OpenAICompatibleModelGateway(config)
        )
        unit_of_work_factory = SqlAlchemySessionUnitOfWorkFactory(database.session_factory)
        workspace = workspace_path or Path.cwd()
        session_service = SessionService(unit_of_work_factory, workspace)
        event_publisher = InProcessEventPublisher()
        event_buffer = LiveRuntimeEventBridge(event_publisher)
        ledger = ToolExecutionLedger()
        approval_factory = SqlAlchemyApprovalUnitOfWorkFactory(database.session_factory)
        plan_approval_service = PlanApprovalService(approval_factory)
        combined_emitter = _combined_emitter(event_buffer, tool_event_emitter)
        tool_runtime, executables, mcp_manager, tool_metadata, tool_sources = (
            await _build_tool_runtime(
                config,
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
        )
        sinks: list[TracerSink] = []
        if config.console_tracing_enabled:
            structured_logger = StructuredLogger()
            sinks.append(
                TracerSink(TraceSink.CONSOLE, ConsoleTracer(structured_logger))
            )
        if config.langsmith_tracing_enabled:
            if config.langsmith_api_key is None:
                raise ConfigurationError(
                    "Enabled LangSmith tracing requires a key.",
                    code="TRACE_CONFIGURATION_INVALID",
                )
            client = Client(
                api_url=config.langsmith_endpoint,
                api_key=config.langsmith_api_key.get_secret_value(),
                workspace_id=config.langsmith_workspace_id,
                hide_inputs=True,
                hide_outputs=_safe_langsmith_mapping,
                hide_metadata=_safe_langsmith_mapping,
                auto_batch_tracing=True,
            )
            langsmith_tracer = LangSmithTracer(
                cast(LangSmithClient, client), project=config.langsmith_project
            )
            sinks.append(TracerSink(TraceSink.LANGSMITH, langsmith_tracer))

        async def emit_trace_warning(
            code: str,
            sink: TraceSink,
            operation: TraceOperation,
            fallback: TraceFallback,
        ) -> None:
            context = current_execution_context()
            warning = ObservabilityWarning(
                run_id=context.run_id,
                session_id=context.session_id,
                code=code,
                sink=sink,
                operation=operation,
                fallback=fallback,
            )
            try:
                await event_publisher.publish(warning)
            except RuntimeError:
                print(
                    json.dumps(
                        {
                            "type": "ObservabilityWarning",
                            "code": code,
                            "sink": sink.value,
                            "operation": operation.value,
                            "fallback": fallback.value,
                        },
                        separators=(",", ":"),
                    ),
                    file=sys.stderr,
                )

        dispatcher = SafeTracerDispatcher(
            tuple(sinks), warning_emitter=emit_trace_warning
        )
        telemetry_subscriber = TelemetrySubscriber(
            EventEnricher(workspace, tool_sources),
            dispatcher,
            warning_emitter=emit_trace_warning,
            ledger=ledger,
            redactor=TelemetryRedactor(),
        )
        telemetry_subscription = event_publisher.subscribe(telemetry_subscriber)
        gateway = ObservedModelGateway(
            raw_gateway,
            emit=event_publisher.publish,
            ledger=ledger,
            provider=config.model_provider,
            model=config.model_name,
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
            context_services = _build_day7_context_services(
                config=config,
                gateway=gateway,
                workspace=workspace,
                provider=HybridContextProvider(
                    ToolLexicalSearchProvider(access, chunker), semantic
                ),
                access=access,
                chunker=chunker,
            )
            context_manager = context_services.context_manager
            graph_runtime = Day4LangGraphRuntime(
                explorer=SelectiveRepositoryExplorer(tool_runtime),
                context_builder=ManagedContextBuilder(
                    context_manager,
                    session_service.context_repository_id,
                    str(workspace.resolve()),
                    session_service.context_turns,
                    context_services.skill_registry,
                    context_services.skill_selector,
                ),
                planner=ModelPlanner(
                    gateway,
                    normalize_argv=executables.normalize_argv,
                    prepare_input=context_manager.fit_model_input,
                    tool_metadata=tool_metadata,
                ),
                agent=JsonAgentDecisionAdapter(
                    gateway,
                    prepare_input=context_manager.fit_model_input,
                    tool_metadata=tool_metadata,
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
                model_call_id=gateway.last_model_call_id,
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
                event_publisher=event_publisher,
                telemetry_subscriber=telemetry_subscriber,
                ledger=ledger,
            ),
            session_service=session_service,
            database=database,
            tool_runtime=tool_runtime,
        )
        yield application
    finally:
        if telemetry_subscription is not None:
            await cast(Any, telemetry_subscription).aclose()
        if telemetry_subscriber is not None:
            await telemetry_subscriber.close()
        if langsmith_tracer is not None:
            try:
                await langsmith_tracer.close()
            except Exception:
                pass
        if mcp_manager is not None:
            await mcp_manager.close()
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
    mcp_manager: MCPManager | None = None
    try:
        tool_runtime, _, mcp_manager, _, _ = await _build_tool_runtime(
            config,
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
        if mcp_manager is not None:
            await mcp_manager.close()
        await database.close()


async def _build_tool_runtime(
    config: RuntimeConfig,
    database: DatabaseBootstrap,
    workspace: Path,
    approval_policy: ApprovalPolicy,
    event_emitter: RuntimeEventEmitter | None,
    *,
    plan_approval_service: PlanApprovalService | None = None,
    ledger: ToolExecutionLedger | None = None,
    allow_lexical_path: Callable[[str], bool] | None = None,
) -> tuple[
    ToolRuntime,
    TrustedExecutables,
    MCPManager | None,
    tuple[JsonObject, ...],
    dict[str, str],
]:
    guard = WorkspaceGuard(workspace)
    executables = TrustedExecutables.resolve(guard.root)
    manager: MCPManager | None = None
    adapters: list[MCPToolAdapter] = []
    mcp_risks: dict[str, RiskLevel] = {}
    unsupported_writes: set[str] = set()
    tool_metadata: list[JsonObject] = []
    try:
        if config.mcp_enabled:
            manager = SDKMCPManager(config.mcp_servers)
            await manager.connect()
            descriptors = await manager.list_tools()
            adapters, mcp_risks, unsupported_writes, tool_metadata = _adapt_mcp_tools(
                descriptors,
                config.mcp_servers,
                manager,
            )
        command_policy = DefaultCommandPolicy(executables, mcp_risks)
        sandbox = LocalProcessSandbox(guard, command_policy, executables)
        tools = [
            *build_native_tools(
                guard,
                sandbox,
                executables,
                allow_lexical_path=allow_lexical_path,
            ),
            *adapters,
        ]
        try:
            registry = ToolRegistry(tools)
        except ValueError as exc:
            if not adapters:
                raise
            raise ConfigurationError(
                "MCP/native Tool registration contains a duplicate name.",
                code="MCP_TOOL_COLLISION",
            ) from exc
    except Exception:
        if manager is not None:
            await manager.close()
        raise
    approval_factory = SqlAlchemyApprovalUnitOfWorkFactory(database.session_factory)
    return (
        ToolRuntime(
            registry,
            command_policy,
            approval_policy,
            ApprovalService(approval_factory),
            emit=event_emitter,
            plan_approval_service=plan_approval_service,
            normalize_argv=executables.normalize_argv,
            ledger=ledger,
            unsupported_write_operations=frozenset(unsupported_writes),
        ),
        executables,
        manager,
        tuple(tool_metadata),
        {
            tool.name: "MCP" if isinstance(tool, MCPToolAdapter) else "NATIVE"
            for tool in tools
        },
    )


def _mcp_risk(server: MCPServerConfig, remote_name: str) -> RiskLevel:
    return next(
        (
            configured.risk_level
            for configured in server.tool_risks
            if configured.tool_name == remote_name
        ),
        RiskLevel.DANGEROUS,
    )


def _adapt_mcp_tools(
    descriptors: tuple[MCPToolDescriptor, ...],
    server_configs: tuple[MCPServerConfig, ...],
    manager: MCPManager,
) -> tuple[
    list[MCPToolAdapter],
    dict[str, RiskLevel],
    set[str],
    list[JsonObject],
]:
    servers = {server.server_id: server for server in server_configs if server.enabled}
    adapters: list[MCPToolAdapter] = []
    risks: dict[str, RiskLevel] = {}
    unsupported_writes: set[str] = set()
    metadata: list[JsonObject] = []
    for descriptor in descriptors:
        server = servers.get(descriptor.server_id)
        if server is None:
            raise ConfigurationError(
                "MCP discovery returned an unknown server identity.",
                code="MCP_CONFIGURATION_INVALID",
            )
        risk = _mcp_risk(server, descriptor.remote_name)
        adapter = MCPToolAdapter(
            descriptor,
            manager,
            risk_level=risk,
            timeout_seconds=server.tool_timeout_seconds,
        )
        adapters.append(adapter)
        risks[adapter.name] = risk
        if risk is RiskLevel.WRITE:
            unsupported_writes.add(adapter.name)
        if risk is RiskLevel.SAFE:
            metadata.append(
                {
                    "registry_name": descriptor.registry_name,
                    "description": descriptor.description,
                    "input_schema": descriptor.input_schema,
                    "risk_level": risk.value,
                    "source": "mcp",
                    "server_id": descriptor.server_id,
                }
            )
    return adapters, risks, unsupported_writes, metadata


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
    buffer: RuntimeEventBuffer | LiveRuntimeEventBridge,
    external: RuntimeEventEmitter | None,
) -> RuntimeEventEmitter:
    async def emit(event: RuntimeEvent) -> None:
        await buffer.emit(event)
        if external is not None:
            await external(event)

    return emit


def _safe_langsmith_mapping(value: dict[str, Any]) -> dict[str, Any]:
    """Defense in depth: the adapter has already constructed the only safe mapping."""

    return dict(value)
