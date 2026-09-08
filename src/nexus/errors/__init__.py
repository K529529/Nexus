"""Nexus-owned error types exposed across stable boundaries."""

from __future__ import annotations


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
    ) -> None:
        super().__init__(message)
        self.code = code or self.default_code
        self.retryable = self.default_retryable if retryable is None else retryable


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
