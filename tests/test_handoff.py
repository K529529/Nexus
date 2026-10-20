from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path

import httpx
from rich.console import Console

from nexus.app.events import Events
from nexus.app.session import SessionLog, resume_session
from nexus.app.tui import Transcript
from nexus.core.agent import run_turn
from nexus.core.context import execution_budget, groups
from nexus.core.types import (
    Emit,
    ExecutionContext,
    Json,
    Limits,
    PlanItem,
    PlanState,
    RuntimeEvent,
    Session,
    Tool,
    ToolResult,
    ToolSpec,
)
from nexus.tools.registry import native_tools
from tests.conftest import Recorder, ScriptedModel, call, reply
from tests.test_context_runtime import ignore, saved_bytes
from tests.test_model import chunk, model_for, sse


async def test_last_patch_is_saved_and_resume_receives_receipt_on_wire(tmp_path: Path) -> None:
    requests: list[Json] = []
    patch = "*** Begin Patch\n*** Add File: result.txt\n+changed\n*** End Patch"
    responses = iter(
        [
            [
                chunk(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "write",
                                "type": "function",
                                "function": {
                                    "name": "apply_patch",
                                    "arguments": json.dumps({"patch": patch}),
                                },
                            }
                        ]
                    },
                    "tool_calls",
                )
            ],
            [chunk({"content": "The patch is saved; verification remains to be done."}, "stop")],
        ]
    )

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, content=sse(next(responses)))

    model = model_for(handle)
    session = Session(tmp_path)
    writer = SessionLog.create(session, "task", {}, tmp_path)
    try:
        result = await run_turn(
            session,
            "implement",
            model,
            native_tools(),
            Events(session, writer, ignore),
            Limits(max_steps=1),
        )
        assert result.outcome == "limited" and result.reason == "max_steps"
        assert result.model_calls == len(requests) == 1 and not result.final_text
        assert result.handoff
        fact = result.handoff["recent_tools"][0]
        assert fact["ok"] and fact["facts"]["changed_files"] == 1
        assert fact["no_subsequent_model_response"] is True
        assert result.handoff["verification"] == "not_assessed_by_runtime"
        path, original = writer.path, saved_bytes(writer)
        prior_run = session.run_id
        writer.close()
        session, writer, _ = resume_session(path, tmp_path, tmp_path)
        resumed = await run_turn(
            session,
            "continue with checks",
            model,
            native_tools(),
            Events(session, writer, ignore),
            Limits(max_steps=3),
        )
        assert session.run_id == prior_run and resumed.tool_calls == 0
        assert resumed.steps == 1 and resumed.handoff is None
        wire = json.dumps(requests[1])
        assert (
            "Previous execution window receipt" in wire and "no_subsequent_model_response" in wire
        )
        assert "Current model turn: 1 / 3" in wire
        assert (tmp_path / "result.txt").read_bytes() == b"changed\n"
        assert saved_bytes(writer).startswith(original)
    finally:
        writer.close()
        await model.close()


async def test_cancel_receipt_separates_unknown_from_not_executed(tmp_path: Path) -> None:
    session = Session(tmp_path, resume_run_id="r", run_id="r")
    session.plan = PlanState("r", (PlanItem("verify change", "in_progress"),))

    async def cancel(args: Json, context: ExecutionContext, emit: Emit) -> ToolResult:
        raise asyncio.CancelledError

    model = ScriptedModel([reply(call("work", {}, "one"), call("work", {}, "two"))])
    result = await run_turn(
        session,
        "work",
        model,
        {"work": Tool(ToolSpec("work", "", {}), cancel)},
        Recorder(),
        Limits(),
    )
    assert result.outcome == "aborted" and result.handoff
    assert result.handoff["unknown_effects_count"] == 1
    assert [v["error_code"] for v in result.handoff["recent_tools"]] == [
        "interrupted_unknown",
        "not_executed",
    ]
    assert result.handoff["pending_plan"] == [{"step": "verify change", "status": "in_progress"}]
    groups(session.messages)


async def test_handoff_ui_does_not_call_success_verified(tmp_path: Path) -> None:
    model = ScriptedModel([reply(call("missing", {}))])
    result = await run_turn(Session(tmp_path), "task", model, {}, Recorder(), Limits(max_steps=1))
    assert result.handoff
    from dataclasses import asdict

    stream = io.StringIO()
    await Transcript(Console(file=stream, width=160))(
        RuntimeEvent("run_finished", "", "s", "r", asdict(result))
    )
    text = stream.getvalue()
    assert "尚未完成最终交付" in text and "任务已验证" in text
    assert "unknown_tool" in text and "/resume" in text and "limited" in text


def test_boundary_guidance_keeps_hard_budget() -> None:
    assert "Budget boundary is near" not in execution_budget(1, 40)
    assert "Prioritize required validation" in execution_budget(38, 40)
    assert "last model turn" in execution_budget(40, 40)
    assert "cannot inspect them" in execution_budget(1, 1)
