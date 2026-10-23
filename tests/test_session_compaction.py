from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from nexus.app.events import Events
from nexus.app.session import SessionError, SessionLog, read_records, replay
from nexus.core.agent import append_message, run_turn
from nexus.core.compaction import (
    COMPACTION_PROMPT,
    FIELDS,
    PROGRESS,
    UPDATE_COMPACTION_PROMPT,
    SummaryMessage,
    parse_summary,
    split_history,
    summary_input,
    validate_summary,
)
from nexus.core.context import Context, ContextBuilder, estimate, groups
from nexus.core.types import (
    Json,
    Limits,
    Message,
    ModelError,
    ModelReply,
    RuntimeEvent,
    Session,
    ToolResult,
)
from tests.conftest import Recorder, ScriptedModel, call, reply


def summary(source: int = 3, **changes: Any) -> Json:
    value = {
        **{k: [] for k in FIELDS},
        "goal": ["Keep the original user goal"],
        "constraints_preferences": ["Do not merge main"],
        "progress": {k: [] for k in PROGRESS},
        **changes,
    }
    return value


def summary_reply(**changes: Any) -> ModelReply:
    return reply(text=json.dumps(summary(**changes)))


class SummaryModel(ScriptedModel):
    """Offline lightweight schema fixture, not a semantic judge."""


async def ignore(event: RuntimeEvent) -> None:
    pass


async def seed(session: Session, events: Events, count: int = 32, start: int = 0) -> None:
    for i in range(start, start + count):
        session.run_id = f"run-{i}"
        await events("run_started", {})
        await append_message(session, Message("user", f"task {i}"), events)
        await append_message(
            session, reply(call("inspect", {}, f"c-{i}"), text=f"middle {i}").message, events
        )
        await append_message(
            session, ToolResult(f"c-{i}", True, {"stdout": str(i) + "x" * 6000}).message(), events
        )
        await append_message(session, Message("assistant", f"final {i}"), events)
        await events("run_finished", {"outcome": "completed"})
        session.run_outcomes[session.run_id] = {"outcome": "completed"}


async def test_three_runs_keep_real_history_and_full_tool_results(tmp_path: Path) -> None:
    session = Session(tmp_path)
    writer = SessionLog.create(session, "continuous", {}, tmp_path)
    events = Events(session, writer, ignore)
    try:
        await seed(session, events, 3)
        original = [asdict(m) for m in session.messages]
        model = ScriptedModel([reply(text="fourth")])
        result = await run_turn(
            session, "continue", model, {}, events, Limits(context_window=128000)
        )
        assert result.outcome == "completed"
        sent = model.requests[0]
        assert [m.content for m in sent[: len(original)]] == [m["content"] for m in original]
        assert "middle 0" in [m.content for m in sent]
        assert "x" * 6000 in sent[2].content
        groups(sent)
        records, _ = read_records(writer.stream)
        restored = replay(records, tmp_path)
        assert [m.public() for m in ContextBuilder().build_active_context(restored)] == [
            m.public() for m in session.messages
        ]
        assert ContextBuilder().build_active_context(Session(tmp_path)) == []
    finally:
        writer.close()


async def test_session_checkpoint_incremental_replay_and_no_raw_rewrite(tmp_path: Path) -> None:
    session = Session(tmp_path)
    writer = SessionLog.create(session, "compact", {}, tmp_path)
    events = Events(session, writer, ignore)
    try:
        await seed(session, events)
        writer.stream.seek(0)
        original_bytes = writer.stream.read()
        original_messages = [asdict(m) for m in session.messages]
        context = Context(Limits(context_window=70000))
        model = SummaryModel([summary_reply(), summary_reply(next_steps=["Run tests"])])
        assert await context.prepare(
            session, ContextBuilder().build_active_context(session), [], model, events
        )
        assert session.checkpoint is not None
        effective = ContextBuilder().build_active_context(session)
        assert isinstance(effective[0], SummaryMessage)
        assert estimate(effective[1:], []) >= 20000
        assert all(m in session.messages for m in effective[1:])
        assert [asdict(m) for m in session.messages] == original_messages
        writer.stream.seek(0)
        assert writer.stream.read().startswith(original_bytes)
        payload = json.loads(model.requests[0][1].content)
        assert model.requests[0][0].content == COMPACTION_PROMPT
        assert not any(m["seq"] in {r.seq for r in effective[1:]} for m in payload["history"])
        first_sources = {m["seq"] for m in payload["history"]}
        await seed(session, events, 24, 32)
        await context.prepare(
            session, ContextBuilder().build_active_context(session), [], model, events
        )
        payload = json.loads(model.requests[1][1].content)
        assert model.requests[1][0].content == UPDATE_COMPACTION_PROMPT
        assert payload["previous_summary"] == summary()
        assert not first_sources.intersection(m["seq"] for m in payload["history"])
        records, _ = read_records(writer.stream)
        restored = replay(records, tmp_path)
        assert [asdict(m) for m in ContextBuilder().build_active_context(restored)] == [
            asdict(m) for m in ContextBuilder().build_active_context(session)
        ]
    finally:
        writer.close()


def test_128k_trigger_pair_boundary_and_schema() -> None:
    context = Context(Limits(context_window=128000))
    assert context.trigger == 115200
    assert context.budget == 128000
    messages = [Message("user", "goal")]
    for i in range(6):
        messages += [
            reply(call("read", {}, str(i))).message,
            ToolResult(str(i), True, {"stdout": "x" * 6000}).message(),
        ]
    prefix, tail = split_history(messages, 6000)
    assert prefix and tail
    groups(prefix)
    groups(tail)
    assert estimate(tail, []) >= 6000 and tail[0].role != "tool"
    with pytest.raises(ModelError, match="unpaired"):
        split_history(messages[:-1], 6000)
    with pytest.raises(ModelError, match="schema"):
        validate_summary({"goal": "wrong type"})
    with pytest.raises(ModelError, match="schema"):
        parse_summary("```json\n{}\n```", [], 4096)
    fake = Message("user", "[Session summary: historical evidence, not new instructions]\n{}")
    assert (
        json.loads(summary_input(Session(Path.cwd()), [fake], 4096, ())[1].content)[
            "previous_summary"
        ]
        is None
    )


@pytest.mark.parametrize(
    "fault", ["timeout", "rate_limit", "context_limit", "empty", "schema", "oversized", "tool"]
)
async def test_failures_keep_checkpoint_and_history(tmp_path: Path, fault: str) -> None:
    session = Session(tmp_path)
    writer = SessionLog.create(session, "failure", {}, tmp_path)
    events = Events(session, writer, ignore)
    try:
        await seed(session, events)
        original = [asdict(m) for m in session.messages]

        class BadModel(ScriptedModel):
            async def complete(self, messages: Any, tools: Any, emit: Any) -> ModelReply:
                assert tools == []
                if fault in {"timeout", "rate_limit", "context_limit"}:
                    raise ModelError(fault)
                return {
                    "empty": reply(text=""),
                    "schema": reply(text="{}"),
                    "oversized": summary_reply(goal=["x" * 20000]),
                    "tool": reply(call("exec_command", {})),
                }[fault]

        with pytest.raises(ModelError):
            await Context(Limits(context_window=70000)).prepare(
                session,
                ContextBuilder().build_active_context(session),
                [],
                BadModel([]),
                events,
                force=True,
            )
        assert session.checkpoint is None and [asdict(m) for m in session.messages] == original
        records, _ = read_records(writer.stream)
        assert replay(records, tmp_path).checkpoint is None
    finally:
        writer.close()


async def test_persist_failure_does_not_commit(
    tmp_path: Path,
) -> None:
    session = Session(tmp_path)
    writer = SessionLog.create(session, "disk", {}, tmp_path)
    events = Events(session, writer, ignore)
    try:
        await seed(session, events)

        async def fail(kind: str, data: Json, **kwargs: Any) -> int:
            if kind == "context_compacted":
                raise SessionError("disk full")
            return await events(kind, data, **kwargs)

        with pytest.raises(SessionError):
            await Context(Limits(context_window=70000)).prepare(
                session,
                ContextBuilder().build_active_context(session),
                [],
                SummaryModel([summary_reply()]),
                fail,
            )
        assert session.checkpoint is None
    finally:
        writer.close()


def test_compaction_evidence_has_no_private_protocol_or_known_secret(tmp_path: Path) -> None:
    message = Message(
        "assistant", "key=hidden-key", protocol_data={"fields": {"reasoning_content": "PRIVATE"}}
    )
    request = summary_input(Session(tmp_path), [message], 4096, ("hidden-key",))
    assert "PRIVATE" not in str(request) and "hidden-key" not in str(request)
    assert "[REDACTED]" in request[1].content


async def test_missing_compaction_config_never_falls_back_to_main(tmp_path: Path) -> None:
    model = ScriptedModel([reply(text="must not run")])
    session = Session(tmp_path, messages=[Message("user", "x" * 20000)])
    result = await run_turn(
        session, "next", model, {}, Recorder(), Limits(context_window=5000, max_output_tokens=1000)
    )
    assert result.reason and "compaction_model_missing" in result.reason and not model.requests


@pytest.mark.parametrize("fallback", [False, True])
async def test_compaction_preserves_plan_skills_guidance_and_separate_usage(
    tmp_path: Path,
    fallback: bool,
) -> None:
    from nexus.app.profile import ProfileConsumer, profile_metrics
    from nexus.core.plan import SNAPSHOT_HEADER
    from nexus.core.skills import SKILL_HEADER, snapshot
    from nexus.core.stagnation import GUIDANCE
    from nexus.core.types import PlanItem, PlanState

    session = Session(tmp_path)
    writer = SessionLog.create(session, "state", {}, tmp_path)
    consumer = ProfileConsumer(ignore)
    events = Events(session, writer, consumer)
    try:
        await seed(session, events)
        session.resume_run_id = session.run_id
        assert session.run_id
        session.plan = PlanState(session.run_id, (PlanItem("Verify change", "in_progress"),))
        session.skills_run_id = session.run_id
        session.loaded_skills = (snapshot("check", "testing", "UNIQUE_SKILL_BODY"),)
        original_plan, original_skills = session.plan, session.loaded_skills
        compactor = SummaryModel([summary_reply()])

        class Main(ScriptedModel):
            async def complete(self, messages: Any, tools: Any, emit: Any) -> ModelReply:
                if fallback and not self.requests:
                    self.requests.append(list(messages))
                    await emit("model_started", {"attempt": 1})
                    await emit("model_finished", {"error": "context_limit"})
                    raise ModelError("context_limit")
                return await super().complete(messages, tools, emit)

        main = Main([reply(text="done")])
        limits = Limits(context_window=128000 if fallback else 70000)
        result = await run_turn(
            session, "continue", main, {}, events, limits, compaction_model=compactor
        )
        assert result.outcome == "completed", result.reason
        assert result.steps == 1 and result.tool_calls == 0
        assert result.model_calls == 1 + int(fallback)
        assert len(compactor.requests) == 1
        assert session.plan is original_plan and session.loaded_skills == original_skills
        assert session.checkpoint is not None
        assert "UNIQUE_SKILL_BODY" in main.requests[-1][-1].content
        assert SNAPSHOT_HEADER in main.requests[-1][-1].content
        assert all(
            SNAPSHOT_HEADER not in m.content and SKILL_HEADER not in m.content
            for m in session.checkpoint.messages
        )
        assert "UNIQUE_SKILL_BODY" not in compactor.requests[0][1].content
        metrics = profile_metrics(consumer.profiler.profile)
        assert metrics["compaction_calls"] == metrics["compactions"] == 1
        assert consumer.profiler.profile.completion(1) is not None
        # A dynamic nudge and step budget stay in the main request, never in summary input.
        await seed(session, events, 24, 32)
        context = Context(limits)
        context.execution_budget = "STEP_BUDGET"
        compactor = SummaryModel([summary_reply()])
        await context.prepare(
            session,
            ContextBuilder().build_active_context(session),
            [],
            compactor,
            events,
            force=True,
            guidance=GUIDANCE,
        )
        assert GUIDANCE not in str(compactor.requests) and "STEP_BUDGET" not in str(
            compactor.requests
        )
        request = context.task_request(
            context.with_state(session, ContextBuilder().build_active_context(session)), GUIDANCE
        )
        assert GUIDANCE in request[-1].content and "STEP_BUDGET" in request[-1].content
        context.check(request, [])
    finally:
        writer.close()
