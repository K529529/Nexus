"""Lifecycle adapter for the official asynchronous PostgreSQL checkpointer."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from sqlalchemy.engine import make_url

from nexus.errors import NexusError

# Exact Nexus types persisted in the V1 graph state and its nested values.
# This changes deserialization policy, not the checkpoint wire format.
_CHECKPOINT_TYPES: tuple[tuple[str, str], ...] = (
    ("nexus.domain.agent_decision", "Observation"),
    ("nexus.domain.agent_decision", "ToolAction"),
    ("nexus.domain.agent_state", "AgentState"),
    ("nexus.domain.approvals", "ApprovalRequest"),
    ("nexus.domain.context", "CodeChunk"),
    ("nexus.domain.context", "ContextCandidate"),
    ("nexus.domain.context", "RetrievalSource"),
    ("nexus.domain.exploration", "ExplorationResult"),
    ("nexus.domain.exploration", "RepositoryFileEvidence"),
    ("nexus.domain.exploration", "RepositoryInstruction"),
    ("nexus.domain.exploration", "SelectedFileContext"),
    ("nexus.domain.exploration", "WorkingContext"),
    ("nexus.domain.model", "ModelMessage"),
    ("nexus.domain.model", "TokenUsageAggregate"),
    ("nexus.domain.persistence", "SessionTurn"),
    ("nexus.domain.planning", "ApprovedPlanEvidence"),
    ("nexus.domain.planning", "AuthorizationScope"),
    ("nexus.domain.planning", "ChangeKind"),
    ("nexus.domain.planning", "ChangedFile"),
    ("nexus.domain.planning", "Plan"),
    ("nexus.domain.planning", "PlanAuthorizationSource"),
    ("nexus.domain.planning", "PlanKind"),
    ("nexus.domain.planning", "PlanStatus"),
    ("nexus.domain.planning", "PlanStep"),
    ("nexus.domain.planning", "PlanStepStatus"),
    ("nexus.domain.planning", "RepairGuidance"),
    ("nexus.domain.planning", "TerminalStatus"),
    ("nexus.domain.runtime_events", "RuntimeStatus"),
    ("nexus.domain.skills", "SelectedSkill"),
    ("nexus.domain.skills", "SkillLocation"),
    ("nexus.domain.skills", "SkillMetadata"),
    ("nexus.domain.skills", "SkillSelectionResult"),
    ("nexus.domain.skills", "SkillSource"),
    ("nexus.domain.tooling", "ApprovalDecision"),
    ("nexus.domain.tooling", "PolicyDecision"),
    ("nexus.domain.tooling", "RiskLevel"),
    ("nexus.domain.tooling", "ToolError"),
    ("nexus.domain.tooling", "ToolResult"),
    ("nexus.domain.validation", "ValidationCheck"),
    ("nexus.domain.validation", "ValidationCheckKind"),
    ("nexus.domain.validation", "ValidationCheckResult"),
    ("nexus.domain.validation", "ValidationConfidence"),
    ("nexus.domain.validation", "ValidationResult"),
    ("nexus.domain.validation", "ValidationStatus"),
)


def _checkpoint_serializer() -> JsonPlusSerializer:
    return JsonPlusSerializer(allowed_msgpack_modules=_CHECKPOINT_TYPES)


class PostgresCheckpointProvider:
    """Own AsyncPostgresSaver creation, setup, and release."""

    def __init__(self, database_url: str) -> None:
        self._connection_string = _to_psycopg_connection_string(database_url)
        self._context: AbstractAsyncContextManager[AsyncPostgresSaver] | None = None
        self._checkpointer: AsyncPostgresSaver | None = None

    async def setup(self) -> None:
        if self._checkpointer is not None:
            return
        context = AsyncPostgresSaver.from_conn_string(
            self._connection_string, serde=_checkpoint_serializer()
        )
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
