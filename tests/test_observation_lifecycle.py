from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from nexus.app.events import Events
from nexus.app.profile import RunProfiler, profile_metrics
from nexus.app.session import SessionLog, read_records, replay
from nexus.core.agent import append_message, run_turn
from nexus.core.context import (
    Context,
    ContextBuilder,
    estimate,
    execution_budget,
    groups,
    protected_seqs,
)
from nexus.core.observations import compact, hot_tool_seqs, provenance
from nexus.core.progress import ProgressLedger
from nexus.core.types import (
    Emit,
    Limits,
    Message,
    ModelError,
    ModelReply,
    RuntimeEvent,
    Session,
    Tool,
    ToolResult,
    ToolSpec,
    Usage,
)
from tests.conftest import Recorder, ScriptedModel, call, reply


def add(session: Session, message: Message) -> Message:
    message.seq = len(session.messages) + 1
    message.run_id = session.run_id
    session.messages.append(message)
    return message


def history(tmp_path: Path, sizes: list[int], *, consumed: bool = False) -> Session:
    session = Session(tmp_path, run_id="run", resume_run_id="run")
    add(session, Message("system", "rules"))
    add(session, Message("user", "original task"))
    for i, size in enumerate(sizes):
        ident = f"c{i}"
        add(session, reply(call("exec_command", {}, ident)).message)
        add(session, ToolResult(ident, True, {"stdout": "x" * size, "stderr": ""}).message())
    if consumed:
        add(session, reply(text="valid assistant").message)
    return session


def context(budget: int) -> Context:
    return Context(Limits(context_window=budget + 1524, max_output_tokens=500))


def active(session: Session) -> list[Message]:
    assert session.run_id is not None
    return ContextBuilder().build_active_context(session, session.run_id)


def recorder(session: Session) -> Recorder:
    result = Recorder()
    result.events = [("seed", {}) for _ in session.messages]
    return result


def test_hot_batch_and_working_set_priority(tmp_path: Path) -> None:
    session = history(tmp_path, [5000], consumed=True)
    add(session, reply(call("exec_command", {}, "a"), call("exec_command", {}, "b")).message)
    first = add(session, ToolResult("a", True, {"stdout": "a" * 6000}).message())
    second = add(session, ToolResult("b", False, {"stdout": "b" * 6000}, "failed").message())
    add(session, Message("user", "resume does not consume"))
    projected = context(4000).project(session, active(session))
    assert hot_tool_seqs(session, "run") == {first.seq, second.seq}
    assert projected.messages[-4] is first and projected.messages[-3] is second
    assert projected.diagnostics["hot_full_count"] == 2
    assert projected.diagnostics["recent_full_count"] == 0
    assert projected.diagnostics["cold_compacted_count"] == 1
    assert estimate([first, second], []) > projected.diagnostics["working_set_target_tokens"]
    groups(projected.messages)


def test_recent_is_contiguous_suffix_and_uses_message_estimator(tmp_path: Path) -> None:
    session = history(tmp_path, [10, 12000, 10], consumed=True)
    result = context(2000).project(session, active(session))
    observations = [m for m in result.messages if m.role == "tool"]
    assert result.diagnostics["working_set_target_tokens"] == 500
    assert result.diagnostics["recent_full_count"] == 1
    assert result.diagnostics["cold_count"] == 2  # Do not skip the huge middle observation.
    assert result.diagnostics["cold_kept_full_count"] == 1  # Tiny oldest result saves no bytes.
    assert observations[-1] is session.messages[-2]
    assert result.diagnostics["observation_full_estimated_tokens"] == sum(
        estimate([m], []) for m in session.messages if m.role == "tool"
    )
    assert (
        context(1_000_000)
        .project(session, active(session))
        .diagnostics["working_set_target_tokens"]
        == 16384
    )


@pytest.mark.parametrize(
    "mode",
    [
        "invalid",
        "duplicate",
        "duplicate_batch",
        "transport",
        "cancel",
        "wrong_role",
        "append_failure",
        "valid",
    ],
)
async def test_only_valid_appended_assistant_consumes(tmp_path: Path, mode: str) -> None:
    session = history(tmp_path, [4000])
    original = session.messages[-1]
    captured = recorder(session)

    async def emit(kind: str, data: Any, *, protocol_data: Any = None) -> int:
        if mode == "append_failure" and kind == "message" and data["role"] == "assistant":
            raise OSError("write failed")
        return await captured(kind, data, protocol_data=protocol_data)

    class Model:
        async def complete(
            self, messages: list[Message], tools: list[ToolSpec], emit: Emit
        ) -> ModelReply:
            assert messages[-3] is original  # Resume input and request suffix do not consume it.
            await emit("model_started", {})
            await emit("model_finished", {"usage": {}})
            if mode == "transport":
                raise ModelError("model_transport")
            if mode == "cancel":
                raise asyncio.CancelledError
            if mode == "invalid":
                return reply(text="invalid", finish="length")
            if mode == "wrong_role":
                return ModelReply(Message("user", "invalid"), "stop")
            if mode == "duplicate":
                return reply(call("exec_command", {}, "c0"))
            if mode == "duplicate_batch":
                return reply(call("exec_command", {}, "z"), call("exec_command", {}, "z"))
            return reply(text="valid")

    result = await run_turn(session, "resume", Model(), {}, emit, Limits())
    assert (original.seq not in hot_tool_seqs(session, "run")) == (mode == "valid"), result
    assert session.messages[3] is original
    assert any(k == "context_projection" for k, _ in captured.events)


@pytest.mark.parametrize("ok", [True, False])
def test_exec_compact_json_budget_utf8_and_both_streams(ok: bool) -> None:
    source = ToolResult(
        "id",
        ok,
        {
            "stdout": "OUT-start " + '中文🙂"\\\n' * 4000 + " OUT-error-end",
            "stderr": "ERR-start " + '错误🙂"\\\n' * 4000 + " ERR-error-end",
            "exit_code": 0 if ok else 1,
            "timed_out": not ok,
            "cancelled": False,
            "cleanup_incomplete": False,
        },
        None if ok else "command_failed",
        truncated=True,
    ).message()
    source.seq = 42
    before = deepcopy(source)
    projected = compact(source, "exec_command")
    assert projected is not source and source == before
    assert projected.content == compact(source, "exec_command").content
    encoded = projected.content.encode("utf-8")
    assert len(encoded) <= (1024 if ok else 2048) and len(encoded) < len(source.content.encode())
    data = json.loads(encoded)
    assert data["source_bytes"] == len(source.content.encode())
    assert data["source_seq"] == 42 and data["projection"] == "compact-v1"
    assert data["truncated"] is True and data["preview_omitted"] is True
    assert data["error_code"] == source_error(source)
    for key, end in (("stdout", "OUT-error-end"), ("stderr", "ERR-error-end")):
        assert end in data["data"][key]["tail"]
        if ok:
            assert "start" in data["data"][key]["head"]
    assert data["data"]["exit_code"] == (0 if ok else 1)
    assert data["data"]["timed_out"] == (not ok)


def source_error(message: Message) -> Any:
    return json.loads(message.content)["error_code"]


@pytest.mark.parametrize("ok", [True, False])
def test_patch_facts_without_diffs_and_with_omitted_count(ok: bool) -> None:
    files = [
        dict(
            path=f"file{i}.py",
            status="modified",
            added_lines=i,
            deleted_lines=1,
            before_hash="b" * 64,
            after_hash="a" * 64,
            truncated=False,
            diff="DIFF" * 4000,
        )
        for i in range(30)
    ]
    source = ToolResult(
        "id",
        ok,
        dict(
            files=files,
            changed_files=35,
            omitted_files=5,
            partial=not ok,
            no_changes=False,
            failed_file=None if ok else "f",
            detail="" if ok else "permission denied",
            created_directories=["directory"],
        ),
        None if ok else "patch_conflict",
    ).message()
    projected = compact(source, "apply_patch")
    data = json.loads(projected.content)["data"]
    assert len(projected.content.encode()) <= (1024 if ok else 2048)
    assert "DIFF" not in projected.content and '"diff"' not in projected.content
    assert data["changed_files"] == 35 and data["partial"] == (not ok)
    assert data["omitted_files"] == 35 - len(data["files"])
    assert data["files"] == [
        {k: v for k, v in f.items() if k != "diff"} for f in files[: len(data["files"])]
    ]
    assert data["files"] and "before_hash" in data["files"][0]
    if not ok:
        assert data["failed_file"] == "f" and data["detail"] == "permission denied"


@pytest.mark.parametrize("tool", ["mcp__server__tool", "future_tool"])
def test_generic_preview_and_no_recursive_projection(tmp_path: Path, tool: str) -> None:
    session = history(tmp_path, [20000], consumed=True)
    session.messages[2].tool_calls[0].name = tool
    source = session.messages[3]
    source.content = ToolResult("c0", True, {"nested": {"value": "数据" * 20000}}).message().content
    original = deepcopy(session.messages)
    first = context(1000).project(session, active(session))
    second = context(1000).project(session, first.messages)
    assert first.messages == second.messages and session.messages == original
    parsed = json.loads(first.messages[3].content)
    assert parsed["tool"] == tool and "preview" in parsed["data"]
    assert parsed["source_bytes"] == len(source.content.encode())
    assert compact(first.messages[3], tool) is first.messages[3]  # No raw source -> no recursion.


@pytest.mark.parametrize(
    "content",
    [
        "broken{",
        "[]",
        "null",
        '{"data":[]}',
        '{"ok":true,"data":{},"truncated":false,"call_id":"wrong"}',
    ],
)
def test_unparseable_results_are_full(content: str) -> None:
    message = Message("tool", content, tool_call_id="id")
    assert compact(message, "exec_command") is message


def test_tiny_result_keeps_full() -> None:
    message = ToolResult("id", True, {"stdout": "ok"}).message()
    assert compact(message, "exec_command") is message


async def test_valid_tool_call_append_consumes_previous_batch(tmp_path: Path) -> None:
    session = history(tmp_path, [4000])
    previous = session.messages[-1]
    result = await run_turn(
        session,
        "continue",
        ScriptedModel([reply(call("missing", {}, "next"))]),
        {},
        recorder(session),
        Limits(max_steps=1),
    )
    assert result.outcome == "limited"
    assert previous.seq not in hot_tool_seqs(session, "run")
    assert hot_tool_seqs(session, "run") == {session.messages[-1].seq}


async def test_snapshot_retains_full_cold_source_and_reprojects_it(tmp_path: Path) -> None:
    session = history(tmp_path, [15000, 15000, 15000], consumed=True)
    ctx = context(12000)
    projected = ctx.project(session, active(session))
    assert projected.diagnostics["cold_compacted_count"] == 3
    original = deepcopy(session.messages)
    # The last tool group remains safety-protected, but its consumed result is COLD.
    await ctx.prepare(
        session,
        projected.messages,
        [],
        ScriptedModel([reply(text="Summary facts")]),
        recorder(session),
        force=True,
    )
    logical = active(session)
    retained = next(m for m in logical if m.role == "tool")
    assert retained is session.messages[-2]
    assert '"projection"' not in retained.content
    again = ctx.project(session, logical)
    assert again.diagnostics["cold_compacted_count"] == 1
    assert next(m for m in again.messages if m.role == "tool") is not retained
    assert again.diagnostics["observation_full_bytes"] == len(retained.content.encode())
    assert session.messages == original


def test_empty_stream_and_escaped_utf8_budgets() -> None:
    for char in ("x", "🙂", "\x00", '"', "\\", "中文"):
        for ok in (False, True):
            for count in (1, 400, 4000):
                for empty in ("stdout", "stderr"):
                    data = {"stdout": char * count, "stderr": char * count, "exit_code": 1}
                    data[empty] = ""
                    source = ToolResult("id", ok, data).message()
                    result = compact(source, "exec_command")
                    parsed = json.loads(result.content)
                    if result is source:
                        continue
                    assert len(result.content.encode()) <= (1024 if ok else 2048)
                    assert len(result.content.encode()) < len(source.content.encode())
                    assert parsed["data"][empty] == {"head": "", "tail": ""}


async def ignore(event: RuntimeEvent) -> None:
    pass


async def test_projection_resume_persistence_and_summary_do_not_consume(tmp_path: Path) -> None:
    session = Session(tmp_path, run_id="run")
    with_session = SessionLog.create(session, "test", {}, tmp_path)
    events = Events(session, with_session, ignore)
    try:
        await events("run_started", {})
        seed = history(tmp_path, [20000, 20, 20])
        for message in seed.messages:
            await append_message(session, deepcopy(message), events)
        original = deepcopy(session.messages)
        ctx = context(12000)
        first = ctx.project(session, active(session))
        assert first.diagnostics["cold_compacted_count"] == 1
        await events("context_projection", first.diagnostics)
        # Forced safety summary consumes projected old history, without consuming the HOT tool.
        model = ScriptedModel([reply(text="Old observable results.")])
        hot = hot_tool_seqs(session, "run")
        await ctx.prepare(session, first.messages, [], model, events, force=True)
        summary_tools = [m for m in model.requests[0] if m.role == "tool"]
        assert len(summary_tools) == 1 and '"projection":"compact-v1"' in summary_tools[0].content
        assert hot_tool_seqs(session, "run") == hot
        logical = active(session)
        assert all('"projection":"compact-v1"' not in m.content for m in logical)
        assert session.messages == original
        projected = ctx.project(session, logical)
        assert projected.diagnostics["observation_full_bytes"] == sum(
            len(m.content.encode()) for m in logical if m.role == "tool"
        )
        records, _ = read_records(with_session.stream)
        restored = replay(records, tmp_path)
        assert restored.messages == original
        assert context(12000).project(restored, active(restored)) == projected
        raw_tools = [
            r["data"]["content"]
            for r in records
            if r["kind"] == "message" and r["data"]["role"] == "tool"
        ]
        assert raw_tools == [m.content for m in original if m.role == "tool"]
        assert all('"projection":"compact-v1"' not in value for value in raw_tools)
    finally:
        with_session.close()


async def test_hot_group_protected_even_before_many_resume_users(tmp_path: Path) -> None:
    session = history(tmp_path, [24000])
    for _ in range(4):
        add(session, Message("user", "continue"))
    protected = protected_seqs(session, active(session), "run")
    assert {session.messages[2].seq, session.messages[3].seq} <= protected
    ctx = context(3000)
    model = ScriptedModel([])
    original = deepcopy(session.messages)
    with pytest.raises(ModelError, match="context_limit"):
        await ctx.prepare(
            session, ctx.project(session, active(session)).messages, [], model, recorder(session)
        )
    assert not model.requests and session.messages == original


@pytest.mark.parametrize("fallback", [False, True])
async def test_final_context_rebuilt_projected_checked_observed_and_profiled(
    tmp_path: Path,
    monkeypatch: Any,
    fallback: bool,
) -> None:
    session = history(tmp_path, [16000, 20, 20, 20])
    # Narrative forces safety compaction even after COLD tools have been projected.
    session.messages[2].content = "earlier narrative " * 700
    ctx = context(9000)
    ctx.execution_budget = execution_budget(1, 40)
    ctx.progress = ProgressLedger.restore(session).prompt(session, 1, 40, [])
    limits = Limits(context_window=(9000 if fallback else 5500) + 1524, max_output_tokens=500)
    captured = recorder(session)
    original = deepcopy(session.messages)
    requests: list[list[Message]] = []
    checked: list[list[Message]] = []
    observed: list[list[Message]] = []
    original_check, original_observe = Context.check, Context.observe

    def check(self: Context, messages: list[Message], tools: list[ToolSpec]) -> None:
        checked.append(messages)
        original_check(self, messages, tools)

    def observe(
        self: Context, response: ModelReply, messages: list[Message], tools: list[ToolSpec]
    ) -> None:
        observed.append(messages)
        original_observe(self, response, messages, tools)

    monkeypatch.setattr(Context, "check", check)
    monkeypatch.setattr(Context, "observe", observe)

    class Provider:
        async def complete(
            self, messages: list[Message], tools: list[ToolSpec], emit: Emit
        ) -> ModelReply:
            if any(m.content.startswith("Summarize the earlier") for m in messages):
                assert any('"projection":"compact-v1"' in m.content for m in messages)
                return reply(text="Prior inspection facts.")
            assert any(messages is item for item in checked)
            requests.append(messages)
            if fallback and len(requests) == 1:
                raise ModelError("context_limit")
            assert any(m.content.startswith("[Historical") for m in messages)
            assert messages == ctx.task_request(ctx.project(session, active(session)).messages)
            return reply(text="done")

    result = await run_turn(session, "continue", Provider(), {}, captured, limits)
    assert result.outcome == "completed", result
    assert len(requests) == (2 if fallback else 1)
    assert observed[-1] is requests[-1]
    assert session.messages[: len(original)] == original
    events = [data for kind, data in captured.events if kind == "context_projection"]
    assert len(events) == len(requests)
    assert all(
        '"projection":"compact-v1"' not in m.content for m in session.compactions["run"].messages
    )
    profiler = RunProfiler()
    profiler.consume(RuntimeEvent("run_started", "", "s", "run", {}))
    for event in events:
        profiler.consume(RuntimeEvent("context_projection", "", "s", "run", event))
    metrics = profile_metrics(profiler.profile)
    assert metrics["observation_projection"]["requests"] == len(requests)  # type: ignore[index]
    assert metrics["usage"]["input_tokens"]["reported"] is None  # type: ignore[index]
    assert provenance()["context_policy"] == "Context Runtime V0.2.1"


async def test_long_run_reduces_repeated_tool_inputs_without_losing_raw_history(
    tmp_path: Path,
) -> None:
    session = Session(tmp_path)
    responses = [reply(call("exec_command", {}, f"c{i}")) for i in range(12)] + [reply(text="done")]
    for response in responses:
        response.usage = Usage()  # Unknown provider usage must stay unknown.
    model = ScriptedModel(responses)
    captured = Recorder()

    async def execute(arguments: Any, execution: Any, emit: Emit) -> ToolResult:
        return ToolResult(execution.call_id, True, {"stdout": "observation " * 2600, "stderr": ""})

    registry = {"exec_command": Tool(ToolSpec("exec_command", "command", {}), execute)}
    result = await run_turn(
        session, "task", model, registry, captured, Limits(context_window=128000, max_steps=20)
    )
    assert result.outcome == "completed"
    diagnostics = [d for k, d in captured.events if k == "context_projection"]
    assert len(diagnostics) == 13 and diagnostics[-1]["hot_full_count"] == 1
    assert diagnostics[-1]["cold_compacted_count"] == 11
    assert (
        diagnostics[-1]["observation_projected_bytes"]
        < diagnostics[-1]["observation_full_bytes"] / 4
    )
    assert all(len(m.content) > 31000 for m in session.messages if m.role == "tool")
    assert result.usage.input_tokens is None
    assert not session.compactions
