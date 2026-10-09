"""One bounded pre-mutation reminder per execution window, never a task scheduler."""

from nexus.core.types import Json, PlanStatus, Session, ToolCall, ToolResult

PLAN_IDLE_THRESHOLD = 8
PRE_MUTATION_THRESHOLD = 24
GUIDANCE = """[Nexus runtime guidance]

You have taken many tool-using steps, and Nexus has not observed an
apply_patch-reported file mutation in this execution window.

Reassess the current task state before continuing.

If the task requires code changes and you have enough evidence, attempt a focused
implementation and keep the plan current.
If the task is analysis-only and you have enough evidence to answer, stop optional
exploration and respond.
If more investigation is genuinely necessary, identify the concrete unresolved blocker
before continuing.
Treat a proposed cause as a hypothesis; test it against the observed behavior with the
smallest distinguishing local check.
If external references or the exact test environment are unavailable, use a local check
of the same behavior rather than repeating failed lookups without a new lead.
Avoid optional exploration that does not advance the task."""

DELEGATION_GUIDANCE = """If an unresolved question concerns an independent module and
spawn_agent can replace further Main exploration, delegate that one question with
known paths and required interfaces. Use returned source evidence to advance
implementation; verify uncertain claims at integration boundaries. Work directly
when delegation would only repeat known searches. Delegation is optional; keep
time for edits and checks."""


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
