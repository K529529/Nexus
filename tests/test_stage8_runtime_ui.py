from __future__ import annotations

import asyncio
import io
from typing import IO, Literal, cast

import pytest
from prompt_toolkit import PromptSession
from prompt_toolkit.application import create_app_session
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.output.vt100 import Vt100_Output
from prompt_toolkit.patch_stdout import StdoutProxy
from prompt_toolkit.utils import get_cwidth
from rich.console import Console

from nexus.app.input import TaskInput
from nexus.app.tui import Transcript
from tests.test_cli_ux import event, until
from tests.test_input_v02 import paste


def screen_rows(prompt: PromptSession[str]) -> list[str]:
    screen = prompt.app.renderer.last_rendered_screen
    assert screen is not None
    return ["".join(c.char for _, c in sorted(screen.data_buffer[y].items()))
            for y in range(screen.height)]


@pytest.mark.parametrize("reworked", [False, True])
async def test_one_renderer_keeps_live_input_through_output_resize_and_approval(
    monkeypatch: pytest.MonkeyPatch, reworked: bool,
) -> None:
    monkeypatch.setenv("INTELLIJ_TERMINAL_COMMAND_BLOCKS_REWORKED", str(int(reworked)))
    monkeypatch.setenv("TERM", "xterm-256color")
    captured = io.StringIO()
    size = [Size(rows=30, columns=100)]
    terminal = Vt100_Output(captured, lambda: size[0], term="xterm-256color")
    delivered: list[tuple[str, str, str]] = []

    async def receive(text: str, mode: Literal["guide", "queue"], target: str) -> None:
        delivered.append((text, mode, target))

    with create_pipe_input() as pipe, create_app_session(input=pipe, output=terminal):
        prompt: PromptSession[str] = PromptSession(output=terminal)
        editor = TaskInput(prompt)
        editor.target_agent = "child-123456"
        editor.agent_panel = lambda: "\n".join([
            "Async Agents · 2/4 active",
            "├ ● scout child-123 · running · 6.3s",
            "│   exec_command · 10,532 tok · fresh",
            "├ ● worker child-456 · running · 7.1s",
            "│   model · 5,234 tok · fresh",
        ])
        # Rich's committed output uses the same real VT transport as the editor.
        # No Rich Live is allowed to independently move this transport's cursor.
        with StdoutProxy(raw=True, sleep_between_writes=0.001) as stdout:
            console = Console(file=cast(IO[str], stdout), force_terminal=True, width=100,
                              color_system="truecolor", legacy_windows=False)
            ui = Transcript(console)
            ui.editor = editor
            renders: list[None] = []
            prompt.app.after_render += lambda _: renders.append(None)
            async with editor.during_run(receive, lambda: None):
                await until(lambda: prompt.app.is_running and bool(renders))
                pipe.send_text("指导中文")
                await until(lambda: prompt.default_buffer.text == "指导中文")
                for index in range(5):
                    await ui(event("model_started", {}))
                    await ui(event("assistant_delta", {"text": "预览中文" * 200}))
                    ui.flush()
                    assert ui.preview is None and ui.activity is None
                    await ui(event("message", {"role": "assistant", "content":
                        f"Committed transcript {index}", "tool_calls": [{"id": str(index)}]}))
                    await ui(event("tool_started", {"name": "exec_command",
                        "call_id": str(index), "arguments_json": '{"command":"git status"}'}))
                    assert ui.activity is None
                await until(lambda: "Committed transcript 4" in captured.getvalue())
                await until(lambda: any("Inspect git status" in row for row in screen_rows(prompt)))
                assert prompt.default_buffer.text == "指导中文"
                rows = screen_rows(prompt)
                assert next(i for i, row in enumerate(rows) if "Async Agents" in row) < next(
                    i for i, row in enumerate(rows) if "Nexus" in row)
                assert any("Enter guide" in row and "Tab queue" in row for row in rows)
                assert any("指导中文" in row for row in rows)
                # Shrink and grow with a multiline, wide-character draft and preview.
                pipe.send_text("\x0a第二行" + paste("粘贴\n" * 20))
                await until(lambda: bool(editor.blocks))
                draft = editor.expand(prompt.default_buffer.text)
                for dimensions in ((10, 40), (8, 25), (24, 80)):
                    size[0] = Size(rows=dimensions[0], columns=dimensions[1])
                    count = len(renders)
                    editor.set_activity("Read " + "很长路径" * 50, "预览" * 200)
                    def redrawn(count: int = count) -> bool:
                        return len(renders) > count
                    await until(redrawn)
                    rows = screen_rows(prompt)
                    screen = prompt.app.renderer.last_rendered_screen
                    assert screen is not None and screen.height <= dimensions[0]
                    cursor = screen.get_cursor_position(prompt.app.layout.current_window)
                    assert 0 <= cursor.y < dimensions[0] and 0 <= cursor.x < dimensions[1]
                    assert any("Working" in row for row in rows)
                    # At 25 columns the editor scrolls its first logical line to
                    # keep the multiline cursor visible. Both frame rails and
                    # the current editable paste line must still be on screen.
                    assert any("┌" in row for row in rows) and any("└" in row for row in rows)
                    assert "lines]" in rows[cursor.y]
                    assert all(get_cwidth(fragment[1]) < dimensions[1]
                               for fragment in editor.runtime_text())
                    assert editor.expand(prompt.default_buffer.text) == draft
                approval = asyncio.create_task(editor.approve())
                await until(lambda: editor.approval is not None and editor.approval.is_running)
                pipe.send_text("\x1b")
                assert await approval == "cancel"
                await until(lambda: prompt.app.is_running and prompt.default_buffer.text != "")
                assert editor.expand(prompt.default_buffer.text) == draft
                pipe.send_text("\r")
                await until(lambda: len(delivered) == 1)
                await until(lambda: prompt.app.is_running and not prompt.default_buffer.text)
                pipe.send_text("排队任务")
                await until(lambda: prompt.default_buffer.text == "排队任务")
                pipe.send_text("\t")
                await until(lambda: len(delivered) == 2)
                assert delivered == [(draft, "guide", "child-123456"),
                                     ("排队任务", "queue", "child-123456")]
            ui.stop_activity()


async def test_many_agents_are_bounded_and_completed_state_is_retained() -> None:
    captured = io.StringIO()
    terminal = Vt100_Output(captured, lambda: Size(rows=12, columns=60))
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=terminal):
        prompt: PromptSession[str] = PromptSession(output=terminal)
        editor = TaskInput(prompt)
        editor.agent_panel = lambda: "Async Agents\n" + "\n".join(
            f"├ ✓ scout child-{i} · completed" for i in range(20))

        async def receive(text: str, mode: Literal["guide", "queue"], target: str) -> None:
            pass

        async with editor.during_run(receive, lambda: None):
            await until(lambda: prompt.app.renderer.last_rendered_screen is not None)
            rows = screen_rows(prompt)
            assert len(rows) <= 12
            assert any("more rows · /agents" in row for row in rows)
            assert any("completed" in row for row in rows)
            assert any("Nexus" in row for row in rows)
            assert any("Enter guide" in row and "Tab queue" in row for row in rows)
        # The editor remains usable after the task ends; completed agents remain inspectable.
        pending = asyncio.create_task(editor.read())
        try:
            await until(lambda: prompt.app.is_running)
            pipe.send_text("next task")
            await until(lambda: prompt.default_buffer.text == "next task")
            rows = screen_rows(prompt)
            assert any("completed" in row for row in rows)
            assert not any("Working" in row for row in rows)
            pipe.send_text("\r")
            assert await pending == "next task"
        finally:
            if not pending.done():
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending


async def test_reworked_native_output_keeps_live_help_inline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class NativeOutput(DummyOutput):
        def get_win32_screen_buffer_info(self) -> None:
            raise AssertionError("Reworked terminal must not use native cursor reconciliation")

        def get_size(self) -> Size:
            return Size(rows=24, columns=80)

    monkeypatch.setenv("INTELLIJ_TERMINAL_COMMAND_BLOCKS_REWORKED", "1")
    output = NativeOutput()
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=output):
        prompt: PromptSession[str] = PromptSession(output=output)
        editor = TaskInput(prompt)
        editor.agent_panel = lambda: "Async Agents\n├ ● scout child · running"
        delivered: list[str] = []

        async def receive(text: str, mode: Literal["guide", "queue"], target: str) -> None:
            delivered.append(text)

        async with editor.during_run(receive, lambda: None):
            await until(lambda: prompt.app.renderer.last_rendered_screen is not None)
            assert not editor.inline_toolbar  # Preserve the accepted idle IME layout.
            assert prompt.bottom_toolbar is None  # Running help must not anchor to screen bottom.
            rows = screen_rows(prompt)
            assert "Enter guide" in rows[6], rows
            assert all(not row.strip() for row in rows[7:])
            assert any("Nexus" in row for row in rows)
            assert not any("│" == row[-1:] for row in rows)  # Right rail stays unpainted for IME.
            pipe.send_text("中文指导")
            await until(lambda: prompt.default_buffer.text == "中文指导")
            pipe.send_text("\r")
            await until(lambda: delivered == ["中文指导"])
