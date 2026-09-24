"""Nexus-owned error types exposed across stable boundaries."""

from __future__ import annotations

SAFE_FAILURE_CATEGORIES = frozenset(
    {
        "ACTION_RELATION",
        "ACTION_SCHEMA",
        "COMMAND_CWD_RELATION",
        "COMMAND_TOOL_RELATION",
        "INVALID_REPOSITORY_PATH",
        "JSON_DECODE",
        "KIND_ENUM",
        "NARRATIVE_AUTHORITY",
        "NON_FROZEN_TOOL_AUTHORITY",
        "PLAN_CONSTRUCTION",
        "PLAN_SCHEMA",
        "REQUIRED_FIELD",
        "SKILL_DUPLICATE_IDS",
        "SKILL_IDS_SCHEMA",
        "SKILL_MAX_SELECTED",
        "SKILL_SUMMARY_SCHEMA",
        "SKILL_UNKNOWN_ID",
        "STEP_KEYS",
        "STEP_SCHEMA",
        "EDIT_TARGET_COUNT",
        "STEPS_SCHEMA",
        "TOP_LEVEL_KEYS",
        "TOP_LEVEL_TYPE",
        "UNEXPECTED",
    }
)


class NexusError(Exception):
    """Base class for safe, structured Nexus failures."""

    default_code = "NEXUS_ERROR"
    default_retryable = False

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        retryable: bool | None = None,
        failure_category: str | None = None,
    ) -> None:
        if failure_category is not None and failure_category not in SAFE_FAILURE_CATEGORIES:
            raise ValueError("Unknown safe failure category.")
        super().__init__(message)
        self.code = code or self.default_code
        self.retryable = self.default_retryable if retryable is None else retryable
        self.failure_category = failure_category


class ConfigurationError(NexusError):
    """The resolved Nexus configuration is missing or invalid."""

    default_code = "CONFIGURATION_ERROR"


class ModelError(NexusError):
    """A model operation failed behind the ModelGateway boundary."""

    default_code = "MODEL_ERROR"


class MCPError(NexusError):
    """An MCP connection, discovery, or call failed behind the adapter boundary."""

    default_code = "MCP_ERROR"


class SessionError(NexusError):
    """A session operation failed a Nexus business rule or persistence boundary."""

    default_code = "SESSION_ERROR"


class ToolExecutionError(NexusError):
    """A tool operation failed behind the structured Tool boundary."""

    default_code = "TOOL_EXECUTION_ERROR"


class ContextError(NexusError):
    """Repository exploration or bounded context construction failed."""

    default_code = "CONTEXT_ERROR"


class ValidationError(NexusError):
    """Validation planning or execution could not produce truthful evidence."""

    default_code = "VALIDATION_ERROR"


class PermissionDeniedError(NexusError):
    """A centralized security policy denied an operation."""

    default_code = "PERMISSION_DENIED"


__all__ = [
    "ConfigurationError",
    "ContextError",
    "MCPError",
    "ModelError",
    "NexusError",
    "PermissionDeniedError",
    "SessionError",
    "ToolExecutionError",
    "ValidationError",
]
