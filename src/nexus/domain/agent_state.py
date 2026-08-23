"""Minimal Day 1 graph state."""

from dataclasses import dataclass

from nexus.domain.model import ModelMessage
from nexus.domain.runtime_events import RuntimeStatus


@dataclass(frozen=True, slots=True)
class AgentState:
    """Nexus-owned state passed through the Day 1 graph boundary."""

    task: str
    messages: list[ModelMessage]
    run_id: str
    session_id: str | None
    status: RuntimeStatus

