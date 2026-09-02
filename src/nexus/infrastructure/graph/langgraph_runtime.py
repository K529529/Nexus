"""Minimal Day 1 LangGraph implementation behind GraphRuntime."""

from __future__ import annotations

from typing import Any, TypedDict, cast

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from nexus.domain.agent_state import AgentState
from nexus.domain.model import ModelMessage
from nexus.domain.planning import PlanApprovalResumeInput
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

    def __init__(
        self,
        model_gateway: ModelGateway,
        *,
        checkpointer: BaseCheckpointSaver[Any] | None = None,
        interrupt_before_model_response: bool = False,
    ) -> None:
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
        self._graph = builder.compile(
            checkpointer=checkpointer,
            interrupt_before=["model_response"] if interrupt_before_model_response else None,
        )

    async def run(
        self,
        state: AgentState,
        *,
        thread_id: str | None = None,
    ) -> AgentState:
        """Translate Nexus state into/out of the hidden LangGraph implementation."""

        graph_state: _GraphState = {
            "task": state.task,
            "messages": state.messages,
            "run_id": state.run_id,
            "session_id": state.session_id,
            "status": state.status,
        }
        config = _thread_config(
            thread_id,
            run_id=state.run_id,
            session_id=state.session_id,
        )
        try:
            result = await self._graph.ainvoke(graph_state, config=config)
        except NexusError:
            raise
        except Exception as exc:
            raise NexusError(
                "The graph execution failed.",
                code="GRAPH_EXECUTION_ERROR",
                retryable=True,
            ) from exc
        status_override = (
            RuntimeStatus.INTERRUPTED
            if thread_id is not None and result.get("status") == RuntimeStatus.STARTED
            else None
        )
        return self._to_agent_state(
            cast(_GraphState, result), state, status_override=status_override
        )

    async def resume(
        self,
        *,
        thread_id: str,
        resume_input: PlanApprovalResumeInput | None = None,
    ) -> AgentState:
        """Resume exclusively from the checkpointer-owned latest thread snapshot."""

        if resume_input is not None:
            raise NexusError(
                "This checkpoint is not waiting for Plan approval.",
                code="INVALID_APPROVAL_TRANSITION",
            )

        try:
            result = await self._graph.ainvoke(None, config=_thread_config(thread_id))
        except Exception as exc:
            raise NexusError(
                "Nexus could not resume the persisted graph execution.",
                code="GRAPH_RESUME_ERROR",
                retryable=True,
            ) from exc
        if not isinstance(result, dict):
            raise NexusError(
                "The resumed graph returned invalid state.",
                code="GRAPH_INVALID_STATE",
            )
        return self._to_agent_state(cast(_GraphState, result), None)

    @staticmethod
    def _to_agent_state(
        result: _GraphState,
        original: AgentState | None,
        *,
        status_override: RuntimeStatus | None = None,
    ) -> AgentState:
        messages = result.get("messages")
        if not isinstance(messages, list) or not all(
            isinstance(message, ModelMessage) for message in messages
        ):
            raise NexusError("The graph returned invalid messages.", code="GRAPH_INVALID_STATE")

        try:
            status = status_override or RuntimeStatus(result["status"])
        except (KeyError, ValueError) as exc:
            raise NexusError(
                "The graph returned an invalid status.",
                code="GRAPH_INVALID_STATE",
            ) from exc

        return AgentState(
            task=str(result.get("task", original.task if original is not None else "")),
            messages=messages,
            run_id=str(result.get("run_id", original.run_id if original is not None else "")),
            session_id=result.get(
                "session_id", original.session_id if original is not None else None
            ),
            status=status,
        )


def _thread_config(
    thread_id: str | None,
    *,
    run_id: str | None = None,
    session_id: str | None = None,
) -> RunnableConfig | None:
    if thread_id is None:
        return None
    metadata = {
        key: value
        for key, value in {"run_id": run_id, "session_id": session_id}.items()
        if value is not None
    }
    return {
        "configurable": {"thread_id": thread_id},
        "metadata": metadata,
    }
