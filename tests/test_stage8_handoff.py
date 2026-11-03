from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal

import pytest
from prompt_toolkit import PromptSession
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from nexus.app import bootstrap, cli
from nexus.app.events import Events
from nexus.app.input import TaskInput
from nexus.app.session import SessionLog, read_records
from nexus.core.inbox import Inbox
from nexus.core.types import Emit, Json, Message, ModelReply, RuntimeEvent, Session, ToolSpec
from tests.conftest import Recorder, ScriptedModel, reply
from tests.test_cli_ux import until
from tests.test_conversation import config


@pytest.mark.parametrize("mode", ["guide", "queue"])
async def test_submitted_input_survives_prompt_handoff(
    mode: Literal["guide", "queue"], monkeypatch: Any,
) -> None:
    inbox, recorder = Inbox(), Recorder()
    prompt_returned, blocked_reader = asyncio.Event(), asyncio.Event()
    delivery_started, release_delivery = asyncio.Event(), asyncio.Event()
    delivered: list[tuple[str, str, str]] = []
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        prompt: PromptSession[str] = PromptSession(output=DummyOutput())
        editor = TaskInput(prompt)
        editor.target_agent = "chosen-child"
        read = editor.read

        async def paused_read() -> str:
            text = await read()
            prompt_returned.set()
            await blocked_reader.wait()  # Exact window: accepted, listener has not returned.
            return text

        async def receive(text: str, selected_mode: Literal["guide", "queue"], target: str) -> None:
            delivery_started.set()
            await release_delivery.wait()  # Approval must not wait for this callback.
            await inbox.receive(text, selected_mode, recorder)
            delivered.append((text, selected_mode, target))

        monkeypatch.setattr(editor, "read", paused_read)
        running = editor.during_run(receive, lambda: None)
        await running.__aenter__()
        try:
            await until(lambda: prompt.app.is_running)
            pipe.send_text("submitted 中文")
            await until(lambda: prompt.default_buffer.text == "submitted 中文")
            pipe.send_text("\r" if mode == "guide" else "\t")
            await asyncio.wait_for(prompt_returned.wait(), 3)
            await asyncio.wait_for(delivery_started.wait(), 3)
            editor.target_agent, editor.submit_mode = "different-child", "guide"
            if mode == "guide":
                approval = asyncio.create_task(editor.approve())
                await until(lambda: editor.approval is not None and editor.approval.is_running)
                assert not inbox.pending and editor.submitted is not None
                release_delivery.set()
                await until(lambda: len(inbox.pending) == 1)
                pipe.send_text("\x1b")
                assert await asyncio.wait_for(approval, 3) == "cancel"
            else:
                ending = asyncio.create_task(running.__aexit__(None, None, None))
                await until(lambda: editor.listener is None)
                assert not ending.done()  # Waiting only for the accepted submission.
                release_delivery.set()
                await asyncio.wait_for(ending, 3)
            await editor.pause_listener()
            await editor.finish_submission()
            assert delivered == [("submitted 中文", mode, "chosen-child")]
            assert [(m.text, m.mode) for m in inbox.pending] == [("submitted 中文", mode)]
            assert len([k for k, _ in recorder.events if k == "input_received"]) == 1
            assert editor.saved_draft == "" and editor.submitted is None
        finally:
            release_delivery.set()
            if mode == "guide":
                await running.__aexit__(None, None, None)


async def test_cancel_after_receipt_persisted_finishes_memory_commit(tmp_path: Path) -> None:
    persisted, release = asyncio.Event(), asyncio.Event()
    session, inbox = Session(tmp_path), Inbox()
    writer = SessionLog.create(session, "test", {}, tmp_path / "state")

    async def consumer(event: RuntimeEvent) -> None:
        if event.kind == "input_received":
            persisted.set()
            await release.wait()

    events = Events(session, writer, consumer)
    receipt = asyncio.create_task(inbox.receive("accepted", "queue", events))
    try:
        await asyncio.wait_for(persisted.wait(), 3)
        receipt.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await receipt
        with writer.path.open("rb") as stream:
            rows, _ = read_records(stream)
        restored = Inbox()
        restored.restore(rows)
        assert len(inbox.pending) == 1 and inbox.pending == restored.pending
        assert inbox.changed.is_set()
    finally:
        release.set()
        writer.close()


async def test_receipt_write_failure_is_not_hidden() -> None:
    async def fail(kind: str, data: Json, *, protocol_data: Json | None = None) -> int:
        raise OSError("disk failure")

    inbox = Inbox()
    with pytest.raises(OSError, match="disk failure"):
        await inbox.receive("text", "guide", fail)
    assert not inbox.pending


async def test_malformed_live_commands_do_not_stop_real_input(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    received: list[RuntimeEvent] = []

    async def consume(event: RuntimeEvent) -> None:
        received.append(event)

    class Model(ScriptedModel):
        async def complete(self, messages: list[Message], tools: list[ToolSpec],
                           emit: Emit) -> ModelReply:
            if not self.requests:
                entered.set()
                await release.wait()
            return await super().complete(messages, tools, emit)

        async def close(self) -> None:
            pass

    model = Model([reply(text="initial"), reply(text="guided")])
    monkeypatch.setattr(bootstrap, "ChatModel", lambda _: model)
    conversation = bootstrap.Conversation(tmp_path, replace(config(monkeypatch),
        memory_enabled=False), consume, home=tmp_path / "state")
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        prompt: PromptSession[str] = PromptSession(output=DummyOutput())
        editor = TaskInput(prompt)
        run = asyncio.create_task(cli.drive(conversation, "original", editor=editor))

        async def submit(text: str, key: str = "\r") -> None:
            await until(lambda: prompt.app.is_running and not prompt.app.is_done)
            # Bracketed paste avoids completion selection and preserves trailing spaces.
            pipe.send_text("\x1b[200~" + text + "\x1b[201~")
            await until(lambda: prompt.default_buffer.text == text)
            pipe.send_text(key)

        try:
            await asyncio.wait_for(entered.wait(), 3)
            for command in ("/withdraw", "/withdraw ", "/withdraw    ", "/edit", "/edit ID",
                            "/edit ID   ", "/withdraw missing", "/edit missing replacement"):
                warnings = len([e for e in received if e.kind == "warning"])
                await submit(command)
                def warned(before: int = warnings) -> bool:
                    return len([e for e in received if e.kind == "warning"]) > before
                await until(warned)
                assert editor.listener is not None and not editor.listener.done()
            await submit("first guidance")
            await until(lambda: len(conversation.inbox.pending) == 1)
            ident = conversation.inbox.pending[0].id
            await submit(f"/edit {ident} updated guidance")
            await until(lambda: conversation.inbox.pending[0].text == "updated guidance")
            await submit("withdraw this", "\t")
            await until(lambda: len(conversation.inbox.pending) == 2)
            removed = conversation.inbox.pending[1].id
            await submit(f"/withdraw {removed}")
            await until(lambda: len(conversation.inbox.pending) == 1)
            await submit("next task", "\t")
            await until(lambda: len(conversation.inbox.pending) == 2)
            release.set()
            assert (await asyncio.wait_for(run, 5)).outcome == "completed"
            assert any("updated guidance" in m.content for m in model.requests[-1])
            assert [(m.text, m.mode) for m in conversation.inbox.pending] == [
                ("next task", "queue"),
            ]
            assert conversation.events is not None
            assert not await conversation.inbox.change(ident, "too late", conversation.events)
        finally:
            release.set()
            if not run.done():
                run.cancel()
                await run
            await conversation.close()
