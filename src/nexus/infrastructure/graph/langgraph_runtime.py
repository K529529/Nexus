"""Minimal Day 1 LangGraph implementation behind GraphRuntime."""

from __future__ import annotations

from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from nexus.domain.agent_state import AgentState
from nexus.domain.model import ModelMessage
from nexus.domain.ports.model_gateway import ModelGateway
from nexus.domain.runtime_events import RuntimeStatus
from nexus.errors import NexusError


class _GraphState(TypedDict):
    task: str
    messages: list[ModelMessage]
    run_id: str
    session_id: str | None
    status: RuntimeStatus


class LangGraphRuntime:
    """Compile and execute START -> model_response -> END."""

    def __init__(self, model_gateway: ModelGateway) -> None:
        async def model_response(state: _GraphState) -> dict[str, object]:
            response = await model_gateway.complete(state["messages"])
            assistant = ModelMessage(role="assistant", content=response.content)
            return {
                "messages": [*state["messages"], assistant],
                "status": RuntimeStatus.COMPLETED,
            }

        builder = StateGraph(_GraphState)
        builder.add_node("model_response", model_response)
        builder.add_edge(START, "model_response")
        builder.add_edge("model_response", END)
        self._graph = builder.compile()

    async def run(self, state: AgentState) -> AgentState:
        """Translate Nexus state into/out of the hidden LangGraph implementation."""

        graph_state: _GraphState = {
            "task": state.task,
            "messages": state.messages,
            "run_id": state.run_id,
            "session_id": state.session_id,
            "status": state.status,
        }
        result = await self._graph.ainvoke(graph_state)
        messages = result.get("messages")
        if not isinstance(messages, list) or not all(
            isinstance(message, ModelMessage) for message in messages
        ):
            raise NexusError("The graph returned invalid messages.", code="GRAPH_INVALID_STATE")

        try:
            status = RuntimeStatus(result["status"])
        except (KeyError, ValueError) as exc:
            raise NexusError(
                "The graph returned an invalid status.",
                code="GRAPH_INVALID_STATE",
            ) from exc

        return AgentState(
            task=str(result.get("task", state.task)),
            messages=messages,
            run_id=str(result.get("run_id", state.run_id)),
            session_id=result.get("session_id", state.session_id),
            status=status,
        )
