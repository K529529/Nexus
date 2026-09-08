"""Stable Nexus-owned provider ports."""

from nexus.domain.ports.approval_unit_of_work import (
    ApprovalUnitOfWork,
    ApprovalUnitOfWorkFactory,
)
from nexus.domain.ports.checkpoint_provider import CheckpointProvider
from nexus.domain.ports.graph_runtime import GraphRuntime
from nexus.domain.ports.mcp import MCPManager
from nexus.domain.ports.model_gateway import ModelGateway
from nexus.domain.ports.repositories import (
    ApprovalRepository,
    RepositoryRepository,
    RunRepository,
    SessionRepository,
    SessionTurnRepository,
)
from nexus.domain.ports.session_unit_of_work import (
    SessionUnitOfWork,
    SessionUnitOfWorkFactory,
)
from nexus.domain.ports.tooling import (
    ApprovalPolicy,
    CommandPolicy,
    InteractiveDecisionCallback,
    SandboxExecutor,
    Tool,
)

__all__ = [
    "ApprovalPolicy",
    "ApprovalRepository",
    "ApprovalUnitOfWork",
    "ApprovalUnitOfWorkFactory",
    "CheckpointProvider",
    "CommandPolicy",
    "GraphRuntime",
    "InteractiveDecisionCallback",
    "ModelGateway",
    "MCPManager",
    "RepositoryRepository",
    "RunRepository",
    "SandboxExecutor",
    "SessionRepository",
    "SessionTurnRepository",
    "SessionUnitOfWork",
    "SessionUnitOfWorkFactory",
    "Tool",
]
