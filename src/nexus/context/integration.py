"""Compatibility bridge from Day 4 requests to the Day 5 ContextManager."""

from collections.abc import Awaitable, Callable
from dataclasses import replace

from nexus.domain.context import ContextRequest
from nexus.domain.exploration import ContextBuildRequest, WorkingContext
from nexus.domain.persistence import SessionTurn
from nexus.domain.ports.context import ContextManager
from nexus.domain.ports.skills import SkillRegistry, SkillSelector
from nexus.domain.skills import SelectedSkill


class ManagedContextBuilder:
    def __init__(
        self,
        manager: ContextManager,
        repository_id: Callable[[], Awaitable[str]],
        workspace: str,
        turns: Callable[[str], Awaitable[list[SessionTurn]]] | None = None,
        skill_registry: SkillRegistry | None = None,
        skill_selector: SkillSelector | None = None,
    ) -> None:
        self._manager = manager
        self._repository_id = repository_id
        self._workspace = workspace
        self._turns = turns
        self._skill_registry = skill_registry
        self._skill_selector = skill_selector
        if (skill_registry is None) is not (skill_selector is None):
            raise ValueError("Skill registry and selector must be configured together.")

    async def build(self, request: ContextBuildRequest) -> WorkingContext:
        selection = None
        selected_skills: tuple[SelectedSkill, ...] = ()
        if self._skill_registry is not None and self._skill_selector is not None:
            metadata = await self._skill_registry.scan_metadata()
            selection = await self._skill_selector.select(
                run_id=request.run_id,
                task=request.task,
                available_skills=metadata,
            )
            selected_skills = await self._skill_registry.load_selected(selection)
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
        context = replace(
            context,
            selected_skills=selected_skills,
            skill_selection_result=selection,
        )
        return await self._manager.prepare_agent_context(
            working_context=context, plan=None, observations=(),
            conversation_turns=(() if self._turns is None
                                else await self._turns(request.session_id)),
        )
