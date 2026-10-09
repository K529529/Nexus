from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from nexus.core.agent import run_turn
from nexus.core.stagnation import (
    PLAN_IDLE_THRESHOLD,
    PRE_MUTATION_THRESHOLD,
    StagnationDetector,
)
from nexus.core.types import (
    Emit,
    ExecutionContext,
    Json,
    Limits,
    ModelReply,
    PlanItem,
    PlanState,
    PlanStatus,
    Session,
    Tool,
    ToolResult,
    ToolSpec,
)
from nexus.tools.plan import create_update_plan_tool
from tests.conftest import Recorder, ScriptedModel, call, reply


def set_plan(session: Session, *statuses: PlanStatus, text: str = "Inspect") -> None:
    assert session.run_id
    session.plan = PlanState(session.run_id, tuple(PlanItem(text, status) for status in statuses))


def observe(
    detector: StagnationDetector,
    session: Session,
    *,
    name: str = "exec_command",
    count: Any = None,
    ok: bool = True,
    step: int = 1,
    max_steps: int = 40,
) -> Json | None:
    return detector.observe_completed_step(
        session,
        [(call(name, {}), ToolResult("c1", ok, {"changed_files": count}))],
        step=step,
        max_steps=max_steps,
    )


def test_global_threshold_counts_once_per_complete_batch(tmp_path: Path) -> None:
    assert PLAN_IDLE_THRESHOLD == 8 and PRE_MUTATION_THRESHOLD == 24
    session = Session(tmp_path, run_id="run")
    detector = StagnationDetector(session)
    assert detector.observe_completed_step(session, [], step=1, max_steps=40) is None
    for step in range(1, 24):
        assert observe(detector, session, step=step) is None
        assert detector.plan_idle_steps == 0
    nudge = observe(detector, session, step=24)
    assert nudge == {
        "trigger": "pre_mutation",
        "mode": "no_plan",
        "plan_idle_steps": 0,
        "pre_mutation_steps": 24,
        "threshold": 24,
        "active_plan_step": None,
    }
    for step in range(25, 40):
        assert observe(detector, session, step=step) is None


@pytest.mark.parametrize("status", ["pending", "in_progress", "completed"])
def test_plan_anchor_creation_cosmetic_updates_and_empty_mode(
    tmp_path: Path, status: PlanStatus
) -> None:
    session = Session(tmp_path, run_id="run")
    detector = StagnationDetector(session)
    assert observe(detector, session) is None
    set_plan(session, status)
    assert observe(detector, session) is None
    assert (detector.pre_mutation_steps, detector.plan_idle_steps) == (2, 0)
    for idle in range(1, 8):
        set_plan(session, status, text=f"Cosmetic wording {idle}")
        assert observe(detector, session) is None
        assert detector.plan_idle_steps == idle
    nudge = observe(detector, session)
    assert nudge and nudge["trigger"] == "plan_idle" and nudge["pre_mutation_steps"] == 10
    assert nudge["active_plan_step"] == ("Cosmetic wording 7" if status == "in_progress" else None)

    other = StagnationDetector(session)
    assert observe(other, session) is None
    set_plan(session)
    assert observe(other, session) is None
    assert (other.pre_mutation_steps, other.plan_idle_steps) == (2, 0)
    assert observe(other, session) is None and other.plan_idle_steps == 0


@pytest.mark.parametrize("pattern", ["toggle", "length", "clear"])
def test_plan_thrashing_cannot_reset_global_counter(tmp_path: Path, pattern: str) -> None:
    session = Session(tmp_path, run_id="run")
    detector = StagnationDetector(session)
    for step in range(1, 25):
        if pattern == "toggle":
            set_plan(session, "pending" if step % 2 else "in_progress")
        elif pattern == "length":
            statuses: list[PlanStatus] = ["pending"] * (1 + step % 2)
            set_plan(session, *statuses)
        else:
            statuses = ["pending"] if step % 2 else []
            set_plan(session, *statuses)
        nudge = observe(detector, session, step=step)
        assert detector.pre_mutation_steps == step and detector.plan_idle_steps == 0
        if step < 24:
            assert nudge is None
        else:
            assert nudge and nudge["trigger"] == "pre_mutation"


def test_transition_resets_only_idle_and_both_thresholds_prefer_plan(tmp_path: Path) -> None:
    session = Session(tmp_path, run_id="run")
    detector = StagnationDetector(session)
    for _ in range(15):
        observe(detector, session)
    set_plan(session, "in_progress", "pending")
    observe(detector, session)
    for _ in range(7):
        assert observe(detector, session) is None
    nudge = observe(detector, session)
    assert nudge and nudge["trigger"] == "plan_idle" and nudge["pre_mutation_steps"] == 24
    set_plan(session, "completed", "in_progress")
    observe(detector, session)
    assert (detector.pre_mutation_steps, detector.plan_idle_steps) == (25, 0)


@pytest.mark.parametrize("ok", [True, False])
def test_reported_mutation_wins_and_disables_even_at_threshold(tmp_path: Path, ok: bool) -> None:
    session = Session(tmp_path, run_id="run")
    detector = StagnationDetector(session)
    for _ in range(23):
        observe(detector, session)
    set_plan(session, "in_progress")
    batch = [
        (call("update_plan", {}, "p"), ToolResult("p", True, {})),
        (call("apply_patch", {}, "a"), ToolResult("a", ok, {"changed_files": 1})),
    ]
    assert detector.observe_completed_step(session, batch, step=24, max_steps=40) is None
    assert detector.mutation_seen and not detector.nudged
    for _ in range(30):
        assert observe(detector, session) is None


@pytest.mark.parametrize("count", [None, 0, -1, True, False, "1", 1.0, [], {}])
def test_non_integer_positive_counts_are_not_mutation(tmp_path: Path, count: Any) -> None:
    session = Session(tmp_path, run_id="run")
    detector = StagnationDetector(session)
    for step in range(1, 25):
        nudge = observe(detector, session, name="apply_patch", count=count, ok=False, step=step)
    assert nudge and not detector.mutation_seen and detector.pre_mutation_steps == 24


@pytest.mark.parametrize("name", ["exec_command", "update_plan", "mcp__edit"])
def test_other_tool_mutations_are_out_of_scope(tmp_path: Path, name: str) -> None:
    session = Session(tmp_path, run_id="run")
    detector = StagnationDetector(session)
    observe(detector, session, name=name, count=3)
    assert not detector.mutation_seen and detector.pre_mutation_steps == 1


def test_final_step_no_nudge_and_foreign_plan_is_not_anchor(tmp_path: Path) -> None:
    session = Session(tmp_path, run_id="new", plan=PlanState("old", (PlanItem("x", "pending"),)))
    detector = StagnationDetector(session)
    for step in range(1, 25):
        assert observe(detector, session, step=step, max_steps=24) is None
    assert detector.plan_idle_steps == 0 and not detector.nudged


async def read_tool(args: Json, context: ExecutionContext, emit: Emit) -> ToolResult:
    return ToolResult(context.call_id, True, {"stdout": "observed"})


def registry(session: Session) -> dict[str, Tool]:
    return {
        "exec_command": Tool(ToolSpec("exec_command", "read", {}), read_tool),
        "update_plan": create_update_plan_tool(session),
    }


def reads(count: int, *, prefix: str = "r", batch_size: int = 1) -> list[ModelReply]:
    return [
        reply(*(call("exec_command", {}, f"{prefix}{step}-{i}") for i in range(batch_size)))
        for step in range(count)
    ]


async def test_stagnation_is_telemetry_only(tmp_path: Path) -> None:
    session = Session(tmp_path)
    events = Recorder()
    model = ScriptedModel(reads(26) + [reply(text="analysis complete")])
    result = await run_turn(session, "inspect", model, registry(session), events, Limits())
    assert result.outcome == "completed"
    assert len([d for k, d in events.events if k == "stagnation_observation"]) == 1
    assert not any("Nexus runtime guidance" in m.content for ms in model.requests for m in ms)
    assert not any(k == "completion_nudge" for k, _ in events.events)
