from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from nexus.app import bootstrap
from nexus.app.config import Config, ModelConfig
from nexus.app.events import Events
from nexus.app.session import SessionError, SessionLog, read_records, replay, resume_session
from nexus.core.context import Context, ContextBuilder, estimate
from nexus.core.types import Limits, Message, ModelReply, Session, Usage
from tests.conftest import ScriptedModel, reply
from tests.test_session_compaction import SummaryModel, ignore, seed, summary_reply


@pytest.mark.parametrize("automatic", [False, True])
async def test_idle_compaction_never_starts_a_coding_task(
    tmp_path: Path,
    monkeypatch: Any,
    automatic: bool,
) -> None:
    monkeypatch.setenv("MAIN_TEST", "main-test-secret")
    monkeypatch.setenv("SUMMARY_TEST", "summary-test-secret")
    config = Config(
        ModelConfig("main", 70000, api_key_env="MAIN_TEST"),
        Limits(context_window=70000),
        compaction_model=ModelConfig(
            "summary", 128000, api_key_env="SUMMARY_TEST", max_output_tokens=4096
        ),
    )
    clients: list[Any] = []

    class SummaryClient(SummaryModel):
        def __init__(self, config: Any) -> None:
            super().__init__([summary_reply()])
            self.closed = False
            clients.append(self)

        async def close(self) -> None:
            self.closed = True

    class MainClient(ScriptedModel):
        def __init__(self, config: Any) -> None:
            response = reply(text="f" * 140000)
            response.usage = Usage()
            super().__init__([response])

        async def close(self) -> None:
            pass

    monkeypatch.setattr(bootstrap, "CompactionModel", SummaryClient)
    monkeypatch.setattr(bootstrap, "ChatModel", MainClient)
    convo = bootstrap.Conversation(tmp_path, config, ignore, home=tmp_path, registry={})
    try:
        convo.ensure_events("history")
        assert convo.events and convo.writer
        await seed(convo.session, convo.events, 10 if automatic else 32)
        previous_run = convo.session.run_id
        before_seq = convo.writer.seq
        if automatic:
            result = await convo.turn("finish")
            assert result.steps == result.model_calls == 1 and result.tool_calls == 0
            assert result.outcome == "completed"
        else:
            assert await convo.compact()
            assert convo.model is None and convo.session.run_id == previous_run
        assert convo.session.checkpoint is not None and len(clients) == 1
        records, _ = read_records(convo.writer.stream)
        new = [r for r in records if r["seq"] > before_seq]
        assert sum(r["kind"] == "run_started" for r in new) == int(automatic)
        assert not any(r["kind"] == "tool_started" for r in new)
        checkpoint = next(r for r in new if r["kind"] == "context_compacted")
        assert checkpoint["run_id"] is None
        started = next(r for r in new if r["kind"] == "compaction_started")
        assert started["data"]["reason"] == ("between_runs" if automatic else "manual")
    finally:
        await convo.close()
    assert clients[0].closed


async def test_legacy_run_checkpoint_then_new_checkpoint_never_resurrects_history(
    tmp_path: Path,
) -> None:
    session = Session(tmp_path)
    writer = SessionLog.create(session, "legacy", {}, tmp_path)
    events = Events(session, writer, ignore)
    try:
        await seed(session, events, 2)
        run = "run-1"
        messages = [m for m in session.messages if m.run_id == run]
        removed = [m.seq for m in messages[1:3]]
        data = {
            "scope": "run",
            "summary": "LEGACY_SUMMARY: inspection happened; no tests.",
            "summary_index": 1,
            "kept_seqs": [messages[0].seq, messages[-1].seq],
            "replaced_seqs": removed,
        }
        seq = await events("context_compacted", data)
        ContextBuilder().restore_compaction(session, run, data, seq)
        records, _ = read_records(writer.stream)
        restored = replay(records, tmp_path)
        active = ContextBuilder().build_active_context(restored)
        assert all(m.seq not in removed for m in active)
        assert any(m.content == "middle 0" for m in active)
        assert sum("LEGACY_SUMMARY" in m.content for m in active) == 1
        session = restored
        events = Events(session, writer, ignore)
        await seed(session, events, 32, 2)
        model = SummaryModel([summary_reply()])
        await Context(Limits(context_window=70000)).prepare(
            session,
            ContextBuilder().build_active_context(session),
            [],
            model,
            events,
        )
        assert str(model.requests).count("LEGACY_SUMMARY") == 1
        records, _ = read_records(writer.stream)
        replayed = replay(records, tmp_path)
        assert [asdict(m) for m in ContextBuilder().build_active_context(replayed)] == [
            asdict(m) for m in ContextBuilder().build_active_context(session)
        ]
    finally:
        writer.close()


@pytest.mark.parametrize("fault", ["split", "duplicate", "version", "schema", "order"])
async def test_corrupt_checkpoint_is_rejected(tmp_path: Path, fault: str) -> None:
    session = Session(tmp_path)
    writer = SessionLog.create(session, "corrupt", {}, tmp_path)
    events = Events(session, writer, ignore)
    try:
        await seed(session, events)
        await Context(Limits(context_window=70000)).prepare(
            session,
            ContextBuilder().build_active_context(session),
            [],
            SummaryModel([summary_reply()]),
            events,
        )
        records, _ = read_records(writer.stream)
        bad = deepcopy(records)
        data = next(r["data"] for r in bad if r["kind"] == "context_compacted")
        if fault == "duplicate":
            data["kept_seqs"].append(data["kept_seqs"][-1])
        elif fault == "version":
            data["version"] = 99
        elif fault == "schema":
            data["summary"]["goal"] = "not an array"
        elif fault == "order":
            data["kept_seqs"].reverse()
        else:
            all_seqs = data["replaced_seqs"] + data["kept_seqs"]
            split = next(i for i, m in enumerate(session.messages) if m.role == "tool")
            data["replaced_seqs"], data["kept_seqs"] = all_seqs[:split], all_seqs[split:]
        with pytest.raises(SessionError):
            replay(bad, tmp_path)
    finally:
        writer.close()


async def test_cancelled_compaction_and_truncated_tail_recover_prior_checkpoint(
    tmp_path: Path,
) -> None:
    session = Session(tmp_path)
    writer = SessionLog.create(session, "cancel", {}, tmp_path)
    events = Events(session, writer, ignore)
    await seed(session, events)
    context = Context(Limits(context_window=70000))
    await context.prepare(
        session,
        ContextBuilder().build_active_context(session),
        [],
        SummaryModel([summary_reply()]),
        events,
    )
    checkpoint = deepcopy(session.checkpoint)
    await seed(session, events, 24, 32)

    class Cancel(ScriptedModel):
        async def complete(self, messages: Any, tools: Any, emit: Any) -> ModelReply:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await context.prepare(
            session, ContextBuilder().build_active_context(session), [], Cancel([]), events
        )
    assert session.checkpoint == checkpoint
    expected = [asdict(m) for m in ContextBuilder().build_active_context(session)]
    path = writer.path
    writer.close()
    with path.open("ab") as stream:
        stream.write(b'{"incomplete":')
    original = path.read_bytes()
    recovered, new_writer, warnings = resume_session(path, tmp_path, tmp_path)
    try:
        assert warnings and new_writer.path != path and path.read_bytes() == original
        assert [asdict(m) for m in ContextBuilder().build_active_context(recovered)] == expected
    finally:
        new_writer.close()


def test_estimate_calibration_reset_and_usage_missing() -> None:
    context = Context(Limits(context_window=128000))
    messages = [Message("user", "task")]
    response = reply(text="done")
    context.observe(response, messages, [])
    # Tiny receipts cannot impose a content-independent ratio on future long inputs.
    assert context.tokens(messages, []) == estimate(messages, [])
    shaped = messages + [Message("assistant", "done")]
    assert context.tokens(shaped, []) == estimate(shaped, [])
    response.usage = Usage()
    context.observe(response, shaped, [])
    assert context.last_report is None and context.tokens(shaped, []) == estimate(shaped, [])
