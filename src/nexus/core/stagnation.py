"""At most two reminders per execution window, gated by model-reported plan progress."""

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


PLAN_GUIDANCE = """[Nexus runtime guidance]

The unfinished plan statuses have stayed unchanged for several tool-using steps.
This is model-reported plan state, not proof that no code changed.

Reassess whether the current investigation is necessary for the requested behavior.
If a broad check is blocked by missing code outside the requested changes, use a
focused contract check where possible and continue the remaining requirements rather
than repairing every unrelated dependency.
When enough evidence exists, make a focused implementation and check it. Keep the
plan accurate and reserve the remaining budget for unfinished interfaces and final
integration checks. For analysis-only tasks, answer when the evidence is sufficient.
If a real blocker prevents progress, report the evidence and limitation instead of
repeating searches without a new lead."""


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
        self.nudge_count = 0
        self.completed_at_nudge = 0

    def observe_completed_step(
        self,
        session: Session,
        batch: list[tuple[ToolCall, ToolResult]],
        *,
        step: int,
        max_steps: int,
    ) -> Json | None:
        if not batch:
            return None
        signature = plan_signature(session)
        self.plan_idle_steps = (
            self.plan_idle_steps + 1 if signature and signature == self.signature else 0
        )
        self.signature = signature
        for call, result in batch:
            count = result.data.get("changed_files")
            if call.name == "apply_patch" and type(count) is int and count > 0:
                self.mutation_seen = True
                self.plan_idle_steps = 0
                return None
        if not self.mutation_seen:
            self.pre_mutation_steps += 1
        if self.nudge_count >= 2 or step >= max_steps:
            return None
        completed = signature.count("completed")
        if self.nudge_count and completed <= self.completed_at_nudge:
            return None
        unfinished = any(status != "completed" for status in signature)
        if unfinished and self.plan_idle_steps >= PLAN_IDLE_THRESHOLD:
            trigger, threshold = "plan_idle", PLAN_IDLE_THRESHOLD
        elif (
            not self.nudge_count
            and not self.mutation_seen
            and (not signature or unfinished)
            and self.pre_mutation_steps >= PRE_MUTATION_THRESHOLD
        ):
            trigger, threshold = "pre_mutation", PRE_MUTATION_THRESHOLD
        else:
            return None
        self.nudge_count += 1
        self.completed_at_nudge = completed
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
