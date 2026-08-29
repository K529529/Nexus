"""The only production composition root for concrete Nexus dependencies."""

from nexus.infrastructure.bootstrap.composition import (
    BootstrappedApplication,
    BootstrappedToolApplication,
    bootstrap_application,
    bootstrap_tool_application,
)

__all__ = [
    "BootstrappedApplication",
    "BootstrappedToolApplication",
    "bootstrap_application",
    "bootstrap_tool_application",
]
