"""Nexus-owned graph execution boundary."""

from typing import Protocol

from nexus.domain.agent_state import AgentState


class GraphRuntime(Protocol):
    """Execute a Nexus-owned state without leaking graph framework types."""

    async def run(self, state: AgentState) -> AgentState:
        """Run the minimal Day 1 graph and return its updated state."""

        ...

