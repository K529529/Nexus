from __future__ import annotations

import json
import os
from contextlib import AsyncExitStack
from copy import deepcopy
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nexus.app import bootstrap
from nexus.app.events import Events
from nexus.app.session import SessionError, SessionLog, read_records, replay, resume_session
from nexus.core.agent import append_message, run_turn
from nexus.core.context import Context, ContextBuilder, groups
from nexus.core.plan import SNAPSHOT_HEADER, plan_data, validate_plan
from nexus.core.types import (
    Emit,
    ExecutionContext,
    Json,
    Limits,
    Message,
    PlanItem,
    PlanState,
    RuntimeEvent,
    Session,
    Tool,
    ToolResult,
    ToolSpec,
)
from nexus.tools.plan import PLAN_SPEC, create_update_plan_tool
from nexus.tools.registry import default_tools
from tests.conftest import Recorder, ScriptedModel, call, reply
from tests.test_conversation import config, ignore


def item(step: str = "Inspect", status: str = "pending") -> Json:
    return {"step": step, "status": status}


def snapshot(messages: list[Message]) -> Json:
    matches = [m.content for m in messages if SNAPSHOT_HEADER in m.content]
    assert len(matches) == 1
    assert matches[0].count(SNAPSHOT_HEADER) == 1
    return json.loads(matches[0].split(SNAPSHOT_HEADER + "\n")[1].split("\n")[0])  # type: ignore[no-any-return]


INVALID = [
    {},
    {"plan": None},
    {"plan": {}},
    {"plan": "[]"},
    {"plan": False},
    {"plan": [], "extra": True},
    {"plan": [], "explanation": 1},
    {"plan": [], "explanation": False},
    {"plan": [], "explanation": []},
    {"plan": [None]},
    {"plan": [{"step": "x"}]},
    {"plan": [{"status": "pending"}]},
    {"plan": [{**item(), "id": "x"}]},
    {"plan": [item("")]},
    {"plan": [item(" \n\t")]},
    {"plan": [{"step": 1, "status": "pending"}]},
    {"plan": [{"step": "x", "status": None}]},
    {"plan": [{"step": "x", "status": []}]},
    {"plan": [item(status="PENDING")]},
    {"plan": [item(status="done")]},
    {"plan": [item(status="in_progress"), item(status="in_progress")]},
    {"plan": [item()] * 21},
    {"plan": [item("😀" * 257)]},
    {"plan": [], "explanation": "😀" * 1025},
]


@pytest.mark.parametrize("arguments", INVALID)
async def test_invalid_plan_is_recoverable_and_preserves_state(
    tmp_path: Path, arguments: Json
) -> None:
    original = PlanState("run", (PlanItem("old", "completed"),))
    session = Session(tmp_path, run_id="run", plan=original)
    events = Recorder()
    result = await create_update_plan_tool(session).execute(
        arguments, ExecutionContext(tmp_path, "c", "unused"), events
    )
    assert not result.ok and result.error_code == "invalid_plan" and result.data["detail"]
    assert session.plan is original and not events.events


async def test_full_replacement_normalization_and_defensive_copies(tmp_path: Path) -> None:
    session = Session(tmp_path, run_id="run")
    tool = create_update_plan_tool(session)
    events = Recorder()
    updates = [
        [item("  Inspect 内部  空格\n", "in_progress"), item("Edit")],
        [item("Edit", "in_progress"), item("Inspect", "completed")],
        [item("Inspect", "pending"), item("Renamed", "completed"), item("Inspect")],
        [item("Finished", "completed")],
        [],
        [],
        [item("😀" * 256)] * 20,
    ]
    for index, entries in enumerate(updates):
        result = await tool.execute(
            {"plan": entries, "explanation": "😀" * 1024},
            ExecutionContext(tmp_path, str(index), "unused"),
            events,
        )
        assert result.ok and result.data["updated"] and session.plan
        expected = [{**entry, "step": entry["step"].strip()} for entry in entries]
        assert plan_data(session.plan.items) == result.data["plan"] == expected
        assert result.data["explanation"] == "😀" * 1024
        result.data["plan"].append(item("mutated return"))
        events.events[-1][1]["plan"].append(item("mutated event"))
        assert plan_data(session.plan.items) == expected
    assert len(events.events) == len(updates)
    assert not os.listdir(tmp_path)
    assert session.plan
    with pytest.raises(FrozenInstanceError):
        session.plan.items[0].step = "mutated"  # type: ignore[misc]


def test_schema_matches_local_constraints() -> None:
    schema = PLAN_SPEC.input_schema
    assert schema["required"] == ["plan"] and not schema["additionalProperties"]
    plan = schema["properties"]["plan"]
    assert plan["maxItems"] == 20 and plan["maxContains"] == 1 and plan["minContains"] == 0
    assert plan["contains"]["properties"]["status"]["const"] == "in_progress"
    assert plan["items"]["required"] == ["step", "status"]
    assert not plan["items"]["additionalProperties"]
    fields = plan["items"]["properties"]
    assert fields["step"] == {"type": "string", "pattern": r"\S", "maxLength": 256}
    assert fields["status"]["enum"] == ["pending", "in_progress", "completed"]
    assert schema["properties"]["explanation"] == {"type": ["string", "null"], "maxLength": 1024}
    for explanation in (None, ""):
        assert validate_plan({"plan": [], "explanation": explanation}) == ((), explanation)


@pytest.mark.parametrize("status", ["pending", "completed"])
async def test_dispatch_order_budget_final_and_next_request(tmp_path: Path, status: str) -> None:
    session = Session(tmp_path)
    events = Recorder()
    await append_message(session, Message("system", "root instructions"), events)
    seen = []

    async def inspect(args: Json, context: ExecutionContext, emit: Emit) -> ToolResult:
        seen.append(session.plan)
        return ToolResult(context.call_id, True, {})

    registry = {
        "update_plan": create_update_plan_tool(session),
        "inspect": Tool(ToolSpec("inspect", "", {}), inspect),
    }
    first = reply(
        call("update_plan", {"plan": [item(status=status)]}, "p1"),
        call("inspect", {}, "i"),
        call("update_plan", {"plan": [item("Latest", status)], "explanation": "audit only"}, "p2"),
    )
    first.message.protocol_data = {"reasoning_content": "private continuation"}
    model = ScriptedModel([first, reply(text="Legal final with any plan status")])
    result = await run_turn(session, "task", model, registry, events, Limits())
    assert result.outcome == "completed" and result.steps == 2 and result.tool_calls == 3
    assert result.model_calls == 2 and len(model.requests) == 2
    assert seen[0] and seen[0].items[0].step == "Inspect"
    assert snapshot(model.requests[0]) == {"plan": []}
    assert snapshot(model.requests[1]) == {"plan": [item("Latest", status)]}
    assert "audit only" not in model.requests[1][0].content
    assert model.requests[1][2].protocol_data == first.message.protocol_data
    groups(model.requests[1])
    assert all(SNAPSHOT_HEADER not in m.content for m in session.messages)
    updates = [data for kind, data in events.events if kind == "plan_updated"]
    assert [data["call_id"] for data in updates] == ["p1", "p2"]
    assert all(data["step"] == 1 for data in updates)
    kinds = [kind for kind, _ in events.events]
    for index, kind in enumerate(kinds):
        if kind == "plan_updated":
            assert kinds[index - 1] == "tool_started"
            assert kinds[index + 1 : index + 3] == ["message", "tool_finished"]


async def test_invalid_then_corrected_plan_and_step_limit(tmp_path: Path) -> None:
    session = Session(tmp_path)
    model = ScriptedModel(
        [
            reply(call("update_plan", {"plan": [item(status="bad")]}, "bad")),
            reply(call("update_plan", {"plan": [item(status="completed")]}, "good")),
        ]
    )
    result = await run_turn(
        session,
        "task",
        model,
        {"update_plan": create_update_plan_tool(session)},
        Recorder(),
        Limits(max_steps=2),
    )
    assert result.outcome == "limited" and result.steps == result.tool_calls == 2
    assert json.loads(session.messages[2].content)["error_code"] == "invalid_plan"
    assert session.plan and session.plan.items[0].status == "completed"


async def saved_plan(tmp_path: Path, *, clear: bool = False) -> tuple[Session, SessionLog, Events]:
    session = Session(tmp_path)
    writer = SessionLog.create(session, "plan test", {}, tmp_path)
    events = Events(session, writer, ignore)
    await append_message(session, Message("system", "root rules"), events)
    calls = [call("update_plan", {"plan": [item("committed")], "explanation": "audit"}, "p1")]
    if clear:
        calls.append(call("update_plan", {"plan": []}, "clear"))
    result = await run_turn(
        session,
        "task",
        ScriptedModel([reply(*calls)]),
        {"update_plan": create_update_plan_tool(session)},
        events,
        Limits(max_steps=1),
    )
    assert result.outcome == "limited"
    return session, writer, events


@pytest.mark.parametrize("clear", [False, True])
@pytest.mark.parametrize("truncated", [False, True])
@pytest.mark.parametrize("missing_result", [False, True])
async def test_commit_resume_recovery_and_new_run(
    tmp_path: Path, clear: bool, truncated: bool, missing_result: bool
) -> None:
    session, writer, _ = await saved_plan(tmp_path, clear=clear)
    expected, run_id = session.plan, session.run_id
    records, _ = read_records(writer.stream)
    assert replay(records, tmp_path).plan == expected
    if missing_result:
        last_commit = max(r["seq"] for r in records if r["kind"] == "plan_updated")
        # Simulate a crash immediately after the authoritative commit.
        writer.stream.seek(0)
        lines = writer.stream.readlines()
        writer.stream.seek(0)
        writer.stream.write(b"".join(lines[:last_commit]))
        writer.stream.truncate()
    path = writer.path
    writer.close()
    if truncated:
        with path.open("ab") as stream:
            stream.write(b'{"unfinished":')
    before = path.read_bytes()
    restored, writer, _ = resume_session(path, tmp_path, tmp_path)
    try:
        assert restored.plan == expected and restored.resume_run_id == run_id
        if missing_result:
            assert json.loads(restored.messages[-1].content)["error_code"] == "interrupted_unknown"
        if truncated:
            assert writer.path != path and path.read_bytes() == before
        # A second reopen must retain the copied commit as well as recovery pairs.
        path = writer.path
    finally:
        writer.close()
    restored, writer, _ = resume_session(path, tmp_path, tmp_path)
    try:
        model = ScriptedModel([reply(text="continued"), reply(text="new task")])
        events = Events(restored, writer, ignore)
        result = await run_turn(restored, "continue", model, {}, events, Limits())
        assert result.outcome == "completed" and result.tool_calls == 0
        assert restored.run_id == run_id and restored.plan == expected
        assert expected
        assert snapshot(model.requests[0]) == {"plan": plan_data(expected.items)}
        records, _ = read_records(writer.stream)
        assert replay(records, tmp_path).plan == expected
        await run_turn(restored, "independent task", model, {}, events, Limits())
        assert restored.run_id != run_id and restored.plan is None
        assert snapshot(model.requests[1]) == {"plan": []}
        records, _ = read_records(writer.stream)
        assert replay(records, tmp_path).plan is None
    finally:
        writer.close()


@pytest.mark.parametrize(
    "corruption",
    [
        "run",
        "call",
        "step",
        "boolean_step",
        "missing_explanation",
        "invalid_plan",
        "not_normalized",
        "duplicate",
        "wrong_tool",
        "missing_start",
        "already_paired",
    ],
)
async def test_malformed_commit_fails_explicitly(tmp_path: Path, corruption: str) -> None:
    _, writer, _ = await saved_plan(tmp_path)
    try:
        records, _ = read_records(writer.stream)
    finally:
        writer.close()
    event = next(r for r in records if r["kind"] == "plan_updated")
    if corruption == "run":
        event["run_id"] = "foreign-run"
    elif corruption == "call":
        event["data"]["call_id"] = "foreign-call"
    elif corruption in {"step", "boolean_step"}:
        event["data"]["step"] = True if corruption == "boolean_step" else 0
    elif corruption == "missing_explanation":
        del event["data"]["explanation"]
    elif corruption == "invalid_plan":
        event["data"]["plan"] = [item(status="bad")]
    elif corruption == "not_normalized":
        event["data"]["plan"] = [item(" untrimmed ")]
    elif corruption == "duplicate":
        records.insert(records.index(event) + 1, deepcopy(event))
    elif corruption == "wrong_tool":
        assistant = next(r for r in records if r["data"].get("tool_calls"))
        assistant["data"]["tool_calls"][0]["name"] = "apply_patch"
    elif corruption == "missing_start":
        records = [r for r in records if r["kind"] != "tool_started"]
    elif corruption == "already_paired":
        records.remove(event)
        records.insert(-1, event)
    with pytest.raises(SessionError, match="Invalid plan_updated"):
        replay(records, tmp_path)


async def test_commit_failure_stops_batch_and_does_not_change_memory(
    tmp_path: Path, monkeypatch: Any
) -> None:
    session, writer, events = await saved_plan(tmp_path)
    original = session.plan
    session.resume_run_id = session.run_id
    append = writer.append
    dispatched = []

    def fail(event: RuntimeEvent, protocol_data: Json | None = None) -> int:
        if event.kind == "plan_updated":
            assert session.plan is original
            writer.failed = True
            raise SessionError("injected flush failure")
        return append(event, protocol_data)

    async def effect(args: Json, context: ExecutionContext, emit: Emit) -> ToolResult:
        dispatched.append(True)
        return ToolResult(context.call_id, True, {})

    monkeypatch.setattr(writer, "append", fail)
    model = ScriptedModel(
        [reply(call("update_plan", {"plan": []}, "failed"), call("effect", {}, "later"))]
    )
    try:
        result = await run_turn(
            session,
            "continue",
            model,
            {
                "update_plan": create_update_plan_tool(session),
                "effect": Tool(ToolSpec("effect", "", {}), effect),
            },
            events,
            Limits(),
        )
        assert result.outcome == "failed"
        assert result.reason and "session_write_failed" in result.reason
        assert session.plan is original and not dispatched and result.tool_calls == 1
        records, _ = read_records(writer.stream)
        assert len([r for r in records if r["kind"] == "plan_updated"]) == 1
        assert not any(m.tool_call_id == "failed" for m in session.messages)
    finally:
        writer.close()


async def test_default_assembly_isolation_custom_override_and_resume(
    tmp_path: Path, monkeypatch: Any
) -> None:
    cfg = config(monkeypatch)
    cfg.limits.max_steps = 1
    models: list[ScriptedModel] = []

    class Model(ScriptedModel):
        def __init__(self, _: object) -> None:
            number = len(models)
            super().__init__(
                [reply(call("update_plan", {"plan": [item(str(number))]}, str(number)))]
            )
            models.append(self)

        async def close(self) -> None:
            pass

    monkeypatch.setattr(bootstrap, "ChatModel", Model)
    first = bootstrap.Conversation(tmp_path, cfg, ignore, home=tmp_path)
    second = bootstrap.Conversation(tmp_path, cfg, ignore, home=tmp_path)
    custom = bootstrap.Conversation(tmp_path, cfg, ignore, home=tmp_path, registry={})
    assert not custom.registry
    assert set(first.registry) == {"exec_command", "apply_patch", "update_plan"}
    try:
        await first.turn("task 0")
        await second.turn("task 1")
        assert first.session.plan and first.session.plan.items[0].step == "0"
        assert second.session.plan and second.session.plan.items[0].step == "1"
        assert first.writer
        path = first.writer.path
    finally:
        await first.close()
        await second.close()
        await custom.close()
    resumed = bootstrap.Conversation(tmp_path, cfg, ignore, home=tmp_path, resume=path)
    try:
        assert resumed.session.plan == first.session.plan
        await resumed.turn("continue")
        assert snapshot(models[2].requests[0]) == {"plan": [item("0")]}
        assert resumed.session.plan and resumed.session.plan.items[0].step == "2"
        assert first.session.plan.items[0].step == "0"
    finally:
        await resumed.close()


async def test_projection_rebuilds_and_preserves_metadata(tmp_path: Path) -> None:
    session, writer, _ = await saved_plan(tmp_path)
    try:
        root = session.messages[0]
        root.protocol_data = {"private": {"value": "original"}}
        original = deepcopy(session.messages)
        before, _ = read_records(writer.stream)
        context = Context(Limits())
        logical = ContextBuilder().build_active_context(session, session.run_id or "")
        first = context.project(session, logical).messages
        second = context.project(session, first).messages
        assert first == second and snapshot(first) == {"plan": [item("committed")]}
        assert (second[0].seq, second[0].run_id, second[0].protocol_data) == (
            root.seq,
            root.run_id,
            root.protocol_data,
        )
        assert second[0].protocol_data
        second[0].protocol_data["private"]["value"] = "mutated"
        second[0].content = "mutated"
        assert session.messages == original
        after, _ = read_records(writer.stream)
        assert after == before and SNAPSHOT_HEADER not in json.dumps(after)
    finally:
        writer.close()


async def test_commit_is_durable_before_memory_and_redacted_before_publication(
    tmp_path: Path,
) -> None:
    session = Session(tmp_path)
    writer = SessionLog.create(session, "task", {}, tmp_path)
    published = []

    async def consume(event: RuntimeEvent) -> None:
        published.append(event)
        if event.kind == "plan_updated":
            assert session.plan is None
            records, _ = read_records(writer.stream)
            assert records[-1]["kind"] == "plan_updated"

    events = Events(session, writer, consume, ("secret-token",))
    model = ScriptedModel(
        [
            reply(
                call(
                    "update_plan",
                    {"plan": [item("inspect secret-token")], "explanation": "secret-token audit"},
                )
            ),
            reply(text="done"),
        ]
    )
    try:
        result = await run_turn(session, "task", model, default_tools(session), events, Limits())
        assert result.outcome == "completed" and session.plan
        records, _ = read_records(writer.stream)
        assert "secret-token" not in json.dumps(records)
        assert "secret-token" not in str(published)
        restored = replay(records, tmp_path)
        assert restored.plan and restored.plan.items[0].step == "inspect [REDACTED]"
        assert any(r["kind"] == "plan_updated" for r in records)
    finally:
        writer.close()


async def test_mcp_cannot_replace_bound_progress_tool(tmp_path: Path, monkeypatch: Any) -> None:
    from nexus.tools import mcp

    class Client:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *args: Any) -> None:
            pass

        async def list_tools(self, **kwargs: Any) -> Any:
            return SimpleNamespace(
                result_type="complete",
                next_cursor=None,
                tools=[
                    SimpleNamespace(name="remote", description="", input_schema={"type": "object"})
                ],
            )

    session = Session(tmp_path)
    registry = default_tools(session)
    original = registry["update_plan"]
    monkeypatch.setattr(mcp, "public_name", lambda *args: "update_plan")
    async with AsyncExitStack() as stack:
        unavailable = await mcp.connect_servers(
            {"test": {"command": "unused"}}, registry, stack, Recorder(), client_factory=Client
        )
    assert unavailable == ["test"] and registry["update_plan"] is original
    assert set(registry) == {"exec_command", "apply_patch", "update_plan"}


async def test_cli_accepts_plan_and_unknown_events_without_duplicate_output(tmp_path: Path) -> None:
    import io

    from rich.console import Console

    from nexus.app.tui import Transcript

    output = io.StringIO()
    transcript = Transcript(Console(file=output, color_system=None))
    session = Session(tmp_path)
    writer = SessionLog.create(session, "task", {}, tmp_path)
    events = Events(session, writer, transcript)
    try:
        result = await run_turn(
            session,
            "task",
            ScriptedModel([reply(call("update_plan", {"plan": [item()]})), reply(text="done")]),
            default_tools(session),
            events,
            Limits(),
        )
        await events("unknown_future_event", {})
        assert result.outcome == "completed"
        assert output.getvalue().count("update_plan") == 1
        assert output.getvalue().count("done") == 1
    finally:
        writer.close()
