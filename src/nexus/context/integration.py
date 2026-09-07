"""Compatibility bridge from Day 4 requests to the Day 5 ContextManager."""

from collections.abc import Awaitable, Callable

from nexus.domain.context import ContextRequest
from nexus.domain.exploration import ContextBuildRequest, WorkingContext
from nexus.domain.persistence import SessionTurn
from nexus.domain.ports.context import ContextManager


class ManagedContextBuilder:
    def __init__(
        self,
        manager: ContextManager,
        repository_id: Callable[[], Awaitable[str]],
        workspace: str,
        turns: Callable[[str], Awaitable[list[SessionTurn]]] | None = None,
    ) -> None:
        self._manager = manager
        self._repository_id = repository_id
        self._workspace = workspace
        self._turns = turns

    async def build(self, request: ContextBuildRequest) -> WorkingContext:
        context = await self._manager.build(
            ContextRequest(
                request.task,
                await self._repository_id(),
                self._workspace,
                request.exploration,
                request.run_id,
                request.session_id,
            )
        )
        return await self._manager.prepare_agent_context(
            working_context=context, plan=None, observations=(),
            conversation_turns=(() if self._turns is None
                                else await self._turns(request.session_id)),
        )
