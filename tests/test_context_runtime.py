from __future__ import annotations

import json
from pathlib import Path

import pytest

from nexus.app.events import Events
from nexus.app.session import SessionLog, read_records, replay, resume_session
from nexus.core.agent import append_message, run_turn
from nexus.core.context import ContextBuilder, execution_budget, project_guidance
from nexus.core.plan import project_plan
from nexus.core.progress import ProgressLedger
from nexus.core.types import (
    ExecutionContext,
    Json,
    Limits,
    Message,
    RuntimeEvent,
    Session,
    Tool,
    ToolResult,
    ToolSpec,
    Usage,
)
from tests.conftest import Recorder, ScriptedModel, call, reply


async def ignore(event: RuntimeEvent) -> None:
    pass


def saved_bytes(writer: SessionLog) -> bytes:
    # Windows locks the first byte against other handles, including read-only ones.
    writer.stream.seek(0)
    return writer.stream.read()


async def test_new_run_projection_keeps_full_jsonl_history(tmp_path: Path) -> None:
    session = Session(tmp_path)
    writer = SessionLog.create(session, "Task A", {}, tmp_path)
    events = Events(session, writer, ignore)

    async def inspect(args: Json, context: ExecutionContext, emit: object) -> ToolResult:
        return ToolResult(context.call_id, True, {"stdout": args["output"]})

    registry = {"inspect": Tool(ToolSpec("inspect", "", {}), inspect)}
    model = ScriptedModel(
        [
            reply(call("inspect", {"output": "Task A source and test logs"})),
            reply(text="Task A explanation"),
            # IDs only need to be unique inside the active run.
            reply(call("inspect", {"output": "Task B observation"})),
            reply(text="Task B answer"),
        ]
    )
    try:
        await append_message(session, Message("system", "system + root repository rules"), events)
        assert (await run_turn(session, "Task A", model, registry, events, Limits())).outcome == (
            "completed"
        )
        first_run = session.run_id
        history = list(session.messages)
        before = saved_bytes(writer)
        assert (await run_turn(session, "Task B", model, registry, events, Limits())).outcome == (
            "completed"
        )
        assert session.run_id != first_run
        assert session.messages[: len(history)] == history
        assert model.requests[2] == project_guidance(
            project_plan(session, [session.messages[0], session.messages[len(history)]]),
            execution_budget(1, 40)
            + "\n\n"
            + ProgressLedger().prompt(session, 1, 40, list(registry)),
        )
        assert all("Task A" not in m.content for m in model.requests[3])
        assert "Task B observation" in model.requests[3][-2].content
        assert "Task A source and test logs" in model.requests[1][-2].content
        assert saved_bytes(writer).startswith(before)

        raw = saved_bytes(writer)
        assert b"Execution budget" not in raw
        assert session.run_id is not None
        ContextBuilder().build_active_context(session, session.run_id)
        assert saved_bytes(writer) == raw
        records, truncated = read_records(writer.stream)
        assert not truncated
        starts = [r for r in records if r["kind"] == "run_started"]
        assert first_run is not None
        assert [r["run_id"] for r in starts] == [first_run, session.run_id]
        for index, start in enumerate(starts):
            end = starts[index + 1]["seq"] if index + 1 < len(starts) else writer.seq + 1
            run_messages = [
                r for r in records if r["kind"] == "message" and start["seq"] < r["seq"] < end
            ]
            assert run_messages and all(r["run_id"] == start["run_id"] for r in run_messages)
        stored = [r for r in records if r["kind"] == "message"]
        assert len(stored) == len(session.messages)
        assert [r["data"] for r in stored] == [m.public() for m in session.messages]
        assert all(r["schema_version"] == 1 and "run_id" not in r["data"] for r in stored)
        restored = replay(records, tmp_path)
        assert [(m.public(), m.seq, m.run_id) for m in restored.messages] == [
            (m.public(), m.seq, m.run_id) for m in session.messages
        ]
        assert restored.resume_run_id is None
    finally:
        writer.close()


async def test_budget_ignores_old_runs_but_protected_output_can_still_overflow(
    tmp_path: Path,
) -> None:
    historical = Message("user", "old logs " * 10000, run_id="old-run")
    session = Session(tmp_path, messages=[Message("system", "rules"), historical])
    limits = Limits(context_window=2500, max_output_tokens=500)
    model = ScriptedModel([reply(text="Small new task answer")])
    assert (await run_turn(session, "Small new task", model, {}, Recorder(), limits)).outcome == (
        "completed"
    )
    assert historical not in model.requests[0] and historical in session.messages

    async def large(args: Json, context: ExecutionContext, emit: object) -> ToolResult:
        return ToolResult(context.call_id, True, {"stdout": "current run data " * 1000})

    response = reply(call("large", {}))
    response.usage = Usage()  # No artificial small usage calibration from the scripted model.
    model = ScriptedModel([response])
    recorder = Recorder()
    result = await run_turn(
        session,
        "Inspect large output",
        model,
        {"large": Tool(ToolSpec("large", "", {}), large)},
        recorder,
        limits,
    )
    assert result.outcome == "limited" and result.reason and "context_limit" in result.reason
    assert len(model.requests) == 1
    assert "current run data " * 1000 in session.messages[-1].content
    assert not any(kind == "context_compacted" for kind, _ in recorder.events)


@pytest.mark.parametrize("outcome", [None, "aborted", "failed", "limited"])
@pytest.mark.parametrize("truncated", [False, True])
async def test_resume_restores_only_interrupted_run(
    tmp_path: Path, outcome: str | None, truncated: bool
) -> None:
    session = Session(tmp_path)
    writer = SessionLog.create(session, "Task A", {}, tmp_path)
    events = Events(session, writer, ignore)
    await append_message(session, Message("system", "root rules"), events)
    await run_turn(
        session,
        "Old completed task",
        ScriptedModel([reply(text="Old answer")]),
        {},
        events,
        Limits(),
    )
    session.run_id = "interrupted-run"
    await events("run_started", {"workspace": str(tmp_path)})
    await append_message(session, Message("user", "Unfinished task"), events)
    assistant = reply(
        call("exec_command", {"command": "must never replay"}, "read"),
        call("exec_command", {"command": "must never replay"}, "pending"),
        text="Inspecting the task",
    ).message
    assistant.protocol_data = {"continuation": "required"}
    await append_message(session, assistant, events)
    await append_message(
        session, ToolResult("read", True, {"stdout": "previous result"}).message(), events
    )
    if outcome:
        await append_message(
            session, ToolResult("pending", False, {}, "not_executed").message(), events
        )
        await events("run_finished", {"outcome": outcome})
    original_records, _ = read_records(writer.stream)
    path = writer.path
    writer.close()
    if truncated:
        with path.open("ab") as stream:
            stream.write(b'{"schema_version":')
    original_bytes = path.read_bytes()
    restored, writer, _ = resume_session(path, tmp_path, tmp_path)
    # Closing and reopening before a model call must not lose recovery membership.
    recovered_path = writer.path
    writer.close()
    restored, writer, _ = resume_session(recovered_path, tmp_path, tmp_path)
    try:
        assert restored.resume_run_id == "interrupted-run"
        before_resume = writer.seq
        model = ScriptedModel([reply(text="Recovered answer")])
        result = await run_turn(
            restored, "Continue", model, {}, Events(restored, writer, ignore), Limits()
        )
        assert result.outcome == "completed" and result.tool_calls == 0
        assert restored.run_id == "interrupted-run" and restored.resume_run_id is None
        records, truncated_tail = read_records(writer.stream)
        assert not truncated_tail
        resumed_records = [r for r in records if r["seq"] > before_resume]
        assert resumed_records[0]["kind"] == "run_started"
        assert all(r["run_id"] == "interrupted-run" for r in resumed_records)
        active = model.requests[0]
        assert execution_budget(1, 40) in active[-1].content
        assert b"Execution budget" not in saved_bytes(writer)
        assert [m.role for m in active[:-1]] == [
            "system",
            "user",
            "assistant",
            "tool",
            "tool",
            "user",
        ]
        assert active[1].content == "Unfinished task"
        assert active[2].protocol_data == {"continuation": "required"}
        assert "previous result" in active[3].content
        assert json.loads(active[4].content)["error_code"] == (
            "not_executed" if outcome else "interrupted_unknown"
        )
        assert all("Old" not in m.content for m in active)
        if truncated:
            assert path.read_bytes() == original_bytes
            copied, _ = read_records(writer.stream)
            for old, new in zip(original_records[1:], copied[1:], strict=False):
                assert {k: v for k, v in old.items() if k != "session_id"} == {
                    k: v for k, v in new.items() if k != "session_id"
                }
    finally:
        writer.close()
