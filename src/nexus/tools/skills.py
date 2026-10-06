"""Load a Skill through the ordinary sequential tool dispatch and event commit path."""

from collections.abc import Callable
from dataclasses import asdict

from nexus.core.skills import active_skills, check_active
from nexus.core.types import (
    Emit,
    ExecutionContext,
    Json,
    Session,
    SkillSnapshot,
    Tool,
    ToolResult,
    ToolSpec,
)

SKILL_SPEC = ToolSpec(
    "load_skill",
    "Load a relevant local Skill by its directory name. Its saved guidance is included in "
    "subsequent requests for this task. Does not run scripts or grant tool permissions.",
    {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "minLength": 1,
                "maxLength": 64,
                "pattern": "^[a-z0-9]+(-[a-z0-9]+)*$",
            }
        },
        "required": ["name"],
        "additionalProperties": False,
    },
)


def create_load_skill_tool(session: Session, load: Callable[[str], SkillSnapshot]) -> Tool:
    async def execute(arguments: Json, context: ExecutionContext, emit: Emit) -> ToolResult:
        try:
            if set(arguments) != {"name"} or not isinstance(arguments["name"], str):
                raise ValueError("Expected only name as a string")
            name = arguments["name"]
            existing = next((s for s in active_skills(session) if s.name == name), None)
            value = existing or load(name)
            if existing is None:
                check_active((*active_skills(session), value))
        except ValueError as exc:
            return ToolResult(context.call_id, False, {"detail": str(exc)}, "invalid_skill")
        if existing is None:
            assert session.run_id is not None
            await emit("skill_loaded", {"call_id": context.call_id, "skill": asdict(value)})
            current = session.loaded_skills if session.skills_run_id == session.run_id else ()
            session.loaded_skills = (*current, value)
            session.skills_run_id = session.run_id
        # The body lives in a single request projection, not duplicated in ToolResult history.
        return ToolResult(
            context.call_id,
            True,
            {
                "name": value.name,
                "digest": value.digest,
                "already_active": existing is not None,
                "detail": "Skill guidance is available in the active skills request section.",
            },
        )

    return Tool(SKILL_SPEC, execute)
