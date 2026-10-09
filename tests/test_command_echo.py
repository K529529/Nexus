from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path

import pytest

from nexus.app.events import Events
from nexus.app.session import SessionLog, read_records, replay
from nexus.core.agent import run_turn
from nexus.core.context import groups
from nexus.core.observations import command_reference
from nexus.core.request_context import logical_messages
from nexus.core.types import ExecutionContext, Limits, Message, Session, ToolResult
from nexus.tools.registry import native_tools
from tests.conftest import ScriptedModel, call, python_command, reply
from tests.test_observation_lifecycle import active, add, context, ignore, recorder

COMMAND = "print('中文🙂')\n" * 200


def seeded(tmp_path: Path, count: int = 1) -> Session:
    session = Session(tmp_path, run_id="run", resume_run_id="run")
    add(session, Message("system", "rules"))
    add(session, Message("user", "task"))
    for i in range(count):
        ident = f"c{i}"
        add(session, reply(call("exec_command", {"command": COMMAND}, ident)).message)
        add(
            session,
            ToolResult(
                ident, True, {"command": COMMAND, "stdout": "ok", "stderr": "", "exit_code": 0}
            ).message(),
        )
    return session


def test_exact_echo_preserves_all_other_facts_and_raw_history(tmp_path: Path) -> None:
    session = seeded(tmp_path)
    raw = deepcopy(session.messages)
    ctx = context(100_000)
    first = ctx.project(session, active(session))
    assert first == ctx.project(session, first.messages)
    assert session.messages == raw
    original = json.loads(raw[-1].content)
    visible = next(m for m in first.messages if m.role == "tool")
    caller = next(c for m in first.messages for c in m.tool_calls)
    projected = json.loads(visible.content)
    assert projected["data"].pop("command_ref") == "c0"
    projected["data"]["command"] = json.loads(caller.arguments_json)["command"]
    assert projected == original
    assert first.diagnostics["command_echo_references"] == 1
    assert first.diagnostics["command_echo_saved_bytes"] == len(raw[-1].content.encode()) - len(
        visible.content.encode()
    )
    assert first.diagnostics["hot_full_count"] == 1
    groups(first.messages)


@pytest.mark.parametrize(
    "fault",
    [
        "no_caller",
        "wrong_tool",
        "wrong_command",
        "wrong_id",
        "broken_args",
        "broken_result",
        "existing_ref",
        "tiny",
        "duplicate",
        "unpaired",
    ],
)
def test_ambiguous_or_unavailable_binding_keeps_original(tmp_path: Path, fault: str) -> None:
    session = seeded(tmp_path)
    caller = session.messages[-2].tool_calls[0]
    result = session.messages[-1]
    logical = list(session.messages)
    if fault == "no_caller":
        logical.remove(session.messages[-2])
    elif fault == "wrong_tool":
        caller.name = "mcp__shell"
    elif fault == "wrong_command":
        caller.arguments_json = json.dumps({"command": COMMAND + "different"})
    elif fault == "wrong_id":
        value = json.loads(result.content)
        value["call_id"] = "other"
        result.content = json.dumps(value)
    elif fault == "broken_args":
        caller.arguments_json = "{"
    elif fault == "broken_result":
        result.content = "{"
    elif fault == "existing_ref":
        value = json.loads(result.content)
        value["data"]["command_ref"] = "other"
        result.content = json.dumps(value)
    elif fault == "tiny":
        caller.arguments_json = json.dumps({"command": "x"})
        result.content = ToolResult("c0", True, {"command": "x"}).message().content
    elif fault == "duplicate":
        logical = logical[:-2] + [deepcopy(logical[-2]), deepcopy(result)] + logical[-2:]
    else:
        logical.insert(-1, Message("user", "break group"))
    projected = context(100_000).project(session, logical)
    assert next(m for m in reversed(projected.messages) if m.role == "tool") is result
    assert projected.diagnostics["command_echo_references"] == 0


def test_cold_preview_keeps_raw_provenance(tmp_path: Path) -> None:
    session = seeded(tmp_path, 3)
    add(session, reply(text="consumed").message)
    projected = context(1000).project(session, active(session))
    assert projected.diagnostics["cold_compacted_count"] == 3
    assert projected.diagnostics["command_echo_references"] == 0
    for raw, visible in zip(session.messages, logical_messages(projected.messages), strict=True):
        if raw.role == "tool":
            assert json.loads(visible.content)["source_bytes"] == len(raw.content.encode())
            assert "command_ref" not in visible.content


async def test_compaction_keeps_full_source_then_rebinds(tmp_path: Path) -> None:
    session = seeded(tmp_path, 4)
    ctx = context(100_000)
    original = deepcopy(session.messages)
    projected = ctx.project(session, active(session))
    assert projected.diagnostics["command_echo_references"] == 4
    await ctx.prepare(
        session,
        projected.messages,
        [],
        ScriptedModel([reply(text="Earlier observations summarized")]),
        recorder(session),
        force=True,
    )
    logical = active(session)
    assert session.messages == original
    assert all('"command_ref"' not in m.content for m in logical)
    assert any(m.role == "tool" for m in logical)
    again = ctx.project(session, logical)
    assert again.diagnostics["command_echo_references"] > 0
    groups(again.messages)


async def test_real_failed_process_request_and_durable_replay(execution: ExecutionContext) -> None:
    session = Session(execution.workspace)
    writer = SessionLog.create(session, "check", {}, execution.workspace)
    command = python_command(
        "import sys; unused="
        + repr("payload" * 150)
        + "; print('CHECK FAILED',file=sys.stderr); raise SystemExit(7)"
    )
    model = ScriptedModel(
        [reply(call("exec_command", {"command": command}, "check")), reply(text="Check failed")]
    )
    try:
        result = await run_turn(
            session,
            "check",
            model,
            native_tools(),
            Events(session, writer, ignore),
            Limits(shell=execution.shell),
        )
        assert result.outcome == "completed"
        request = model.requests[1]
        tool = next(m for m in request if m.role == "tool")
        parsed = json.loads(tool.content)
        assert parsed["ok"] is False
        assert parsed["data"]["exit_code"] == (1 if os.name == "nt" else 7)
        assert "CHECK FAILED" in parsed["data"]["stderr"]
        assert parsed["data"]["command_ref"] == "check"
        assert "command" not in parsed["data"]
        caller = next(c for m in request for c in m.tool_calls if c.id == "check")
        assert json.loads(caller.arguments_json)["command"] == command
        records, _ = read_records(writer.stream)
        restored = replay(records, execution.workspace)
        assert restored.messages == session.messages
        raw = next(m for m in restored.messages if m.role == "tool")
        assert json.loads(raw.content)["data"]["command"] == command
        assert "command_ref" not in raw.content
        assert command_reference(raw, caller).content == tool.content
    finally:
        writer.close()
