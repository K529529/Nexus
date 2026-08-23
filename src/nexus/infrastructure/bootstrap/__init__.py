"""The only production composition root for concrete Nexus dependencies."""

from nexus.infrastructure.bootstrap.composition import (
    BootstrappedApplication,
    bootstrap_application,
)

__all__ = ["BootstrappedApplication", "bootstrap_application"]

