from __future__ import annotations

import json
import math
from copy import deepcopy
from pathlib import Path
from typing import Any

import httpx
import pytest

from nexus.app.session import read_records, replay
from nexus.core.agent import append_message, run_turn
from nexus.core.context import Context, ContextBuilder, estimate, execution_budget, groups
from nexus.core.plan import SNAPSHOT_HEADER, plan_data
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
    Session,
    ToolResult,
    ToolSpec,
    Usage,
)
from nexus.tools.plan import PLAN_SPEC, create_update_plan_tool
from tests.conftest import Recorder, ScriptedModel, call, reply
from tests.test_model import chunk, model_for, sse
from tests.test_plan import item, saved_plan, snapshot
from tests.test_safety_compaction import add_tools


async def test_actual_api_plan_updates_clear_and_reasoning_continuation(
    tmp_path: Path, monkeypatch: Any
) -> None:
    session = Session(tmp_path)
    events = Recorder()
    await append_message(session, Message("system", "SYSTEM\nproject\nenvironment"), events)
    requests: list[Json] = []
    observed: list[list[Message]] = []
    original_observe = Context.observe

    def observe(
        self: Context, response: ModelReply, messages: list[Message], tools: list[ToolSpec]
    ) -> None:
        original_observe(self, response, messages, tools)
        assert self.last_estimate == estimate(messages, tools)
        observed.append(deepcopy(messages))

    monkeypatch.setattr(Context, "observe", observe)

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        index = len(requests)
        if index <= 2:
            args = {"plan": [item("Next visible")] if index == 1 else []}
            delta = {
                "role": "assistant",
                "reasoning_content": f"PRIVATE_{index}",
                "tool_calls": [
                    {
                        "index": 0,
                        "id": f"p{index}",
                        "type": "function",
                        "function": {"name": "update_plan", "arguments": json.dumps(args)},
                    }
                ],
            }
            return httpx.Response(200, content=sse([chunk(delta, "tool_calls")]))
        return httpx.Response(200, content=sse([chunk({"content": "done"}, "stop")]))

    model = model_for(handle)
    try:
        result = await run_turn(
            session,
            "task\n" + "Stable source context.\n" * 800,
            model,
            {"update_plan": create_update_plan_tool(session)},
            events,
            Limits(),
        )
    finally:
        await model.close()
    assert result.outcome == "completed" and result.model_calls == 3 and result.tool_calls == 2
    assert len(observed) == len(requests) == 3
    # Plan and budget changes must leave the entire previous logical wire prefix intact.
    # Only the previous ephemeral suffix is replaced before appending new history.
    for previous, following in zip(requests, requests[1:], strict=False):
        prefix = previous["messages"][:-1]
        assert following["messages"][: len(prefix)] == prefix
        assert len(json.dumps(prefix)) > 16000
    for request in requests:
        assert request["messages"][-1]["role"] == "user"
        assert sum(SNAPSHOT_HEADER in m["content"] for m in request["messages"]) == 1
        assert "sections" not in request["messages"][-1]
    for index, (request, messages) in enumerate(zip(requests, observed, strict=True)):
        assert request["messages"][0]["content"] == messages[0].content
        assert snapshot(messages) == {"plan": [item("Next visible")] if index == 1 else []}
        assert messages[0].content == "SYSTEM\nproject\nenvironment"
        assert "PRIVATE" not in messages[0].content
        assert request["tools"][0]["function"]["parameters"] == PLAN_SPEC.input_schema
    assistant = next(m for m in requests[1]["messages"] if m["role"] == "assistant")
    assert assistant["reasoning_content"] == "PRIVATE_1"
    assistants = [m for m in requests[2]["messages"] if m["role"] == "assistant"]
    assert [m["reasoning_content"] for m in assistants] == ["PRIVATE_1", "PRIVATE_2"]
    assert all(SNAPSHOT_HEADER not in m.content for m in session.messages)
    assert "PRIVATE" not in json.dumps(events.events)


async def test_cold_plan_result_can_compact_without_losing_current_plan(tmp_path: Path) -> None:
    session = Session(tmp_path, run_id="run")
    events = Recorder()
    await append_message(session, Message("system", "rules"), events)
    await append_message(session, Message("user", "task"), events)
    entries = [item(str(i) + "中" * 250) for i in range(20)]
    await append_message(
        session, reply(call("update_plan", {"plan": entries}, "p")).message, events
    )
    result = await create_update_plan_tool(session).execute(
        {"plan": entries},
        ExecutionContext(tmp_path, "p", "unused"),
        events,
    )
    await append_message(session, result.message(), events)
    for i in range(4):
        await append_message(session, reply(call("read", {}, str(i))).message, events)
        await append_message(
            session, ToolResult(str(i), True, {"text": "x" * 4000}).message(), events
        )
    original = deepcopy(session.messages)
    context = Context(Limits(context_window=12000, max_output_tokens=1000))
    logical = ContextBuilder().build_active_context(session, "run")
    projected = context.project(session, logical)
    old_result = next(m for m in projected.messages if m.tool_call_id == "p")
    assert json.loads(old_result.content)["projection"]
    assert projected.diagnostics["cold_compacted_count"] > 0
    assert snapshot(projected.messages) == {"plan": entries}
    assert session.messages == original
    groups(projected.messages)


@pytest.mark.parametrize("fallback", [False, True])
async def test_safety_summary_rebuild_fallback_and_budget_share_plan(
    tmp_path: Path, fallback: bool, monkeypatch: Any
) -> None:
    session, writer, events = await saved_plan(tmp_path)
    try:
        assert session.run_id and session.plan
        run_id, plan = session.run_id, session.plan
        await add_tools(session, events, "old_narrative", 1200)
        await add_tools(session, events, "recent1", 1)
        await add_tools(session, events, "recent2", 1)
        session.resume_run_id = run_id
        original = deepcopy(session.messages)
        logical = ContextBuilder().build_active_context(session, run_id)
        initial = Context(Limits()).project(session, logical).messages
        size = estimate(initial, [])
        limits = Limits(
            context_window=(size * 2 if fallback else int(size / 0.88)) + 1524,
            max_output_tokens=500,
        )
        checks: list[list[Message]] = []
        original_check = Context.check

        def check(self: Context, messages: list[Message], tools: list[ToolSpec]) -> None:
            assert snapshot(messages) == {"plan": plan_data(plan.items)}
            checks.append(messages)
            original_check(self, messages, tools)

        monkeypatch.setattr(Context, "check", check)

        class Model(ScriptedModel):
            async def complete(
                self, messages: list[Message], tools: list[ToolSpec], emit: Emit
            ) -> ModelReply:
                assert snapshot(messages) == {"plan": plan_data(plan.items)}
                if fallback and not self.requests:
                    self.requests.append(list(messages))
                    raise ModelError("context_limit")
                return await super().complete(messages, tools, emit)

        model = Model(
            [
                reply(text="Historical stale plan said complete; cannot update structured state."),
                reply(text="final"),
                reply(text="Unfinished plan acknowledged."),
            ]
        )
        result = await run_turn(session, "continue", model, {}, events, limits)
        assert result.outcome == "completed" and len(model.requests) == (4 if fallback else 3)
        assert session.plan is plan and session.messages[: len(original)] == original
        summary_input, final_input = model.requests[-3:-1]
        assert summary_input[-2].content.startswith("Summarize the earlier conversation")
        assert all("Execution budget" not in m.content for m in summary_input)
        assert execution_budget(1, 40) in final_input[-1].content
        if fallback:
            assert execution_budget(1, 40) in model.requests[0][-1].content
        for request in model.requests:
            groups(request)
            assert estimate(request, []) <= Context(limits).budget
        assert final_input in checks
        records, _ = read_records(writer.stream)
        compacted = next(r["data"] for r in records if r["kind"] == "context_compacted")
        if fallback:
            assert compacted["before_estimate"] == estimate(model.requests[0], [])
        else:
            assert compacted["before_estimate"] > size
        assert compacted["after_estimate"] == estimate(final_input, [])
        assert SNAPSHOT_HEADER not in json.dumps(records)
        assert "Execution budget" not in json.dumps(records)
        restored = replay(records, tmp_path)
        assert restored.plan == plan
        for message in final_input:
            if message.role == "assistant" and message.tool_calls:
                assert message.protocol_data == {"continuation": message.tool_calls[0].id}
        assert not any(SNAPSHOT_HEADER in m.content for m in session.compactions[run_id].messages)
    finally:
        writer.close()


def test_plan_counts_toward_capacity_and_calibration(tmp_path: Path) -> None:
    session = Session(
        tmp_path,
        run_id="run",
        messages=[
            Message("system", "rules", seq=1),
            Message("user", "task", seq=2, run_id="run"),
        ],
    )
    session.plan = PlanState("run", tuple(PlanItem("中" * 256, "pending") for _ in range(20)))
    context = Context(Limits(context_window=3524, max_output_tokens=500))
    logical = ContextBuilder().build_active_context(session, "run")
    projected = context.project(session, logical).messages
    assert estimate(logical, []) < context.budget < estimate(projected, [])
    with pytest.raises(ModelError, match="context_limit"):
        context.check(projected, [])
    response = reply(text="calibration")
    response.usage = Usage(12345, 2, 12347, "reported")
    context.observe(response, projected, [])
    assert context.last_estimate == estimate(projected, [])
    assert context.tokens(projected, []) == 12345
    session.plan = PlanState("run", ())
    cleared = context.project(session, projected).messages
    assert snapshot(cleared) == {"plan": []}
    assert context.tokens(cleared, []) == math.ceil(
        estimate(cleared, []) * 12345 / estimate(projected, [])
    )


async def test_protected_plan_limits_without_summary_or_truncation(tmp_path: Path) -> None:
    session = Session(tmp_path, run_id="run", resume_run_id="run")
    events = Recorder()
    await append_message(session, Message("system", "rules"), events)
    session.plan = PlanState("run", tuple(PlanItem("中" * 256, "pending") for _ in range(20)))
    original = session.plan
    model = ScriptedModel([])
    result = await run_turn(
        session, "continue", model, {}, events, Limits(context_window=3524, max_output_tokens=500)
    )
    assert result.outcome == "limited" and result.reason and "context_limit" in result.reason
    assert not model.requests and session.plan is original and not session.compactions
