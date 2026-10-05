from __future__ import annotations

import math
import os
from copy import deepcopy
from pathlib import Path
from typing import Literal

import pytest

from nexus.core.agent import run_turn
from nexus.core.context import Context, estimate, execution_budget, groups
from nexus.core.stagnation import GUIDANCE
from nexus.core.types import Limits, Message, Session, ToolSpec, Usage
from tests.conftest import Recorder, ScriptedModel, call, reply
from tests.test_stagnation import registry


@pytest.mark.parametrize("role", ["system", "user"])
def test_request_copy_preserves_source_and_metadata(role: Literal["system", "user"]) -> None:
    logical = [
        Message(role, "rules / environment / plan", seq=3, run_id="r", protocol_data={"x": [1]})
    ]
    original = deepcopy(logical)
    context = Context(Limits())
    for step in (1, 9, 40):
        context.execution_budget = execution_budget(step, 40)
        request = context.task_request(logical, GUIDANCE)
        assert request[0].content == (
            logical[0].content + "\n\n" + GUIDANCE + "\n\n" + execution_budget(step, 40)
        )
        assert request[0].content.count("Execution budget") == 1
        assert f"Remaining model turns, including this one: {41 - step}\n" in request[0].content
        assert (request[0].seq, request[0].run_id) == (3, "r")
        assert request[0].protocol_data == {"x": [1]}
        assert request[0].protocol_data is not None
        request[0].protocol_data["x"].append(2)
        request[0].content = "modified copy"
        assert logical == original


def test_counter_changes_keep_calibration_but_other_prefix_changes_reset_it() -> None:
    context = Context(Limits())
    logical = [Message("system", "rules / plan"), Message("user", "task")]
    for change in ("plan", "schema", "nudge"):
        context.execution_budget = execution_budget(9, 40)
        request = context.task_request(logical)
        context.check(request, [])
        response = reply(text="observed")
        response.usage = Usage(1234, 1, 1235, "reported")
        context.observe(response, request, [])
        assert context.last_estimate == estimate(request, []) > estimate(logical, [])
        context.execution_budget = execution_budget(10, 40)
        following = context.task_request(logical + [response.message])
        expected = math.ceil(estimate(following, []) * 1234 / estimate(request, []))
        assert context.tokens(following, []) == expected
        assert context.last_report == 1234
        tools = []
        if change == "plan":
            following[0].content = following[0].content.replace("rules / plan", "rules / new plan")
        elif change == "schema":
            tools = [ToolSpec("new", "new tool", {})]
        else:
            following = context.task_request(logical, GUIDANCE)
        assert context.tokens(following, tools) == estimate(following, tools)
        assert context.last_report is None


async def test_protected_budget_overflow_is_honest_and_does_not_call_model(tmp_path: Path) -> None:
    session = Session(tmp_path, run_id="run", messages=[Message("system", "rules", seq=1)])
    context = Context(Limits())
    logical = session.messages + [Message("user", "task", seq=2, run_id="run")]
    projected = context.project(session, logical).messages
    base = estimate(projected, [])
    context.execution_budget = execution_budget(1, 40)
    total = estimate(context.task_request(projected), [])
    budget = (base + total) // 2
    assert base < budget < total
    model = ScriptedModel([])
    events = Recorder()
    result = await run_turn(
        session,
        "task",
        model,
        {},
        events,
        Limits(context_window=budget + 1524, max_output_tokens=500),
    )
    assert result.outcome == "limited" and result.reason and "context_limit" in result.reason
    assert not model.requests and not session.compactions


@pytest.mark.parametrize("final_tool", [False, True])
async def test_counts_turns_not_tools_and_keeps_last_turn_semantics(
    tmp_path: Path, final_tool: bool
) -> None:
    session = Session(tmp_path, messages=[Message("system", "rules", seq=1)])
    first = reply(call("exec_command", {}, "a"), call("exec_command", {}, "b"))
    first.message.protocol_data = {"reasoning_content": "private continuation"}
    last = reply(call("exec_command", {}, "c")) if final_tool else reply(text="done")
    model = ScriptedModel([first, last])
    events = Recorder()
    result = await run_turn(session, "task", model, registry(session), events, Limits(max_steps=2))
    assert result.steps == result.model_calls == len(model.requests) == 2
    assert result.tool_calls == 2 + int(final_tool)
    assert result.outcome == ("limited" if final_tool else "completed")
    assert result.reason == ("max_steps" if final_tool else None)
    for step, request in enumerate(model.requests, 1):
        assert execution_budget(step, 2) in request[0].content
        assert request[0].content.count("Execution budget") == 1
        groups(request)
    assert model.requests[1][2].protocol_data == first.message.protocol_data
    assert all("Execution budget" not in m.content for m in session.messages)
    assert all("Execution budget" not in str(data) for _, data in events.events)
    assert not any(data.get("stagnation_nudge_included") for _, data in events.events)
    assert not os.listdir(tmp_path)
