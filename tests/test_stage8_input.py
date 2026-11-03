from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from nexus.app.config import default_shell
from nexus.app.input import TaskInput
from nexus.core.agent import run_turn
from nexus.core.inbox import Inbox
from nexus.core.types import Emit, Json, Limits, Message, ModelReply, Session, ToolSpec
from nexus.tools.registry import native_tools
from tests.conftest import Recorder, ScriptedModel, call, python_command, reply
from tests.test_cli_ux import until


async def test_guidance_waits_for_entire_real_tool_batch(tmp_path: Path) -> None:
    inbox, recorder = Inbox(), Recorder()
    model = ScriptedModel([
        reply(call("exec_command", {"command": python_command(
            "import time; print('ready', flush=True); time.sleep(.3)"
        )}, "one"), call("exec_command", {"command": python_command("print('two')")}, "two")),
        reply(text="Answered guidance and completed original task."),
    ])
    session = Session(tmp_path)
    task = asyncio.create_task(run_turn(
        session, "original", model, native_tools(), recorder,
        Limits(shell=default_shell()), inbox=inbox,
    ))
    await asyncio.wait_for(recorder.output_started.wait(), 10)
    await inbox.receive("also explain", "guide", recorder)
    await inbox.receive("next task", "queue", recorder)
    result = await task
    assert result.outcome == "completed"
    request = model.requests[1]
    assert [m.tool_call_id for m in request if m.role == "tool"] == ["one", "two"]
    guide_index = next(i for i, m in enumerate(request) if "also explain" in m.content)
    assert request[guide_index].role == "user"
    assert guide_index > max(i for i, m in enumerate(request) if m.role == "tool")
    assert not any(m.content == "next task" for m in request)
    assert len(inbox.pending) == 1 and inbox.pending[0].mode == "queue"


async def test_guidance_during_final_response_continues_same_run(tmp_path: Path) -> None:
    inbox, recorder = Inbox(), Recorder()

    class Model(ScriptedModel):
        async def complete(self, messages: list[Message], tools: list[ToolSpec],
                           emit: Emit) -> ModelReply:
            if not self.requests:
                await inbox.receive("one more detail", "guide", recorder)
            return await super().complete(messages, tools, emit)

    model = Model([reply(text="initial answer"), reply(text="updated answer")])
    result = await run_turn(Session(tmp_path), "task", model, {}, recorder, Limits(), inbox=inbox)
    assert result.final_text == "updated answer"
    assert len(model.requests) == 2 and not inbox.pending
    assert len([k for k, _ in recorder.events if k == "run_started"]) == 1


async def test_pending_edit_withdraw_and_delivery_receipts() -> None:
    inbox, recorder = Inbox(), Recorder()
    await inbox.receive("first", "queue", recorder)
    ident = inbox.pending[0].id
    assert await inbox.change(ident, "edited", recorder)
    assert inbox.pending[0].text == "edited"
    assert await inbox.change(ident, None, recorder)
    assert not inbox.pending
    assert not await inbox.change(ident, "late", recorder)


async def test_running_editor_enter_tab_and_approval_draft() -> None:
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        prompt: PromptSession[str] = PromptSession(output=DummyOutput())
        editor, inbox, recorder = TaskInput(prompt), Inbox(), Recorder()
        async with editor.during_run(
            lambda text, mode: inbox.receive(text, mode, recorder), lambda: None,
        ):
            await until(lambda: prompt.app.is_running)
            pipe.send_text("guidance")
            await until(lambda: prompt.default_buffer.text == "guidance")
            pipe.send_text("\r")
            await until(lambda: len(inbox.pending) == 1)
            await until(lambda: prompt.app.is_running)
            pipe.send_text("next")
            await until(lambda: prompt.default_buffer.text == "next")
            pipe.send_text("\t")
            await until(lambda: len(inbox.pending) == 2)
            assert [m.mode for m in inbox.pending] == ["guide", "queue"]
            await until(lambda: prompt.app.is_running)
            pipe.send_text("draft 中文")
            await until(lambda: prompt.default_buffer.text == "draft 中文")
            approval = asyncio.create_task(editor.approve())
            await until(lambda: editor.approval is not None and editor.approval.is_running)
            pipe.send_text("\x1b")
            assert await asyncio.wait_for(approval, 5) == "cancel"
            await until(lambda: prompt.app.is_running)
            assert prompt.default_buffer.text == "draft 中文"
        assert editor.saved_draft == "draft 中文"


async def test_guidance_cannot_be_edited_while_commit_is_yielding(tmp_path: Path) -> None:
    inbox, recorder = Inbox(), Recorder()
    await inbox.receive("keep this guidance", "guide", recorder)
    ident = inbox.pending[0].id

    async def emit(kind: str, data: Json, *, protocol_data: Json | None = None) -> int:
        if kind == "message" and data.get("input_id") == ident:
            assert not await inbox.change(ident, None, recorder)
            assert not await inbox.change(ident, "too late", recorder)
        return await recorder(kind, data)

    result = await run_turn(Session(tmp_path), "task", ScriptedModel([reply(text="done")]),
                            {}, emit, Limits(), inbox=inbox)
    assert result.outcome == "completed" and not inbox.pending


async def test_queued_slash_text_runs_once_as_task(tmp_path: Path, monkeypatch: Any) -> None:
    import argparse
    import io
    import sys

    from prompt_toolkit import Application
    from prompt_toolkit.input import DummyInput
    from rich.console import Console

    from nexus.app import bootstrap, cli
    from nexus.core.inbox import InputMessage
    from tests.test_conversation import config

    cfg = config(monkeypatch)
    instances: list[bootstrap.Conversation] = []
    original = bootstrap.Conversation

    class Model(ScriptedModel):
        async def close(self) -> None:
            pass

    model = Model([reply(text="task handled")])

    def conversation(*args: Any, **kwargs: Any) -> bootstrap.Conversation:
        instance = original(*args, **kwargs)
        instance.inbox.pending.append(InputMessage("queued", "/help", "queue"))
        instances.append(instance)
        return instance

    class Prompt:
        def __init__(self, **kwargs: Any) -> None:
            self.app: Application[str] = Application(input=DummyInput(), output=DummyOutput())

        async def prompt_async(self, *args: Any, **kwargs: Any) -> str:
            return "/exit"

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(cli, "load_config", lambda **kw: cfg)
    monkeypatch.setattr(cli, "read_toml", lambda _: {"model": {"name": "test"}})
    monkeypatch.setattr(cli, "Console", lambda **kw: Console(file=io.StringIO()))
    monkeypatch.setattr(cli, "PromptSession", Prompt)
    monkeypatch.setattr(cli, "Conversation", conversation)
    monkeypatch.setattr(bootstrap, "ChatModel", lambda _: model)
    with create_app_session(output=DummyOutput()):
        assert await asyncio.wait_for(cli.application(argparse.Namespace(command=None)), 5) == 0
    assert len(model.requests) == 1
    assert not instances[0].inbox.pending
    assert any(m.input_id == "queued" and m.content == "/help"
               for m in instances[0].session.messages)
