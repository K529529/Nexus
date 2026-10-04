"""Session-bound progress tool using the ordinary registry and durable event path."""

from nexus.core.plan import MAX_EXPLANATION, MAX_ITEMS, MAX_STEP, plan_data, validate_plan
from nexus.core.types import (
    Emit,
    ExecutionContext,
    Json,
    PlanState,
    Session,
    Tool,
    ToolResult,
    ToolSpec,
)

PLAN_SPEC = ToolSpec(
    "update_plan",
    "Replace the complete progress plan. States: pending, in_progress, completed; "
    "at most one in_progress. An empty array clears it. Tracks progress only; "
    "does not execute tasks or decide task completion.",
    {
        "type": "object",
        "properties": {
            "plan": {
                "type": "array",
                "maxItems": MAX_ITEMS,
                "items": {
                    "type": "object",
                    "properties": {
                        "step": {"type": "string", "pattern": r"\S", "maxLength": MAX_STEP},
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "completed"],
                        },
                    },
                    "required": ["step", "status"],
                    "additionalProperties": False,
                },
                "contains": {"properties": {"status": {"const": "in_progress"}}},
                "minContains": 0,
                "maxContains": 1,
            },
            "explanation": {"type": ["string", "null"], "maxLength": MAX_EXPLANATION},
        },
        "required": ["plan"],
        "additionalProperties": False,
    },
)


def create_update_plan_tool(session: Session) -> Tool:
    async def update(arguments: Json, context: ExecutionContext, emit: Emit) -> ToolResult:
        try:
            items, explanation = validate_plan(arguments)
        except ValueError as exc:
            return ToolResult(context.call_id, False, {"detail": str(exc)}, "invalid_plan")
        assert session.run_id is not None
        state = PlanState(session.run_id, items)
        await emit(
            "plan_updated",
            {"call_id": context.call_id, "plan": plan_data(items), "explanation": explanation},
        )
        session.plan = state
        return ToolResult(
            context.call_id,
            True,
            {"updated": True, "plan": plan_data(items), "explanation": explanation},
        )

    return Tool(PLAN_SPEC, update)
