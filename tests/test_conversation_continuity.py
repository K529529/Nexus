from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import httpx
import pytest

from nexus.app.events import Events
from nexus.app.session import SessionLog, read_records, replay, resume_session
from nexus.core.agent import append_message, run_turn
from nexus.core.context import Context, ContextBuilder, estimate, groups
from nexus.core.conversation import ConversationHistory, project_conversation
from nexus.core.types import (
    Emit,
    ExecutionContext,
    Json,
    Limits,
    Message,
    Session,
    Tool,
    ToolResult,
)
from nexus.tools.registry import EXEC_SPEC
from tests.conftest import Recorder, ScriptedModel, reply
from tests.test_context_runtime import ignore, saved_bytes
from tests.test_model import chunk, model_for, sse


async def test_three_runs_on_wire_and_completed_session_resume(tmp_path: Path) -> None:
    requests: list[Json] = []
    answers = iter(
        [
            [
                chunk(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "a",
                                "type": "function",
                                "function": {
                                    "name": "exec_command",
                                    "arguments": '{"command":"test"}',
                                },
                            }
                        ]
                    },
                    "tool_calls",
                )
            ],
            [
                chunk(
                    {"content": "Built editor; chose atomic save.", "reasoning_content": "PRIVATE"},
                    "stop",
                )
            ],
            [chunk({"content": "Added navigation; checks passed."}, "stop")],
            [chunk({"content": "The editor preserves the current directory."}, "stop")],
            [chunk({"content": "Resumed completed conversation."}, "stop")],
        ]
    )

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, content=sse(next(answers)))

    async def tool(args: Json, context: ExecutionContext, emit: Emit) -> ToolResult:
        return ToolResult(context.call_id, True, {"stdout": "RAW_OLD_TOOL" * 1000})

    model = model_for(handle)
    session = Session(tmp_path)
    writer = SessionLog.create(session, "editor", {}, tmp_path)
    events = Events(session, writer, ignore)
    try:
        await append_message(session, Message("system", "rules"), events)
        for task in ("Build an editor", "Continue with navigation", "Explain the previous change"):
            result = await run_turn(
                session, task, model, {"exec_command": Tool(EXEC_SPEC, tool)}, events, Limits()
            )
            assert result.outcome == "completed"
        for request in requests[2:]:
            serialized = json.dumps(request)
            assert "Build an editor" in serialized and "chose atomic save" in serialized
            assert "RAW_OLD_TOOL" not in serialized and "PRIVATE" not in serialized
            assert not any(m.get("tool_calls") or m["role"] == "tool" for m in request["messages"])
        assert "Added navigation" in json.dumps(requests[3])
        assert "RAW_OLD_TOOL" in json.dumps(requests[1])
        original = saved_bytes(writer)
        path = writer.path
        writer.close()
        session, writer, _ = resume_session(path, tmp_path, tmp_path)
        assert session.resume_run_id is None
        await run_turn(
            session, "What did we deliver?", model, {}, Events(session, writer, ignore), Limits()
        )
        assert "Added navigation" in json.dumps(requests[-1])
        assert saved_bytes(writer).startswith(original)
        records, _ = read_records(writer.stream)
        restored = replay(records, tmp_path)
        assert restored.run_outcomes == session.run_outcomes
        assert all(not isinstance(m, ConversationHistory) for m in restored.messages)
    finally:
        writer.close()
        await model.close()


@pytest.mark.parametrize("outcome", ["completed", "failed", "aborted", "limited", None])
async def test_status_and_new_conversation_isolation(tmp_path: Path, outcome: str | None) -> None:
    session = Session(tmp_path, messages=[Message("user", "prior goal", run_id="old")])
    if outcome:
        session.run_outcomes["old"] = {"outcome": outcome, "reason": "reason"}
    model = ScriptedModel([reply(text="done"), reply(text="fresh")])
    await run_turn(session, "follow up", model, {}, Recorder(), Limits())
    text = "".join(m.content for m in model.requests[0])
    assert "prior goal" in text and (outcome or "interrupted/unknown") in text
    await run_turn(Session(tmp_path), "new conversation", model, {}, Recorder(), Limits())
    assert "prior goal" not in "".join(m.content for m in model.requests[1])


def test_history_bound_and_stable_prefix_with_no_raw_tools(tmp_path: Path) -> None:
    session = Session(tmp_path, run_id="active")
    for i in range(200):
        session.messages.extend(
            [
                Message("user", f"goal{i}: " + "需求" * 10000, run_id=str(i)),
                Message("assistant", f"answer{i}: " + "decision " * 1000, run_id=str(i)),
            ]
        )
    original = deepcopy(session.messages)
    active = [Message("system", "rules"), Message("user", "current", run_id="active")]
    for budget in (1500, 8000, 1000000):
        messages = project_conversation(session, active, budget)
        history = messages[1]
        assert isinstance(history, ConversationHistory)
        assert estimate([history], []) <= min(6500, budget * 0.20 + 100)
        assert "goal199" in history.content and "Older runs omitted:" in history.content
        assert "goal0:" not in history.content
        assert not history.tool_calls and history.protocol_data is None
        groups(messages)
        later = project_conversation(session, active + [Message("assistant", "progress")], budget)
        assert later[:3] == messages
    assert session.messages == original


def test_projection_is_not_compaction_input_or_reference(tmp_path: Path) -> None:
    session = Session(
        tmp_path,
        run_id="b",
        messages=[
            Message("system", "rules", seq=1),
            Message("user", "previous", seq=2, run_id="a"),
            Message("user", "current", seq=3, run_id="b"),
        ],
    )
    context = Context(Limits())
    logical = ContextBuilder().build_active_context(session, "b")
    projected = context.project(session, logical).messages
    assert any(isinstance(m, ConversationHistory) for m in projected)
    assert context.project(session, projected).messages == projected
    assert [m.seq for m in logical] == [1, 3]
    diagnostics = context.project(session, logical).diagnostics
    history = next(m for m in projected if isinstance(m, ConversationHistory))
    assert diagnostics["conversation_history_bytes"] == len(history.content.encode("utf-8"))
    assert diagnostics["conversation_history_estimated_tokens"] == estimate([history], [])


def test_escaped_history_stays_within_request_budget(tmp_path: Path) -> None:
    session = Session(
        tmp_path,
        run_id="new",
        messages=[
            Message("user", '"\\\n\t' * 10000, run_id="old"),
            Message("assistant", '"\\\n\t' * 10000, run_id="old"),
        ],
    )
    for budget in (1500, 8000, 1000000):
        projected = project_conversation(session, [], budget)
        assert estimate(projected, []) < min(6200, budget * 0.20)
