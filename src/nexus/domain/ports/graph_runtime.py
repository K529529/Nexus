"""Nexus-owned graph execution boundary."""

from typing import Protocol

from nexus.domain.agent_state import AgentState


class GraphRuntime(Protocol):
    """Execute a Nexus-owned state without leaking graph framework types."""

    async def run(
        self,
        state: AgentState,
        *,
        thread_id: str | None = None,
    ) -> AgentState:
        """Run the graph, optionally binding it to a durable execution thread."""

        ...

    async def resume(self, *, thread_id: str) -> AgentState:
        """Resume the latest persisted checkpoint for an execution thread."""

        ...
