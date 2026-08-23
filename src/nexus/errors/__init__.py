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


__all__ = ["ConfigurationError", "ModelError", "NexusError"]

