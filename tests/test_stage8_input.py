from __future__ import annotations

import asyncio
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from nexus.app.config import default_shell
from nexus.app.input import TaskInput
from nexus.core.agent import run_turn
from nexus.core.inbox import Inbox
from nexus.core.types import Emit, Limits, Message, ModelReply, Session, ToolSpec
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
