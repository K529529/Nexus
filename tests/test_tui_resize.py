from __future__ import annotations

import asyncio
import io

import pytest
from prompt_toolkit import PromptSession
from prompt_toolkit.application import create_app_session
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output.vt100 import Vt100_Output
from rich.console import Console
from rich.control import Control
from rich.live import Live
from rich.text import Text

from nexus.app.console import DisplayReplay
from nexus.app.input import TaskInput
from tests.test_cli_ux import until
from tests.test_input_v02 import paste


def test_replay_preserves_print_options_and_snapshots_without_live_frames() -> None:
    console = Console(file=io.StringIO(), force_terminal=True, color_system="truecolor",
                      no_color=False, width=80)
    display = DisplayReplay(console)
    text = Text("original 中文", style="bold")
    console.print(text, style="red", end="\n\n")
    text.append(" MUTATED")
    with Live("TRANSIENT SPINNER", console=console, transient=True, auto_refresh=False):
        console.print("committed result")
    console.print(Control.move_to(0, 0))
    count = len(display.entries)
    first = display.repaint()
    assert "original 中文" in first and "committed result" in first
    assert "MUTATED" not in first and "TRANSIENT SPINNER" not in first
    assert "\x1b[1;31m" in first
    console.width = 20
    second = display.repaint()
    assert "original 中文" in second and "committed result" in second
    assert len(display.entries) == count
    assert second.count("committed result") == 1


@pytest.mark.parametrize("cancel", ["none", "submit", "clear"])
async def test_resize_rebuilds_history_and_preserves_draft_without_cpr(cancel: str) -> None:
    stream = io.StringIO()
    size = Size(rows=30, columns=90)
    output = Vt100_Output(stream, lambda: size, enable_cpr=True)
    history = Console(file=io.StringIO(), force_terminal=True, width=90)
    display = DisplayReplay(history)
    history.print("committed transcript")
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=output):
        prompt: PromptSession[str] = PromptSession(output=output)
        editor = TaskInput(prompt, display.repaint)
        pending = asyncio.create_task(editor.read())
        try:
            await until(lambda: prompt.app.is_running)
            # This literal is user data and must not be scrubbed to hide CPR bugs.
            draft = "中文 🐱\n[14;1R\nthird\nfourth"
            pipe.send_text(paste(draft))
            await until(lambda: editor.expand(prompt.default_buffer.text) == draft)
            for next_size in (Size(10, 40), Size(48, 130), Size(12, 60), Size(30, 90)):
                size = next_size
                editor.queue_resize()
            await until(lambda: "\x1b[3J" in stream.getvalue())
            assert "committed transcript" in stream.getvalue()
            assert "\x1b[6n" not in stream.getvalue()
            assert editor.expand(prompt.default_buffer.text) == draft
            if cancel != "none":
                editor.queue_resize()
            if cancel == "clear":
                pipe.send_text("\x03")
                await until(lambda: not prompt.default_buffer.text and not editor.resize_pending)
                assert not pending.done()
                draft = "continue"
                pipe.send_text(draft)
            pipe.send_text("\r")
            assert await pending == draft
            submitted_output = stream.getvalue()
            await asyncio.sleep(0.15)
            assert editor.resize_handle is None
            assert stream.getvalue() == submitted_output
        finally:
            if not pending.done():
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending
