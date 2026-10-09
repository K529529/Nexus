"""Legacy stagnation counters for telemetry only; never injected as model guidance."""

from nexus.core.types import Json, PlanStatus, Session, ToolCall, ToolResult

PLAN_IDLE_THRESHOLD = 8
PRE_MUTATION_THRESHOLD = 24


def plan_signature(session: Session) -> tuple[PlanStatus, ...]:
    plan = session.plan
    return (
        tuple(item.status for item in plan.items) if plan and plan.run_id == session.run_id else ()
    )


class StagnationDetector:
    def __init__(self, session: Session) -> None:
        self.signature = plan_signature(session)
        self.plan_idle_steps = 0
        self.pre_mutation_steps = 0
        self.mutation_seen = False
        self.nudged = False

    def observe_completed_step(
        self,
        session: Session,
        batch: list[tuple[ToolCall, ToolResult]],
        *,
        step: int,
        max_steps: int,
    ) -> Json | None:
        if self.mutation_seen or not batch:
            return None
        for call, result in batch:
            count = result.data.get("changed_files")
            if call.name == "apply_patch" and type(count) is int and count > 0:
                self.mutation_seen = True
                return None

        signature = plan_signature(session)
        self.pre_mutation_steps += 1
        self.plan_idle_steps = (
            self.plan_idle_steps + 1 if signature and signature == self.signature else 0
        )
        self.signature = signature
        if self.nudged or step >= max_steps:
            return None
        if signature and self.plan_idle_steps >= PLAN_IDLE_THRESHOLD:
            trigger, threshold = "plan_idle", PLAN_IDLE_THRESHOLD
        elif self.pre_mutation_steps >= PRE_MUTATION_THRESHOLD:
            trigger, threshold = "pre_mutation", PRE_MUTATION_THRESHOLD
        else:
            return None
        self.nudged = True
        active = (
            [item.step for item in session.plan.items if item.status == "in_progress"]
            if signature and session.plan
            else []
        )
        return {
            "trigger": trigger,
            "mode": "plan" if signature else "no_plan",
            "plan_idle_steps": self.plan_idle_steps,
            "pre_mutation_steps": self.pre_mutation_steps,
            "threshold": threshold,
            "active_plan_step": active[0] if len(active) == 1 else None,
        }
