from __future__ import annotations

import os
from pathlib import Path

import pytest

from nexus.app.events import Events
from nexus.app.session import SessionLog, read_records, replay
from nexus.core.agent import COMPLETION_GUIDANCE, run_turn
from nexus.core.types import (
    Emit,
    Limits,
    Message,
    ModelError,
    ModelReply,
    Session,
    ToolSpec,
    Usage,
)
from nexus.tools.registry import default_tools
from tests.conftest import Recorder, ScriptedModel, call, reply
from tests.test_conversation import ignore
from tests.test_stagnation import reads, registry


def unfinished() -> ModelReply:
    return reply(call("update_plan", {"plan": [{"step": "Implement", "status": "in_progress"}]}))


def includes_reminder(messages: list[Message]) -> bool:
    return any(COMPLETION_GUIDANCE in m.content for m in messages)


@pytest.mark.parametrize("conflict", ["plan", "debt"])
async def test_first_final_conflict_nudges_once_and_second_final_is_allowed(
    tmp_path: Path,
    conflict: str,
) -> None:
    session = Session(tmp_path)
    setup = (
        unfinished()
        if conflict == "plan"
        else reply(
            call(
                "apply_patch",
                {
                    "patch": "*** Begin Patch\n*** Add File: a.py\n+x=1\n*** End Patch",
                },
            )
        )
    )
    model = ScriptedModel([setup, reply(text="done"), reply(text="Unresolved; stopping here.")])
    events = Recorder()
    result = await run_turn(session, "implement", model, default_tools(session), events, Limits())
    assert result.outcome == "completed" and result.steps == 3
    assert result.final_text == "Unresolved; stopping here."
    nudges = [d for k, d in events.events if k == "completion_nudge"]
    assert len(nudges) == 1 and nudges[0]["step"] == 2
    assert nudges[0]["unfinished_plan"] == (conflict == "plan")
    assert nudges[0]["validation_debt"] == (conflict == "debt")
    assert [includes_reminder(ms) for ms in model.requests] == [False, False, True]
    assert not includes_reminder(session.messages)
    assert all("Progress and validation evidence" not in m.content for m in session.messages)
    assert result.model_calls == 3


async def test_clean_final_is_unchanged(tmp_path: Path) -> None:
    session = Session(tmp_path)
    events = Recorder()
    model = ScriptedModel([reply(text="answer")])
    result = await run_turn(session, "question", model, {}, events, Limits())
    assert result.outcome == "completed" and result.model_calls == result.steps == 1
    assert not any(k == "completion_nudge" for k, _ in events.events)


async def test_nudge_does_not_increase_step_budget(tmp_path: Path) -> None:
    session = Session(tmp_path)
    events = Recorder()
    model = ScriptedModel([unfinished(), reply(text="done")])
    result = await run_turn(
        session, "implement", model, default_tools(session), events, Limits(max_steps=2)
    )
    assert result.outcome == "limited" and result.reason == "max_steps"
    assert result.model_calls == result.steps == 2
    assert len([d for k, d in events.events if k == "completion_nudge"]) == 1


async def test_nudge_is_once_across_tools_resume_and_independent_runs(tmp_path: Path) -> None:
    session = Session(tmp_path)
    writer = SessionLog.create(session, "task", {}, tmp_path)
    try:
        events = Events(session, writer, ignore)
        model = ScriptedModel(
            [unfinished(), reply(text="done"), reply(call("exec_command", {}, "inspect"))]
        )
        result = await run_turn(
            session, "implement", model, registry(session), events, Limits(max_steps=3)
        )
        assert result.outcome == "limited"
        records, _ = read_records(writer.stream)
        restored = replay(records, tmp_path)
        assert restored.completion_nudged_run_id == session.run_id
        assert restored.resume_run_id == session.run_id
        resumed = ScriptedModel([reply(text="Still unresolved; final stop.")])
        result = await run_turn(
            restored, "continue", resumed, registry(restored), Recorder(), Limits()
        )
        assert result.outcome == "completed" and len(resumed.requests) == 1
        assert not includes_reminder(resumed.requests[0])
        fresh = ScriptedModel([unfinished(), reply(text="done"), reply(text="limitation")])
        fresh_events = Recorder()
        result = await run_turn(
            restored, "new task", fresh, registry(restored), fresh_events, Limits()
        )
        assert result.outcome == "completed" and restored.run_id != session.run_id
        assert len([d for k, d in fresh_events.events if k == "completion_nudge"]) == 1
        assert not any(
            COMPLETION_GUIDANCE in str(r["data"]) for r in records if r["kind"] == "message"
        )
    finally:
        writer.close()


async def test_nudge_can_reconcile_plan_without_forcing_mutation(tmp_path: Path) -> None:
    session = Session(tmp_path)
    model = ScriptedModel(
        [
            unfinished(),
            reply(text="done"),
            reply(
                call(
                    "update_plan",
                    {"plan": [{"step": "Explain findings", "status": "completed"}]},
                    "reconcile",
                )
            ),
            reply(text="Analysis complete; no code change required."),
        ]
    )
    events = Recorder()
    result = await run_turn(session, "inspect", model, default_tools(session), events, Limits())
    assert result.outcome == "completed" and result.steps == 4
    assert not includes_reminder(model.requests[-1])
    assert not os.listdir(tmp_path)
    assert len([d for k, d in events.events if k == "completion_nudge"]) == 1


async def test_reminder_survives_context_fallback_but_not_summary_or_storage(
    tmp_path: Path,
) -> None:
    class Model(ScriptedModel):
        rejected = False
        summaries: list[list[Message]] = []

        async def complete(
            self, messages: list[Message], tools: list[ToolSpec], emit: Emit
        ) -> ModelReply:
            if any(m.content.startswith("Summarize the earlier") for m in messages):
                self.summaries.append(messages)
                assert not includes_reminder(messages)
                assert all("Progress and validation evidence" not in m.content for m in messages)
                return reply(text="Earlier inspection recorded.")
            if includes_reminder(messages) and not self.rejected:
                self.rejected = True
                raise ModelError("context_limit")
            return await super().complete(messages, tools, emit)

    session = Session(tmp_path, messages=[Message("system", "rules", seq=1)])
    responses = (
        [unfinished()] + reads(8) + [reply(text="done"), reply(text="Unresolved limitation.")]
    )
    for response in responses:
        response.usage = Usage()
    model = Model(responses)
    events = Recorder()
    result = await run_turn(session, "implement", model, registry(session), events, Limits())
    assert result.outcome == "completed", result
    assert model.rejected and len(model.summaries) == 1
    assert includes_reminder(model.requests[-1])
    assert not includes_reminder(session.messages)
    assert all(not includes_reminder(s.messages) for s in session.compactions.values())
    assert len([d for k, d in events.events if k == "completion_nudge"]) == 1
