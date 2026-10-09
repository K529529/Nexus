from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from nexus.app.config import default_shell
from nexus.core.agent import run_turn
from nexus.core.types import Json, Limits, Session, ToolCall
from nexus.tools.registry import native_tools
from tests.conftest import Recorder, ScriptedModel, call, python_command, reply


async def test_real_read_patch_test_loop(tmp_path: Path) -> None:
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    patch = (
        "--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,2 @@\n"
        " def add(a, b):\n-    return a - b\n+    return a + b\n"
    )
    model = ScriptedModel(
        [
            reply(
                call("exec_command", {"command": python_command("print(open('calc.py').read())")}),
                text="Inspecting first.",
            ),
            reply(call("apply_patch", {"patch": patch}, "c2")),
            reply(
                call(
                    "exec_command",
                    {
                        "command": python_command(
                            "from calc import add; assert add(2,3)==5; print('1 passed')"
                        )
                    },
                    "c3",
                )
            ),
            reply(text="Fixed addition; the assertion passed."),
        ]
    )
    session = Session(tmp_path)
    result = await run_turn(
        session, "fix addition", model, native_tools(), Recorder(), Limits(shell=default_shell())
    )
    assert result.outcome == "completed"
    assert (result.steps, result.model_calls, result.tool_calls) == (4, 4, 3)
    assert result.usage.total_tokens == 60
    assert "return a + b" in (tmp_path / "calc.py").read_text()
    results = [json.loads(m.content) for m in session.messages if m.role == "tool"]
    assert all(r["ok"] for r in results)
    assert "1 passed" in results[-1]["data"]["stdout"]
    assert [m for m in model.requests[1] if m.role == "tool"][-1].tool_call_id == "c1"


async def test_same_batch_failure_continues_and_model_repairs(tmp_path: Path) -> None:
    model = ScriptedModel(
        [
            reply(
                call("missing", {}, "a"),
                ToolCall("b", "exec_command", "not json"),
                call("exec_command", {"command": python_command("raise SystemExit(1)")}, "c"),
            ),
            reply(call("exec_command", {"command": python_command("print('repaired')")}, "d")),
            reply(text="done"),
        ]
    )
    session = Session(tmp_path)
    result = await run_turn(
        session, "try", model, native_tools(), Recorder(), Limits(shell=default_shell())
    )
    assert result.outcome == "completed"
    values = [json.loads(m.content) for m in session.messages if m.role == "tool"]
    assert [v["error_code"] for v in values] == [
        "unknown_tool",
        "invalid_arguments",
        "command_exit_nonzero",
        None,
    ]


@pytest.mark.parametrize(
    "response",
    [
        reply(text="", finish="stop"),
        reply(text="partial", finish="length"),
        reply(call("exec_command", {"command": "unused"}, "")),
        reply(call("exec_command", {}, "a"), call("exec_command", {}, "a")),
    ],
)
async def test_invalid_model_response_never_dispatches(tmp_path: Path, response: object) -> None:
    from nexus.core.types import ModelReply

    assert isinstance(response, ModelReply)
    recorder = Recorder()
    result = await run_turn(
        Session(tmp_path), "test", ScriptedModel([response]), native_tools(), recorder, Limits()
    )
    assert result.outcome == "failed"
    assert not any(kind == "tool_started" for kind, _ in recorder.events)


async def test_last_step_executes_tools_without_extra_summary(tmp_path: Path) -> None:
    model = ScriptedModel([reply(call("missing", {}))])
    result = await run_turn(
        Session(tmp_path), "test", model, native_tools(), Recorder(), Limits(max_steps=1)
    )
    assert result.outcome == "limited"
    assert result.tool_calls == 1 and len(model.requests) == 1


async def test_cancel_marks_remaining_unexecuted(tmp_path: Path) -> None:
    recorder = Recorder()
    session = Session(tmp_path)
    model = ScriptedModel(
        [
            reply(
                call(
                    "exec_command",
                    {
                        "command": python_command(
                            "import time; print('ready',flush=True); time.sleep(30)"
                        )
                    },
                    "a",
                ),
                call("exec_command", {"command": "never run"}, "b"),
            )
        ]
    )
    task = asyncio.create_task(
        run_turn(session, "cancel", model, native_tools(), recorder, Limits(shell=default_shell()))
    )
    await asyncio.wait_for(recorder.output_started.wait(), timeout=10)
    task.cancel()
    result = await task
    assert result.outcome == "aborted", result
    values: list[Json] = [json.loads(m.content) for m in session.messages if m.role == "tool"]
    assert len(values) == 2
    assert values[0]["data"]["cancelled"] is True
    assert values[1]["error_code"] == "not_executed"
