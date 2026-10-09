from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from nexus.app import events as event_module
from nexus.app.events import Events
from nexus.app.session import SessionLog, read_records, replay
from nexus.core import agent
from nexus.core.agent import run_turn
from nexus.core.types import (
    Emit,
    ExecutionContext,
    Json,
    Limits,
    Message,
    ModelReply,
    Session,
    Tool,
    ToolResult,
    ToolSpec,
)
from nexus.tools.subagent import create_spawn_agent_tool
from tests.conftest import Recorder, ScriptedModel, call, reply
from tests.test_conversation import ignore


@dataclass
class Clock:
    value: float = 100.0
    wall: str = "2026-10-10T12:00:00+00:00"


class AdvancingModel(ScriptedModel):
    def __init__(self, replies: list[ModelReply], clock: Clock) -> None:
        super().__init__(replies)
        self.clock = clock

    async def complete(
        self, messages: list[Message], tools: list[ToolSpec], emit: Emit
    ) -> ModelReply:
        self.clock.value += 1
        return await super().complete(messages, tools, emit)


def assert_no_clock_in_requests(model: ScriptedModel) -> None:
    for messages in model.requests:
        assert "execution_elapsed_ms" not in json.dumps([m.public() for m in messages])


async def test_elapsed_survives_wall_rollback_and_resets_each_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock()
    monkeypatch.setattr(agent, "time", SimpleNamespace(monotonic=lambda: clock.value))
    monkeypatch.setattr(event_module, "now", lambda: clock.wall)
    session = Session(tmp_path)
    writer = SessionLog.create(session, "task", {}, tmp_path)
    events = Events(session, writer, ignore)

    async def write(args: Json, context: ExecutionContext, emit: Emit) -> ToolResult:
        clock.value += 2
        clock.wall = "2026-10-10T11:00:00+00:00"  # NTP/VM wall-clock correction.
        (context.workspace / "changed.txt").write_text("changed", encoding="utf-8")
        return ToolResult(context.call_id, True, {"changed_files": 1}, duration_ms=2000)

    try:
        for _ in range(2):
            clock.value += 20
            clock.wall = "2026-10-10T12:00:00+00:00"
            model = AdvancingModel(
                [reply(call("apply_patch", {}, "write")), reply(text="done")], clock
            )
            result = await run_turn(
                session,
                "task",
                model,
                {"apply_patch": Tool(ToolSpec("apply_patch", "write", {}), write)},
                events,
                Limits(),
            )
            assert result.outcome == "completed" and result.duration_ms == 4000
            assert_no_clock_in_requests(model)
        records, _ = read_records(writer.stream)
        starts = [i for i, r in enumerate(records) if r["kind"] == "run_started"]
        assert len(starts) == 2
        for index, start in enumerate(starts):
            end = starts[index + 1] if index + 1 < len(starts) else len(records)
            window = records[start:end]
            timed = [
                r
                for r in window
                if r["kind"]
                in {
                    "run_started",
                    "run_finished",
                    "model_started",
                    "model_finished",
                    "tool_started",
                    "tool_finished",
                }
            ]
            elapsed = [r["data"]["execution_elapsed_ms"] for r in timed]
            assert elapsed == [0, 1000, 1000, 1000, 3000, 4000, 4000, 4000]
            assert any(
                b["timestamp"] < a["timestamp"] for a, b in zip(timed, timed[1:], strict=False)
            )
            finished = next(r for r in window if r["kind"] == "tool_finished")
            message = next(r for r in records if r["seq"] == finished["data"]["message_seq"])
            assert json.loads(message["data"]["content"])["data"]["changed_files"] == 1
            assert "execution_elapsed_ms" not in json.dumps(message["data"])
        assert replay(records, tmp_path).messages == session.messages
    finally:
        writer.close()


async def test_child_timeline_keeps_its_origin_and_parent_correlation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock()
    monkeypatch.setattr(agent, "time", SimpleNamespace(monotonic=lambda: clock.value))
    (tmp_path / "module.py").write_text("value = 1\n", encoding="utf-8")
    child = AdvancingModel(
        [
            reply(call("inspect_repository", {"mode": "read", "path": "module.py"}, "read")),
            reply(text='{"findings":["module.py:1 value is 1"],"uncertainties":[]}'),
        ],
        clock,
    )
    main = AdvancingModel(
        [reply(call("spawn_agent", {"task": "inspect value"}, "child")), reply(text="done")], clock
    )
    session = Session(tmp_path)
    events = Recorder()
    result = await run_turn(
        session,
        "task",
        main,
        {"spawn_agent": create_spawn_agent_tool(session, child, Limits())},
        events,
        Limits(),
    )
    assert result.outcome == "completed" and result.duration_ms == 4000
    child_start = next(d for k, d in events.events if k == "subagent_run_started")
    child_finish = next(d for k, d in events.events if k == "subagent_run_finished")
    assert child_start["execution_elapsed_ms"] == 0
    assert child_start["parent_execution_elapsed_ms"] == 1000
    assert child_finish["execution_elapsed_ms"] == 2000
    assert child_finish["parent_execution_elapsed_ms"] == 3000
    child_model = [d for k, d in events.events if k == "subagent_model_started"]
    assert [(d["execution_elapsed_ms"], d["parent_execution_elapsed_ms"]) for d in child_model] == [
        (1000, 2000),
        (2000, 3000),
    ]
    assert all(d["parent_call_id"] == "child" for d in child_model)
    for kind in ["subagent_started", "subagent_finished"]:
        data = next(d for k, d in events.events if k == kind)
        assert "parent_execution_elapsed_ms" in data and "execution_elapsed_ms" not in data
    assert_no_clock_in_requests(main)
    assert_no_clock_in_requests(child)


async def test_resume_same_run_id_starts_a_new_timing_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock()
    monkeypatch.setattr(agent, "time", SimpleNamespace(monotonic=lambda: clock.value))
    session = Session(tmp_path)
    events = Recorder()
    first = await run_turn(
        session,
        "task",
        AdvancingModel([reply(call("unknown", {}, "first"))], clock),
        {},
        events,
        Limits(max_steps=1),
    )
    assert first.outcome == "limited" and first.duration_ms == 1000
    first_id = session.run_id
    session.resume_run_id = first_id
    clock.value += 100
    second_model = AdvancingModel([reply(text="done")], clock)
    second = await run_turn(session, "continue", second_model, {}, events, Limits())
    assert second.outcome == "completed" and second.duration_ms == 1000
    assert session.run_id == first_id
    assert [d["execution_elapsed_ms"] for k, d in events.events if k == "run_started"] == [0, 0]
    assert [d["execution_elapsed_ms"] for k, d in events.events if k == "run_finished"] == [
        1000,
        1000,
    ]
    assert_no_clock_in_requests(second_model)
