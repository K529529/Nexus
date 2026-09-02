"""Day 4 structured Agent decision port."""

from typing import Protocol

from nexus.domain.agent_decision import AgentDecision, AgentDecisionRequest


class AgentDecisionAdapter(Protocol):
    async def decide(self, request: AgentDecisionRequest) -> AgentDecision: ...
