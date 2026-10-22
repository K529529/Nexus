from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nexus.app.events import Events
from nexus.app.session import SessionLog, read_records, replay
from nexus.core import tokens
from nexus.core.agent import run_turn
from nexus.core.context import Context, ContextBuilder
from nexus.core.tokens import TokenEstimator
from nexus.core.types import Limits, Message, Session, Usage
from tests.conftest import Recorder, ScriptedModel, call, reply
from tests.test_session_compaction import SummaryModel, ignore, seed, summary_reply


class Clock:
    value = 0.0

    def tick(self) -> float:
        self.value += 0.001
        return self.value


def test_one_timer_per_count_including_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock()
    monkeypatch.setattr(tokens, "time", SimpleNamespace(perf_counter=clock.tick))
    counter = TokenEstimator()
    messages = [Message("user", "中文 Python JSON " * 100)]
    original = deepcopy(messages)
    expected = counter._raw(messages, [])
    assert counter.count(messages, []) == expected
    counter.observe(expected, messages, [])
    assert counter.timing_since()["token_count_calls"] == 2
    assert counter.timing_since()["token_count_ms"] == pytest.approx(2)
    factor = counter.factor
    counter.reset_timing()
    assert counter.factor == factor and messages == original
    assert counter.timing_since()["token_count_calls"] == 0

    def fail(*_: Any) -> int:
        raise ValueError("fixed failure")

    monkeypatch.setattr(counter, "_raw", fail)
    with pytest.raises(ValueError, match="fixed failure"):
        counter.raw(messages, [])
    assert counter.timing_since()["token_count_calls"] == 1
    assert counter.timing_since()["token_count_ms"] == pytest.approx(1)


async def test_step_snapshots_exclude_api_wait_and_do_not_accumulate_across_steps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = Clock()
    monkeypatch.setattr(tokens, "time", SimpleNamespace(perf_counter=clock.tick))

    class OfflineModel(ScriptedModel):
        async def complete(self, messages: Any, tools: Any, emit: Any) -> Any:
            clock.value += 30  # Simulated API wait; no wall-clock sleep or network.
            return await super().complete(messages, tools, emit)

    model = OfflineModel([reply(call("missing", {})), reply(text="done")])
    recorder = Recorder()
    result = await run_turn(Session(tmp_path), "task", model, {}, recorder, Limits())
    assert result.outcome == "completed"
    projections = [d for k, d in recorder.events if k == "context_projection"]
    usages = [d for k, d in recorder.events if k == "context_usage"]
    assert len(projections) == len(usages) == 2
    for step, (projection, usage) in enumerate(zip(projections, usages, strict=True), 1):
        assert projection["step"] == usage["step"] == step
        assert projection["token_count_calls"] == 4
        assert usage["token_count_calls"] == 7
        assert projection["token_count_ms"] == pytest.approx(4)
        assert usage["token_count_ms"] == pytest.approx(7)
        assert usage["compaction_token_count_calls"] == 0
    assert not any(k == "token_count" for k, _ in recorder.events)


async def test_compaction_metrics_are_additive_metadata_not_checkpoint_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = Clock()
    monkeypatch.setattr(tokens, "time", SimpleNamespace(perf_counter=clock.tick))

    class OfflineSummary(SummaryModel):
        async def complete(self, messages: Any, tools: Any, emit: Any) -> Any:
            clock.value += 50
            return await super().complete(messages, tools, emit)

    session = Session(tmp_path)
    writer = SessionLog.create(session, "performance", {}, tmp_path)
    events = Events(session, writer, ignore)
    try:
        await seed(session, events)
        original = deepcopy(session.messages)
        context = Context(Limits(context_window=70000))
        context.begin_step()
        response = summary_reply()
        response.usage = Usage()
        assert await context.prepare(
            session,
            ContextBuilder().build_active_context(session),
            [],
            OfflineSummary([response]),
            events,
        )
        records, _ = read_records(writer.stream)
        started = next(r["data"] for r in records if r["kind"] == "compaction_started")
        usage = next(r["data"] for r in records if r["kind"] == "compaction_usage")
        compacted = next(r["data"] for r in records if r["kind"] == "context_compacted")
        assert started["compaction_token_count_calls"] == 1
        assert usage["compaction_token_count_calls"] == 3
        assert usage["compaction_token_count_ms"] == pytest.approx(3)
        assert started["history_split_ms"] == usage["history_split_ms"] >= 0
        performance = context.performance()
        assert performance["token_count_calls"] == (
            performance["main_token_count_calls"] + performance["compaction_token_count_calls"]
        )
        assert not any(k in compacted for k in performance)
        assert session.messages == original
        restored = replay(records, tmp_path)
        assert restored.checkpoint == session.checkpoint
    finally:
        writer.close()
