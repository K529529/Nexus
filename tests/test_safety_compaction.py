from __future__ import annotations

import json
from collections.abc import AsyncIterator
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from nexus.app.events import Events
from nexus.app.session import SessionError, SessionLog, read_records, replay, resume_session
from nexus.core.agent import append_message, run_turn
from nexus.core.context import (
    Context,
    ContextBuilder,
    estimate,
    execution_budget,
    groups,
    project_guidance,
)
from nexus.core.plan import project_plan
from nexus.core.progress import ProgressLedger
from nexus.core.types import (
    Emit,
    Limits,
    Message,
    ModelError,
    ModelReply,
    RuntimeEvent,
    Session,
    ToolResult,
    ToolSpec,
    Usage,
)
from tests.conftest import ScriptedModel, call, reply

History = tuple[Session, SessionLog, Events]


async def ignore(event: RuntimeEvent) -> None:
    pass


async def add_tools(session: Session, events: Events, name: str, repetitions: int) -> None:
    assistant = reply(call("exec_command", {}, name)).message
    # Exercise safety compaction with narrative that observation projection cannot shrink.
    assistant.content = f"{name} observation " * repetitions if repetitions > 1 else ""
    assistant.protocol_data = {"continuation": name}
    await append_message(session, assistant, events)
    await append_message(
        session,
        ToolResult(name, True, {"stdout": f"{name} observation "}).message(),
        events,
    )


@pytest.fixture
async def history(tmp_path: Path) -> AsyncIterator[History]:
    session = Session(tmp_path, run_id="run1")
    writer = SessionLog.create(session, "run1", {}, tmp_path)
    events = Events(session, writer, ignore)
    try:
        await append_message(session, Message("system", "repository rules"), events)
        await events("run_started", {"workspace": str(tmp_path)})
        await append_message(session, Message("user", "RUN1_SECRET_TASK"), events)
        await append_message(session, Message("assistant", "RUN1_RESULT"), events)
        await events("run_finished", {"outcome": "completed"})
        session.run_id = "run2"
        await events("run_started", {"workspace": str(tmp_path)})
        await append_message(session, Message("user", "Original run2 goal; keep verbatim"), events)
        await add_tools(session, events, "old", 500)
        await add_tools(session, events, "recent1", 1)
        await add_tools(session, events, "recent2", 1)
        yield session, writer, events
    finally:
        writer.close()


def active(session: Session) -> list[Message]:
    return ContextBuilder().build_active_context(session, "run2")


def near_limit(session: Session) -> Limits:
    return Limits(
        context_window=int(estimate(active(session), []) / 0.90) + 500 + 1024, max_output_tokens=500
    )


async def compact(session: Session, events: Events) -> ScriptedModel:
    model = ScriptedModel(
        [reply(text="Inspected code; tests passed; original task remains unfinished.")]
    )
    context = Context(near_limit(session))
    assert await context.prepare(session, active(session), [], model, events)
    assert estimate(active(session), []) <= context.budget * 0.60
    return model


async def test_threshold_compaction_is_non_destructive_and_run_scoped(history: History) -> None:
    session, writer, events = history
    original = deepcopy(session.messages)
    writer.stream.seek(0)
    before = writer.stream.read()
    model = await compact(session, events)
    projected = active(session)
    assert session.messages == original
    writer.stream.seek(0)
    assert writer.stream.read().startswith(before)
    assert len(model.requests) == 1
    assert all("RUN1_" not in m.content for m in model.requests[0] + projected)
    assert [c.id for m in model.requests[0] for c in m.tool_calls] == ["old"]
    assert [c.id for m in projected for c in m.tool_calls] == ["recent1", "recent2"]
    assert any(m.content == "Original run2 goal; keep verbatim" for m in projected)
    assert projected[0].content == "repository rules"
    for message in projected:
        if message.role == "assistant":
            assert message is next(m for m in session.messages if m.seq == message.seq)
            assert message.protocol_data == {"continuation": message.tool_calls[0].id}
    assert model.requests[0][2].protocol_data == {"continuation": "old"}
    groups(projected)
    records, _ = read_records(writer.stream)
    restored = replay(records, session.workspace)
    assert active(restored) == projected
    assert restored.messages == session.messages
    assert len([r for r in records if r["kind"] == "message"]) == len(original)
    assert sum(r["kind"] == "context_compacted" for r in records) == 1

    # A later request sees new messages without resurrecting the replaced originals.
    await add_tools(session, events, "later", 1)
    assert [c.id for m in active(session) for c in m.tool_calls] == ["recent1", "recent2", "later"]
    new = ScriptedModel([reply(text="New independent answer")])
    result = await run_turn(session, "New independent task", new, {}, events, Limits())
    assert result.outcome == "completed"
    assert (
        new.requests[0][0]
        == project_guidance(project_plan(session, [session.messages[0]]), execution_budget(1, 40))[
            0
        ]
    )
    assert [m.content for m in new.requests[0][1:-1]] == ["New independent task"]
    assert session.messages[: len(original)] == original


async def test_summary_input_preserves_interleaved_resume_message_order(history: History) -> None:
    session, _, events = history
    resume_request = Message("user", "Continue, but inspect the new failure first")
    await append_message(session, resume_request, events)
    await add_tools(session, events, "after_resume", 1)
    await add_tools(session, events, "latest1", 1)
    await add_tools(session, events, "latest2", 1)
    before = active(session)
    original_history = deepcopy(session.messages)

    model = await compact(session, events)

    # Only the last two complete tool groups are excluded from summary input.
    # The resume request must remain between the earlier and later tool history.
    summary_history = model.requests[0][:-2]
    assert summary_history == before[:-4]
    assert summary_history.index(resume_request) == before.index(resume_request)
    assert summary_history[-2].tool_calls[0].id == "after_resume"
    assert model.requests[0][-2].content.startswith("Summarize the earlier conversation")
    groups(model.requests[0])
    assert session.messages == original_history
    assert resume_request in active(session)


@pytest.mark.parametrize("input_tokens,attempted", [(8499, False), (8500, True)])
async def test_exact_threshold_uses_reported_input_not_cumulative_usage(
    history: History, input_tokens: int, attempted: bool
) -> None:
    session, _, events = history
    context = Context(Limits(context_window=11524, max_output_tokens=500))
    messages = context.project(session, active(session)).messages
    context.tokens(messages, [])  # Establish the schema/prefix before calibration.
    response = reply(text="earlier reply")
    response.usage = Usage(input_tokens, 20, 1_000_000, "reported")
    context.observe(response, messages, [])
    model = ScriptedModel([reply(text="Observable inspection facts.")])
    assert await context.prepare(session, messages, [], model, events) is attempted
    assert len(model.requests) == int(attempted)
    if attempted:
        assert context.last_estimate is None and context.last_report is None


@pytest.mark.parametrize("truncated", [False, True])
async def test_repeated_compaction_and_interrupted_resume(
    history: History, truncated: bool
) -> None:
    session, writer, events = history
    await compact(session, events)
    first_summary_seq = session.compactions["run2"].through_seq
    await add_tools(session, events, "next_old", 500)
    await add_tools(session, events, "next_recent1", 1)
    await add_tools(session, events, "next_recent2", 1)
    await compact(session, events)
    expected = active(session)
    records, _ = read_records(writer.stream)
    last = next(r for r in reversed(records) if r["kind"] == "context_compacted")
    assert first_summary_seq in last["data"]["replaced_seqs"]
    assert len([m for m in expected if m.content.startswith("[Historical")]) == 1
    await append_message(session, reply(call("exec_command", {}, "unfinished")).message, events)
    path = writer.path
    writer.close()
    if truncated:
        with path.open("ab") as stream:
            stream.write(b'{"incomplete":')
    original = path.read_bytes()
    restored, recovered_writer, _ = resume_session(path, session.workspace, session.workspace)
    try:
        recovered = active(restored)
        assert recovered[: len(expected)] == expected
        assert json.loads(recovered[-1].content)["error_code"] == "interrupted_unknown"
        assert restored.resume_run_id == "run2"
        groups(recovered)
        if truncated:
            assert path.read_bytes() == original
        model = ScriptedModel(
            [
                reply(text="Finished resumed work"),
                reply(text="Unknown prior side effects remain unverified."),
            ]
        )
        result = await run_turn(
            restored, "Continue", model, {}, Events(restored, recovered_writer, ignore), Limits()
        )
        assert result.outcome == "completed" and result.tool_calls == 0
        assert model.requests[0] == project_guidance(
            project_plan(restored, recovered + [restored.messages[-3]]),
            execution_budget(1, 40)
            + "\n\n"
            + ProgressLedger.restore(restored).prompt(restored, 1, 40, []),
        )
        assert "old observation " * 500 not in str([m.public() for m in model.requests[0]])
        records, _ = read_records(recovered_writer.stream)
        assert active(replay(records, session.workspace)) == active(restored)
    finally:
        recovered_writer.close()


@pytest.mark.parametrize("preemptive", [False, True])
@pytest.mark.parametrize("still_too_large", [False, True])
async def test_provider_limit_compacts_at_most_once_per_boundary(
    history: History, preemptive: bool, still_too_large: bool
) -> None:
    session, writer, events = history
    session.resume_run_id = "run2"
    seen: list[RuntimeEvent] = []

    async def consume(event: RuntimeEvent) -> None:
        seen.append(event)

    class Provider:
        regular = 0
        summaries = 0

        async def complete(
            self, messages: list[Message], tools: list[ToolSpec], emit: Emit
        ) -> ModelReply:
            await emit("model_started", {})
            if any(m.content.startswith("Summarize the earlier") for m in messages):
                self.summaries += 1
                assert not tools
                await emit("assistant_delta", {"text": "SUMMARY_MUST_NOT_STREAM"})
                response = reply(text="Observable facts; continue the original task.")
            else:
                self.regular += 1
                if still_too_large or (not preemptive and self.regular == 1):
                    raise ModelError("context_limit")
                assert any(m.content.startswith("[Historical") for m in messages)
                response = reply(text="done")
            await emit(
                "model_finished",
                {
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 5,
                        "total_tokens": 15,
                        "source": "reported",
                    }
                },
            )
            return response

    provider = Provider()
    result = await run_turn(
        session,
        "Continue",
        provider,
        {},
        Events(session, writer, consume),
        near_limit(session) if preemptive else Limits(),
    )
    assert result.outcome == ("limited" if still_too_large else "completed")
    assert provider.summaries == 1 and provider.regular == (1 if preemptive else 2)
    assert result.model_calls == provider.summaries + provider.regular
    assert not any(e.kind == "assistant_delta" for e in seen)
    assert any(e.kind == "model_started" and e.data.get("purpose") == "compaction" for e in seen)
    if preemptive and not still_too_large:
        assert result.usage.total_tokens == 30  # Include the summary request in billing usage.


@pytest.mark.parametrize("force", [False, True])
async def test_failed_summary_keeps_original_context(history: History, force: bool) -> None:
    session, writer, events = history
    before = deepcopy(session.messages)
    context = Context(near_limit(session))
    model = ScriptedModel([reply(text="")])
    if force:
        with pytest.raises(ModelError, match="context_limit"):
            await context.prepare(session, active(session), [], model, events, force=True)
    else:
        assert await context.prepare(session, active(session), [], model, events)
    assert session.messages == before and not session.compactions
    assert len(model.requests) == 1
    records, _ = read_records(writer.stream)
    assert not any(r["kind"] == "context_compacted" for r in records)


async def test_invalid_projection_reference_is_rejected(history: History) -> None:
    session, writer, events = history
    await compact(session, events)
    records, _ = read_records(writer.stream)
    record = next(r for r in records if r["kind"] == "context_compacted")
    record["data"]["replaced_seqs"][0] = next(
        m.seq for m in session.messages if m.run_id == "run1" and m.role == "user"
    )
    with pytest.raises(SessionError, match="Invalid message/run record"):
        replay(records, session.workspace)


async def test_compaction_write_failure_never_uses_unsaved_summary(
    history: History, monkeypatch: Any
) -> None:
    session, writer, events = history
    before = deepcopy(session.messages)
    session.resume_run_id = "run2"
    original_append = writer.append

    def fail(event: RuntimeEvent, protocol_data: Any = None) -> int:
        if event.kind == "context_compacted":
            writer.failed = True
            raise SessionError("simulated compaction flush failure")
        return original_append(event, protocol_data)

    monkeypatch.setattr(writer, "append", fail)
    model = ScriptedModel([reply(text="summary only"), reply(text="must not reach model")])
    result = await run_turn(session, "Continue", model, {}, events, near_limit(session))
    assert result.outcome == "failed" and len(model.requests) == 1
    assert not session.compactions and session.messages[: len(before)] == before


async def test_summary_cannot_split_tool_group(history: History) -> None:
    session, writer, events = history
    await compact(session, events)
    records, _ = read_records(writer.stream)
    record = next(r for r in records if r["kind"] == "context_compacted")
    # Corrupt a snapshot so it replaces only the result while retaining its tool call.
    assistant_seq = record["data"]["replaced_seqs"].pop(0)
    record["data"]["kept_seqs"].insert(2, assistant_seq)
    with pytest.raises(SessionError):
        replay(records, session.workspace)
