from __future__ import annotations

import argparse
import asyncio
import io
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal

import pytest
from prompt_toolkit import PromptSession
from prompt_toolkit.application import create_app_session
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output.vt100 import Vt100_Output
from rich.console import Console

from nexus.app import bootstrap, cli
from nexus.app.cli import agent_command
from nexus.app.config import MultiAgentConfig
from nexus.app.input import TaskInput
from nexus.app.screen import ConversationScreen
from nexus.app.tui import Transcript
from nexus.core.types import Emit, Message, ModelReply, ToolSpec
from tests.conftest import ScriptedModel, call, reply
from tests.test_cli_ux import until
from tests.test_conversation import config
from tests.test_stage8_collaboration import root_conversation
from tests.test_stage8_runtime_ui import screen_rows


@pytest.mark.parametrize("reworked", [False, True])
async def test_persistent_composer_navigation_and_child_guidance(
    tmp_path: Path, monkeypatch: Any, reworked: bool,
) -> None:
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("INTELLIJ_TERMINAL_COMMAND_BLOCKS_REWORKED", str(int(reworked)))
    root = root_conversation(tmp_path, monkeypatch)
    manager = root.coordinator
    assert manager is not None and root.events is not None
    captured, printed = io.StringIO(), io.StringIO()
    sizes = [Size(rows=28, columns=100)]
    terminal = Vt100_Output(captured, lambda: sizes[0], term="xterm-256color")
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=terminal):
        prompt: PromptSession[str] = PromptSession(output=terminal)
        editor = TaskInput(prompt)
        console = Console(file=printed, force_terminal=True, legacy_windows=False, width=100)
        screen = ConversationScreen(editor, console, lambda: root)
        transcript = Transcript(console)
        transcript.editor = editor
        root.consumer = transcript
        received: list[tuple[str, str, str]] = []

        async def receive(text: str, mode: Literal["guide", "queue"], target: str) -> None:
            if await agent_command(root, editor, text):
                return
            received.append((text, mode, target))
            if mode == "guide" and target != "root":
                assert (await manager.user_message(target, text))["received"]
            else:
                assert root.events is not None
                await root.inbox.receive(text, mode, root.events)

        try:
            first = (await manager.spawn("root", {"role": "scout", "scope": "a",
                "task": "first task", "task_name": "执行流程调查"}))["agent_id"]
            second = (await manager.spawn("root", {"role": "scout", "scope": "b",
                "task": "second task", "task_name": "测试覆盖调查"}))["agent_id"]
            await until(lambda: all(a.accepting for a in manager.agents.values()))
            child = manager.owner(first)
            assert child.events is not None
            await child.events("message", {"role": "assistant", "content": "child-only report"})
            console.print("Root-only transcript")
            async with editor.during_run(receive, lambda: None):
                await until(lambda: prompt.app.is_running)
                run_future = prompt.app.future
                assert run_future is not None
                pipe.send_text("root guidance")
                await until(lambda: prompt.default_buffer.text == "root guidance")
                pipe.send_text("\r")
                await until(lambda: len(received) == 1)
                assert prompt.app.future is run_future and not run_future.done()
                assert prompt.app.refresh_interval == 0
                pipe.send_text("root draft 中文")
                await until(lambda: prompt.default_buffer.text == "root draft 中文")
                pipe.send_text("\x1b[17~")  # F6
                await until(lambda: screen.picker)
                pipe.send_text("\x1b[B\r")
                await until(lambda: editor.target_agent == first and not screen.picker)
                assert prompt.default_buffer.text == ""
                assert "Root-only transcript" not in "".join(f[1] for f in screen.body())
                # Live session history is loaded on entry, then child output remains isolated.
                await child.events("message", {"role": "assistant", "content": "visible child"})
                await until(lambda: "visible child" in "\n".join(screen_rows(prompt)))
                pipe.send_text("child guidance")
                await until(lambda: prompt.default_buffer.text == "child guidance")
                pipe.send_text("\r")
                await until(lambda: len(received) == 2)
                assert received[-1] == ("child guidance", "guide", first)
                assert any(m.text == "child guidance" for m in child.inbox.guides())
                pipe.send_text("queued task")
                await until(lambda: prompt.default_buffer.text == "queued task")
                pipe.send_text("\t")
                await until(lambda: len(received) == 3)
                assert received[-1] == ("queued task", "queue", first)
                await editor.finish_submission()
                assert root.inbox.pending[-1].mode == "queue"
                pipe.send_text("child draft")
                await until(lambda: prompt.default_buffer.text == "child draft")
                pipe.send_text("\x1b")
                await until(lambda: editor.target_agent == "root")
                assert prompt.default_buffer.text == "root draft 中文"
                # The plural command is accepted as a navigation alias too.
                await agent_command(root, editor, f"/agents {second[:8]}")
                assert editor.target_agent == second
                await screen.select(first)
                assert prompt.default_buffer.text == "child draft"
                approval = asyncio.create_task(editor.approve())
                await until(lambda: editor.approval is not None and editor.approval.is_running)
                pipe.send_text("\x1b")
                assert await approval == "cancel"
                await until(lambda: prompt.app.is_running)
                assert prompt.default_buffer.text == "child draft"
                assert editor.target_agent == first
                assert printed.getvalue() == ""  # No Rich/Stdout erase-and-repaint cycle.
                pasted = "\n" + "中文多行内容\n" * 20
                pipe.send_text("\x1b[200~" + pasted + "\x1b[201~")
                await until(lambda: editor.expand(prompt.default_buffer.text)
                            == "child draft" + pasted)
                pipe.send_text("\r")
                await until(lambda: len(received) == 4)
                assert received[-1] == ("child draft" + pasted, "guide", first)
                assert editor.history.get_strings()[-1] == "child draft" + pasted
        finally:
            await root.close()
            screen.close()


async def test_real_cli_full_multiagent_journey(tmp_path: Path, monkeypatch: Any) -> None:
    """Real CLI/Inbox/Loop; only model I/O is replaced with deterministic barriers."""
    monkeypatch.setenv("TERM", "xterm-256color")
    cfg = replace(config(monkeypatch), memory_enabled=False,
                  multi_agent=MultiAgentConfig(enabled=True))
    child_release, root_release = asyncio.Event(), asyncio.Event()
    instances: list[bootstrap.Conversation] = []
    editors: list[TaskInput] = []
    models: list[Any] = []

    class Model(ScriptedModel):
        def __init__(self, settings: Any) -> None:
            self.child = settings.reasoning_effort == "low"
            super().__init__([reply(text="child report"), reply(text="guided child report")]
                if self.child else [reply(
                    call("spawn_agent", {"role": "scout", "task": "inspect flow",
                        "scope": "a", "task_name": "执行流程调查"}, "a"),
                    call("spawn_agent", {"role": "scout", "task": "inspect tests",
                        "scope": "b", "task_name": "测试覆盖调查"}, "b")),
                    *[reply(text="root final report") for _ in range(4)]])
            models.append(self)

        async def complete(self, messages: list[Message], tools: list[ToolSpec],
                           emit: Emit) -> ModelReply:
            if self.child:
                await child_release.wait()
            elif self.requests:
                await root_release.wait()
            return await super().complete(messages, tools, emit)

        async def close(self) -> None:
            pass

    class Conversation(bootstrap.Conversation):
        def __init__(self, workspace: Path, config: Any, consumer: Any) -> None:
            super().__init__(workspace, config, consumer, home=tmp_path / "home")
            instances.append(self)

    class Editor(TaskInput):
        def __init__(self, prompt: PromptSession[str]) -> None:
            super().__init__(prompt)
            editors.append(self)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(cli, "read_toml", lambda _: {"model": {"name": "test"}})
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    printed = io.StringIO()
    monkeypatch.setattr(cli, "Console", lambda **_: Console(
        file=printed, force_terminal=True, legacy_windows=False, width=100))
    monkeypatch.setattr(cli, "Conversation", Conversation)
    monkeypatch.setattr(cli, "TaskInput", Editor)
    monkeypatch.setattr(bootstrap, "ChatModel", Model)
    terminal = Vt100_Output(io.StringIO(), lambda: Size(rows=30, columns=100))
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=terminal):
        prompt: PromptSession[str] = PromptSession(output=terminal)
        monkeypatch.setattr(cli, "PromptSession", lambda **_: prompt)
        app = asyncio.create_task(cli.application(argparse.Namespace(command=None)))

        async def send(text: str, key: str = "\r") -> None:
            pipe.send_text(text)
            await until(lambda: prompt.default_buffer.text == text)
            pipe.send_text(key)

        try:
            await until(lambda: prompt.app.is_running)
            await send("investigate with two scouts")
            await until(lambda: instances[0].coordinator is not None
                        and len(instances[0].coordinator.agents) == 2)
            manager = instances[0].coordinator
            assert manager is not None
            await until(lambda: all(a.accepting for a in manager.agents.values()))
            first = next(iter(manager.agents))
            editor = editors[0]
            screen = editor.screen
            assert screen is not None
            await until(lambda: prompt.app.is_running and editor.live_submit is not None)
            await send(f"/agents {first[:8]}")
            await until(lambda: editor.target_agent == first)
            for command, hint in (("/withdraw", "Use /withdraw ID."),
                                  ("/withdraw   ", "Use /withdraw ID."),
                                  ("/edit", "Use /edit ID replacement text."),
                                  ("/edit missing", "Use /edit ID replacement text.")):
                screen.notice = ""
                await send(command)
                def shown(expected: str = hint) -> bool:
                    return expected in screen.notice
                await until(shown)
                assert editor.listener is not None and not editor.listener.done()
            await send("check edge cases")
            await until(lambda: any(m.text == "check edge cases"
                        for m in manager.owner(first).inbox.guides()))
            await send("follow-up root task", "\t")
            await until(lambda: any(m.text == "follow-up root task"
                        for m in instances[0].inbox.pending))
            assert instances[0].inbox.pending[-1].mode == "queue"
            child_release.set()
            await until(lambda: all(a.state == "completed" for a in manager.agents.values()))
            child_model = manager.owner(first).model
            assert isinstance(child_model, Model)
            assert any("check edge cases" in m.content for m in child_model.requests[-1])
            await until(lambda: "只读" in "\n".join(screen_rows(prompt)))
            await send("must not revive")
            await until(lambda: "停止接收" in screen.notice)
            assert not manager.owner(first).inbox.guides()
            assert "root final report" not in "".join(f[1] for f in screen.body())
            root_release.set()
            await until(lambda: any(m.content == "follow-up root task"
                        for m in models[0].requests[-1]) and editor.live_submit is None)
            await until(lambda: prompt.app.is_running)
            assert editor.target_agent == "root"
            assert "follow-up root task" in [m.content for m in models[0].requests[-1]]
            assert not instances[0].inbox.pending
            calls_before = len(models[0].requests)
            await send(f"/agents {first[:8]}")
            await until(lambda: editor.target_agent == first and prompt.app.is_running)
            await send("idle readonly input")
            await until(lambda: "停止接收" in screen.notice and prompt.app.is_running)
            assert len(models[0].requests) == calls_before
            pipe.send_text("\x1b")
            await until(lambda: editor.target_agent == "root")
            previous_session = screen.session_identity
            await send("/new")
            await until(lambda: screen.session_identity != previous_session
                        and prompt.app.is_running)
            assert not screen.children and not screen.drafts
            assert "root final report" not in "".join(f[1] for f in screen.body())
            await send("/exit")
            assert await asyncio.wait_for(app, 5) == 0
            assert printed.getvalue().count("Interaction Summary") == 1
        finally:
            child_release.set()
            root_release.set()
            if not app.done():
                pipe.close()
                await asyncio.wait_for(app, 5)


async def test_quiet_frames_typing_scroll_and_async_delivery(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    monkeypatch.setenv("TERM", "xterm-256color")
    root = root_conversation(tmp_path, monkeypatch)
    transport = io.StringIO()
    size = [Size(rows=24, columns=90)]
    output = Vt100_Output(transport, lambda: size[0], term="xterm-256color")
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=output):
        prompt: PromptSession[str] = PromptSession(output=output)
        editor = TaskInput(prompt)
        console = Console(file=io.StringIO(), force_terminal=True, legacy_windows=False)
        screen = ConversationScreen(editor, console, lambda: root)
        ui = Transcript(console)
        ui.editor = editor
        root.consumer = ui
        received: list[tuple[str, str, str]] = []
        blocked, release = asyncio.Event(), asyncio.Event()

        async def receive(text: str, mode: Literal["guide", "queue"], target: str) -> None:
            blocked.set()
            await release.wait()
            received.append((text, mode, target))

        try:
            async with editor.during_run(receive, lambda: None):
                await until(lambda: prompt.app.is_running)
                for i in range(50):
                    console.print(f"History line {i}")
                await asyncio.sleep(0.3)
                before = transport.getvalue()
                await asyncio.sleep(0.4)
                assert transport.getvalue() == before  # No periodic rendering when unchanged.
                pipe.send_text("中文 draft")
                await until(lambda: prompt.default_buffer.text == "中文 draft")
                await asyncio.sleep(0.2)
                before = transport.getvalue()
                for i in range(10):
                    screen.activity("root", "Thinking", f"streaming preview {i}")
                    await asyncio.sleep(0.02)
                assert transport.getvalue() == before  # No token redraw while composing.
                pipe.send_text("\x1b[5~")  # PgUp
                await until(lambda: screen.scroll_line is not None)
                await until(lambda: "History line 49" not in "\n".join(screen_rows(prompt)))
                anchor = screen.scroll_line
                for i in range(10):
                    console.print(f"New output {i}")
                await asyncio.sleep(0.2)
                assert screen.scroll_line == anchor
                assert "New output 9" not in "\n".join(screen_rows(prompt))
                for rows, columns in ((10, 40), (8, 25), (24, 90)):
                    size[0] = Size(rows=rows, columns=columns)
                    frame_ready = asyncio.Event()

                    def rendered_frame(_: Any, ready: asyncio.Event = frame_ready) -> None:
                        ready.set()

                    prompt.app.after_render.add_handler(rendered_frame)
                    try:
                        prompt.app.invalidate()
                        await asyncio.wait_for(frame_ready.wait(), 3)
                    finally:
                        prompt.app.after_render.remove_handler(rendered_frame)
                    rendered = prompt.app.renderer.last_rendered_screen
                    assert rendered is not None
                    cursor = rendered.get_cursor_position(prompt.app.layout.current_window)
                    assert rendered.height <= rows and 0 <= cursor.y < rows
                    assert any("┌" in row for row in screen_rows(prompt))
                    assert prompt.default_buffer.text == "中文 draft"
                pipe.send_text("\r")
                await asyncio.wait_for(blocked.wait(), 3)
                pipe.send_text("queued")
                await until(lambda: prompt.default_buffer.text == "queued")
                pipe.send_text("\t")
                await until(lambda: len(editor.submission_queue) == 1)
                pipe.send_text("draft kept")
                await until(lambda: prompt.default_buffer.text == "draft kept")
                await editor.pause_listener()
                assert editor.saved_draft == "draft kept"
                assert received == []
                release.set()
                await editor.finish_submission()
                assert received == [("中文 draft", "guide", "root"), ("queued", "queue", "root")]
        finally:
            release.set()
            await root.close()
            screen.close()


async def test_screen_callback_failure_is_not_swallowed(tmp_path: Path, monkeypatch: Any) -> None:
    root = root_conversation(tmp_path, monkeypatch)
    output = Vt100_Output(io.StringIO(), lambda: Size(rows=24, columns=90))
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=output):
        prompt: PromptSession[str] = PromptSession(output=output)
        editor = TaskInput(prompt)
        screen = ConversationScreen(editor, Console(file=io.StringIO()), lambda: root)

        async def fail(text: str, mode: Literal["guide", "queue"], target: str) -> None:
            raise OSError("receipt persistence failed")

        try:
            with pytest.raises(OSError, match="receipt persistence failed"):
                async with editor.during_run(fail, lambda: None):
                    await until(lambda: prompt.app.is_running)
                    pipe.send_text("accepted")
                    await until(lambda: prompt.default_buffer.text == "accepted")
                    pipe.send_text("\r")
                    await until(lambda: editor.listener is not None and editor.listener.done())
            assert editor.delivery is not None
            with pytest.raises(OSError, match="receipt persistence failed"):
                await editor.finish_submission()
        finally:
            await root.close()
            screen.close()


async def test_screen_cancel_stops_active_run(tmp_path: Path, monkeypatch: Any) -> None:
    root = root_conversation(tmp_path, monkeypatch)
    output = Vt100_Output(io.StringIO(), lambda: Size(rows=24, columns=90))
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=output):
        prompt: PromptSession[str] = PromptSession(output=output)
        editor = TaskInput(prompt)
        console = Console(file=io.StringIO(), force_terminal=True)
        screen = ConversationScreen(editor, console, lambda: root)
        ui = Transcript(console)
        ui.editor = editor
        root.consumer = ui
        task = asyncio.create_task(cli.drive(root, "long running task", editor=editor))
        try:
            await until(lambda: prompt.app.is_running)
            pipe.send_text("draft must not become guidance")
            await until(lambda: bool(prompt.default_buffer.text))
            pipe.send_text("\x03")
            result = await asyncio.wait_for(task, 5)
            assert result.outcome == "aborted"
            assert editor.live_submit is None and not root.inbox.pending
            assert editor.saved_draft == ""
            assert "就绪" in screen.heading()
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await root.close()
            screen.close()
