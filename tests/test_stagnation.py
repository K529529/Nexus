from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any

import httpx
import pytest

from nexus.app.events import Events
from nexus.app.session import SessionLog, read_records, replay, resume_session
from nexus.core.agent import append_message, run_turn
from nexus.core.context import Context, estimate, execution_budget, groups, project_guidance
from nexus.core.plan import SNAPSHOT_HEADER
from nexus.core.stagnation import (
    GUIDANCE,
    PLAN_IDLE_THRESHOLD,
    PRE_MUTATION_THRESHOLD,
    RECOVERY_GUIDANCE,
    StagnationDetector,
)
from nexus.core.types import (
    Emit,
    ExecutionContext,
    Json,
    Limits,
    Message,
    ModelError,
    ModelReply,
    PlanItem,
    PlanState,
    PlanStatus,
    RuntimeEvent,
    Session,
    Tool,
    ToolResult,
    ToolSpec,
    Usage,
)
from nexus.tools.plan import create_update_plan_tool
from tests.conftest import Recorder, ScriptedModel, call, reply
from tests.test_conversation import ignore
from tests.test_model import chunk, model_for, sse


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


def has_guidance(messages: list[Message]) -> bool:
    return any(GUIDANCE in m.content for m in messages)


async def test_real_chain_order_one_nudge_persistence_and_fresh_resume(tmp_path: Path) -> None:
    session = Session(tmp_path)
    writer = SessionLog.create(session, "task", {}, tmp_path)
    events = Events(session, writer, ignore)
    await append_message(session, Message("system", "root rules"), events)
    responses = reads(32, batch_size=2)
    # Actual Plan tool: creation counts globally; subsequent text/explanation updates do not reset.
    responses[0] = reply(
        call(
            "update_plan",
            {
                "plan": [{"step": "Inspect", "status": "in_progress"}],
            },
            "plan",
        )
    )
    responses[4] = reply(
        call(
            "update_plan",
            {
                "plan": [{"step": "Inspect renamed", "status": "in_progress"}],
                "explanation": "audit only",
            },
            "rename",
        )
    )
    responses[7].message.protocol_data = {"reasoning_content": "PRIVATE_CONTINUATION"}
    model = ScriptedModel(responses)
    try:
        result = await run_turn(
            session, "task", model, registry(session), events, Limits(max_steps=32)
        )
        assert result.outcome == "limited" and result.reason == "max_steps"
        assert [i + 1 for i, ms in enumerate(model.requests) if has_guidance(ms)] == [10]
        records, _ = read_records(writer.stream)
        scheduled = [r for r in records if r["kind"] == "stagnation_nudge"]
        assert len(scheduled) == 1 and scheduled[0]["data"]["step"] == 9
        assert scheduled[0]["data"]["pre_mutation_steps"] == 9
        included = [r for r in records if r["data"].get("stagnation_nudge_included")]
        assert len(included) == 1 and included[0]["kind"] == "model_started"
        assert included[0]["data"]["step"] == 10
        assert scheduled[0]["seq"] < included[0]["seq"]
        assert not has_guidance(session.messages)
        assert all(GUIDANCE not in json.dumps(r["data"]) for r in records if r["kind"] == "message")
        restored = replay(records, tmp_path)
        assert restored.plan == session.plan and restored.messages == session.messages
        assert all(
            GUIDANCE not in m.content
            for snap in session.compactions.values()
            for m in snap.messages
        )
        for request in model.requests:
            groups(request)
            assert sum(m.content.count(SNAPSHOT_HEADER) for m in request) == 1
        assert next(
            m for m in model.requests[9] if m.tool_calls and m.tool_calls[0].id == "r7-0"
        ).protocol_data == {"reasoning_content": "PRIVATE_CONTINUATION"}
        started = [r["data"]["call_id"] for r in records if r["kind"] == "tool_started"]
        finished = [r["data"]["call_id"] for r in records if r["kind"] == "tool_finished"]
        assert (
            started
            == finished
            == [c.id for response in responses for c in response.message.tool_calls]
        )
        path = writer.path
    finally:
        writer.close()

    restored, resumed_writer, _ = resume_session(path, tmp_path, tmp_path)
    try:
        resumed_model = ScriptedModel(
            reads(9, prefix="resumed") + [reply(text="Analysis complete")]
        )
        result = await run_turn(
            restored,
            "continue",
            resumed_model,
            registry(restored),
            Events(restored, resumed_writer, ignore),
            Limits(),
        )
        assert result.outcome == "completed"
        assert [i + 1 for i, ms in enumerate(resumed_model.requests) if has_guidance(ms)] == [9]
        assert restored.plan and restored.plan.items[0].status == "in_progress"
        fresh = ScriptedModel(reads(9, prefix="new") + [reply(text="done")])
        result = await run_turn(
            restored,
            "independent",
            fresh,
            registry(restored),
            Events(restored, resumed_writer, ignore),
            Limits(),
        )
        assert result.outcome == "completed" and restored.plan is None
        assert not any(has_guidance(ms) for ms in fresh.requests)
    finally:
        resumed_writer.close()


async def test_global_reminder_final_boundary_and_no_workspace_changes(tmp_path: Path) -> None:
    for max_steps in (24, 27):
        session = Session(tmp_path)
        events = Recorder()
        model = ScriptedModel(reads(26) + [reply(text="done")])
        result = await run_turn(
            session, "analysis only", model, registry(session), events, Limits(max_steps=max_steps)
        )
        assert result.steps == max_steps
        scheduled = [d for k, d in events.events if k == "stagnation_nudge"]
        assert len(scheduled) == (0 if max_steps == 24 else 1)
        assert [i + 1 for i, ms in enumerate(model.requests) if has_guidance(ms)] == (
            [] if max_steps == 24 else [25]
        )
        assert result.outcome == ("limited" if max_steps == 24 else "completed")
    assert not os.listdir(tmp_path)


def test_projection_is_a_copy_and_task_neutral() -> None:
    assert "If the task requires code changes" in GUIDANCE
    assert "If the task is analysis-only" in GUIDANCE
    assert "Current active plan step" not in GUIDANCE
    anchor = Message(
        "system",
        "SYSTEM\nproject\nenvironment\nPlan",
        seq=2,
        run_id="run",
        protocol_data={"nested": ["retained"]},
    )
    original = deepcopy(anchor)
    for role in ("system", "user"):
        anchor.role = role
        projected = project_guidance([anchor], GUIDANCE)
        assert projected[0] is not anchor and projected[0].content == anchor.content
        assert projected[-1].content == GUIDANCE
        assert (projected[0].seq, projected[0].run_id) == (2, "run")
        assert projected[0].protocol_data == anchor.protocol_data
        assert projected[0].protocol_data is not None
        projected[0].protocol_data["nested"].append("changed")
        assert anchor.protocol_data == original.protocol_data
        assert anchor.content == original.content


@pytest.mark.parametrize("fallback", [False, True])
async def test_budget_compaction_retry_delivery_and_calibration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fallback: bool
) -> None:
    session = Session(tmp_path, run_id="run", resume_run_id="run")
    set_plan(session, "in_progress")
    events = Recorder()
    await append_message(session, Message("system", "root rules"), events)
    responses = reads(8)
    for response in responses:
        response.usage = Usage()  # Capacity test uses estimates, not the fake 10-token usage.
    responses[0].message.content = "old inspection evidence " * 60
    responses[0].message.protocol_data = {"reasoning_content": "private continuation"}
    tools = registry(session)
    specs = [t.spec for t in tools.values()]
    expected = deepcopy(session)
    expected.messages.append(Message("user", "continue", run_id="run", seq=2))
    for response in responses:
        assistant = deepcopy(response.message)
        assistant.run_id, assistant.seq = "run", len(expected.messages) + 1
        expected.messages.append(assistant)
        tool_result = ToolResult(assistant.tool_calls[0].id, True, {"stdout": "observed"}).message()
        tool_result.run_id, tool_result.seq = "run", len(expected.messages) + 1
        expected.messages.append(tool_result)
    normal = Context(Limits()).project(expected, expected.messages).messages
    context = Context(Limits())
    context.execution_budget = execution_budget(9, 40)
    base = estimate(context.task_request(normal), specs)
    with_nudge = estimate(context.task_request(normal, GUIDANCE), specs)
    budget = with_nudge * 3 if fallback else int((base + with_nudge) / (2 * 0.85))
    assert fallback or base < budget * 0.85 < with_nudge

    class Model(ScriptedModel):
        failed = False

        async def complete(
            self, messages: list[Message], tools: list[ToolSpec], emit: Emit
        ) -> ModelReply:
            if fallback and has_guidance(messages) and not self.failed:
                self.failed = True
                self.requests.append(deepcopy(messages))
                await emit("model_started", {"attempt": 1})
                raise ModelError("context_limit")
            return await super().complete(messages, tools, emit)

    model = Model(
        responses
        + [
            reply(text="Old observations summarized."),
            reply(call("exec_command", {}, "after-nudge")),
            reply(text="done"),
        ]
    )
    observed: list[list[Message]] = []
    original_observe = Context.observe

    def capture(
        self: Context, response: ModelReply, messages: list[Message], tools: list[ToolSpec]
    ) -> None:
        original_observe(self, response, messages, tools)
        assert self.last_estimate == estimate(messages, tools)
        assert self.last_report == response.usage.input_tokens
        observed.append(deepcopy(messages))

    monkeypatch.setattr(Context, "observe", capture)
    result = await run_turn(
        session,
        "continue",
        model,
        tools,
        events,
        Limits(context_window=budget + 1524, max_output_tokens=500),
    )
    assert result.outcome == "completed", result
    assert result.steps == 10 and result.tool_calls == 9
    assert result.model_calls == 11 + int(fallback)
    assert len(model.requests) == 11 + int(fallback)
    summary_requests = [
        ms
        for ms in model.requests
        if any(m.content.startswith("Summarize the earlier") for m in ms)
    ]
    assert len(summary_requests) == 1 and not has_guidance(summary_requests[0])
    assert all("Execution budget" not in m.content for m in summary_requests[0])
    task_requests = [ms for ms in model.requests if ms not in summary_requests]
    nudged_requests = [ms for ms in task_requests if has_guidance(ms)]
    assert len(nudged_requests) == 1 + int(fallback)
    for request in nudged_requests:
        assert sum(m.content.count(GUIDANCE) for m in request) == 1
        assert sum(m.content.count(SNAPSHOT_HEADER) for m in request) == 1
        assert sum(m.content.count(execution_budget(9, 40)) for m in request) == 1
        groups(request)
    assert len(observed) == 10 and observed[8] == nudged_requests[-1]
    assert not has_guidance(observed[9])
    assert execution_budget(10, 40) in observed[9][-1].content
    assert estimate(nudged_requests[-1], specs) <= budget
    assert not has_guidance(session.messages)
    assert all(not has_guidance(snapshot.messages) for snapshot in session.compactions.values())
    summary_events = [
        d for k, d in events.events if k == "model_started" and d.get("purpose") == "compaction"
    ]
    assert len(summary_events) == 1 and "stagnation_nudge_included" not in summary_events[0]
    included = [
        d for k, d in events.events if k == "model_started" and d.get("stagnation_nudge_included")
    ]
    assert len(included) == 1 + int(fallback) and all(d["step"] == 9 for d in included)
    assert len([d for k, d in events.events if k == "stagnation_nudge"]) == 1


async def test_actual_mock_api_request_preserves_reasoning_and_consumes_nudge(
    tmp_path: Path,
) -> None:
    session = Session(tmp_path, run_id="run", resume_run_id="run")
    set_plan(session, "in_progress")
    events = Recorder()
    await append_message(session, Message("system", "SYSTEM\nproject\nenvironment"), events)
    requests: list[Json] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        index = len(requests)
        if index <= 9:
            delta = {
                "role": "assistant",
                "reasoning_content": f"PRIVATE_{index}",
                "tool_calls": [
                    {
                        "index": 0,
                        "id": f"c{index}",
                        "type": "function",
                        "function": {"name": "exec_command", "arguments": "{}"},
                    }
                ],
            }
            return httpx.Response(200, content=sse([chunk(delta, "tool_calls")]))
        return httpx.Response(200, content=sse([chunk({"content": "done"}, "stop")]))

    model = model_for(handle)
    try:
        result = await run_turn(
            session, "analysis only", model, registry(session), events, Limits()
        )
    finally:
        await model.client.close()
    assert result.outcome == "completed" and len(requests) == 10
    for index, request in enumerate(requests, 1):
        content = request["messages"][-1]["content"]
        assert content.count(GUIDANCE) == int(index == 9)
        assert content.count(SNAPSHOT_HEADER) == 1
        assert content.count("Execution budget") == 1
        assert execution_budget(index, 40) in content
    assistants = [m for m in requests[8]["messages"] if m["role"] == "assistant"]
    assert [m["reasoning_content"] for m in assistants] == [f"PRIVATE_{i}" for i in range(1, 9)]
    assert not has_guidance(session.messages)
    assert "PRIVATE" not in json.dumps([d for k, d in events.events if k == "stagnation_nudge"])


@pytest.mark.parametrize("mutates", [False, True])
async def test_whole_batch_uses_final_plan_and_mutation_has_priority(
    tmp_path: Path, mutates: bool
) -> None:
    session = Session(tmp_path, run_id="run", resume_run_id="run")
    set_plan(session, "in_progress")
    tools = registry(session)

    async def patch(args: Json, context: ExecutionContext, emit: Emit) -> ToolResult:
        return ToolResult(context.call_id, False, {"changed_files": 1}, "patch_io_error")

    tools["apply_patch"] = Tool(ToolSpec("apply_patch", "", {}), patch)
    batch = [
        call("update_plan", {"plan": [{"step": "Inspect", "status": "completed"}]}, "p1"),
        call("update_plan", {"plan": [{"step": "Inspect", "status": "in_progress"}]}, "p2"),
    ]
    if mutates:
        batch.append(call("apply_patch", {}, "patch"))
    events = Recorder()
    model = ScriptedModel(
        reads(7) + [reply(*batch)] + reads(25, prefix="later") + [reply(text="done")]
    )
    result = await run_turn(session, "continue", model, tools, events, Limits())
    assert result.outcome == "completed"
    assert [i + 1 for i, ms in enumerate(model.requests) if has_guidance(ms)] == (
        [] if mutates else [9]
    )
    assert len([d for k, d in events.events if k == "stagnation_nudge"]) == int(not mutates)


async def test_scheduled_does_not_mean_included_when_protected_context_cannot_fit(
    tmp_path: Path,
) -> None:
    session = Session(tmp_path, run_id="run", resume_run_id="run")
    set_plan(session, "in_progress")
    events = Recorder()
    await append_message(session, Message("system", "root rules"), events)
    tools = registry(session)
    specs = [t.spec for t in tools.values()]
    projected = (
        Context(Limits())
        .project(session, session.messages + [Message("user", "continue", seq=2, run_id="run")])
        .messages
    )
    context = Context(Limits())
    context.execution_budget = execution_budget(9, 40)
    base = estimate(context.task_request(projected), specs)
    total = estimate(context.task_request(projected, GUIDANCE), specs)
    budget = (base + total) // 2
    responses = reads(8) + [reply(text="must not request")]
    # Provider calibration reveals insufficient protected capacity at the boundary
    # where the nudge is scheduled, even though earlier requests fit.
    responses[7].usage = Usage(100_000, 5, 100_005, "reported")
    model = ScriptedModel(responses)
    result = await run_turn(
        session,
        "continue",
        model,
        tools,
        events,
        Limits(context_window=budget + 1524, max_output_tokens=500),
    )
    assert result.outcome == "limited" and result.reason and "context_limit" in result.reason
    assert len(model.requests) == 8
    assert len([d for k, d in events.events if k == "stagnation_nudge"]) == 1
    assert not any(d.get("stagnation_nudge_included") for _, d in events.events)
    assert not session.compactions and not has_guidance(session.messages)


async def test_nudge_audit_write_failure_stops_further_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = Session(tmp_path)
    writer = SessionLog.create(session, "task", {}, tmp_path)
    events = Events(session, writer, ignore)
    original_append = writer.append

    def fail_nudge(event: RuntimeEvent, protocol_data: Json | None = None) -> int:
        if event.kind == "stagnation_nudge":
            raise OSError("injected append failure")
        return original_append(event, protocol_data)

    monkeypatch.setattr(writer, "append", fail_nudge)
    model = ScriptedModel(reads(25) + [reply(text="must not request")])
    try:
        result = await run_turn(session, "task", model, registry(session), events, Limits())
        assert result.outcome == "failed" and result.reason == "runtime_error:OSError"
        assert result.tool_calls == 24 and len(model.requests) == 24
        records, _ = read_records(writer.stream)
        assert not any(r["data"].get("stagnation_nudge_included") for r in records)
        assert not any(r["kind"] == "stagnation_nudge" for r in records)
        assert replay(records, tmp_path).messages == session.messages
    finally:
        writer.close()


def test_recovery_needs_reported_progress_and_stability_and_is_bounded(tmp_path: Path) -> None:
    session = Session(tmp_path, run_id="run")
    set_plan(session, "in_progress", "pending")
    detector = StagnationDetector(session)
    for step in range(1, 9):
        nudge = observe(detector, session, step=step, max_steps=80)
    assert nudge and nudge["trigger"] == "plan_idle"
    for step in range(9, 18):
        set_plan(session, "in_progress", "pending", text=f"renamed {step}")
        assert observe(detector, session, step=step, max_steps=80) is None
    set_plan(session, "completed", "in_progress")
    assert observe(detector, session, step=18, max_steps=80) is None
    for step in range(19, 26):
        assert observe(detector, session, step=step, max_steps=80) is None
    # A partial patch write also resets the idle interval.
    assert (
        observe(detector, session, name="apply_patch", count=1, ok=False, step=26, max_steps=80)
        is None
    )
    for step in range(27, 34):
        assert observe(detector, session, step=step, max_steps=80) is None
    recovery = observe(detector, session, step=34, max_steps=80)
    assert recovery and recovery["trigger"] == "plan_idle_recovery"
    set_plan(session, "completed", "completed", "in_progress")
    for step in range(35, 80):
        assert observe(detector, session, step=step, max_steps=80) is None


@pytest.mark.parametrize("statuses", [(), ("completed",), ("in_progress",)])
def test_recovery_does_not_fire_for_absent_complete_or_unadvanced_plan(
    tmp_path: Path, statuses: tuple[PlanStatus, ...]
) -> None:
    session = Session(tmp_path, run_id="run")
    set_plan(session, "in_progress")
    detector = StagnationDetector(session)
    for _ in range(8):
        observe(detector, session)
    assert detector.nudged
    set_plan(session, *statuses)
    for _ in range(25):
        assert observe(detector, session) is None
    assert not detector.recovery_sent


async def test_recovery_is_request_only_and_delivered_once_after_progress(tmp_path: Path) -> None:
    session = Session(tmp_path)
    events = Recorder()
    responses = [
        reply(
            call(
                "update_plan",
                {
                    "plan": [
                        {"step": "inspect", "status": "in_progress"},
                        {"step": "implement", "status": "pending"},
                    ]
                },
                "plan",
            )
        ),
        *reads(8, prefix="first"),
        reply(
            call(
                "update_plan",
                {
                    "plan": [
                        {"step": "inspect", "status": "completed"},
                        {"step": "implement", "status": "in_progress"},
                    ]
                },
                "advance",
            )
        ),
        *reads(17, prefix="second"),
        reply(text="done"),
    ]
    model = ScriptedModel(responses)
    result = await run_turn(session, "task", model, registry(session), events, Limits(max_steps=30))
    assert result.outcome == "completed"
    assert [i + 1 for i, ms in enumerate(model.requests) if has_guidance(ms)] == [10]
    assert [
        i + 1
        for i, ms in enumerate(model.requests)
        if any(RECOVERY_GUIDANCE in m.content for m in ms)
    ] == [19]
    assert [d["trigger"] for k, d in events.events if k == "stagnation_nudge"] == [
        "plan_idle",
        "plan_idle_recovery",
    ]
    for messages in [session.messages] + [
        snapshot.messages for snapshot in session.compactions.values()
    ]:
        assert all(RECOVERY_GUIDANCE not in m.content for m in messages)
    assert all(RECOVERY_GUIDANCE not in json.dumps(d) for k, d in events.events if k == "message")
    for request in model.requests:
        groups(request)
