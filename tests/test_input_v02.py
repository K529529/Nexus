from __future__ import annotations

import asyncio
import io
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from prompt_toolkit import PromptSession
from prompt_toolkit.application import create_app_session
from prompt_toolkit.clipboard import ClipboardData
from prompt_toolkit.data_structures import Size
from prompt_toolkit.document import Document
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.layout.processors import TransformationInput
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.output.color_depth import ColorDepth
from prompt_toolkit.output.vt100 import Vt100_Output
from rich.console import Console
from rich.text import Text

from nexus.app.input import COMMANDS, PURPLE, PasteDisplay, TaskInput
from nexus.app.tui import Transcript
from tests.test_cli_ux import event, until

F2 = "\x1bOQ"
F3 = "\x1bOR"


def paste(text: str) -> str:
    return "\x1b[200~" + text + "\x1b[201~"


@asynccontextmanager
async def editor_session(output: Any = None) -> AsyncIterator[Any]:
    terminal_output = output or DummyOutput()
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=terminal_output):
        prompt: PromptSession[str] = PromptSession(output=output or DummyOutput())
        editor = TaskInput(prompt)
        pending = asyncio.create_task(editor.read())
        try:
            await until(lambda: prompt.app.is_running)
            yield editor, prompt, pipe, pending
        finally:
            if not pending.done():
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending


async def test_enter_sends_and_explicit_newline_edits() -> None:
    async with editor_session() as (_, prompt, pipe, pending):
        pipe.send_text("first\x0asecond\x1bOSthird")  # Ctrl+J and F4
        await until(lambda: prompt.default_buffer.text == "first\nsecond\nthird")
        assert not pending.done()
        pipe.send_text("\r")
        assert await asyncio.wait_for(pending, 5) == "first\nsecond\nthird"
        assert prompt.show_frame is True


async def test_folded_display_frame_and_cursor_mapping(monkeypatch: Any) -> None:
    monkeypatch.setenv("TERM", "xterm-256color")
    capture = io.StringIO()
    output = Vt100_Output(capture, lambda: Size(rows=24, columns=80),
                         term="xterm-256color", default_color_depth=ColorDepth.DEPTH_24_BIT)
    text = "SECRET_BODY 中文\n" * 82 + "tail"
    async with editor_session(output) as (editor, prompt, pipe, pending):
        pipe.send_text(paste(text))
        await until(lambda: bool(editor.blocks))
        await until(lambda: "[paste #1 +83 lines]" in capture.getvalue())
        assert "SECRET_BODY" not in capture.getvalue()
        screen = prompt.app.renderer.last_rendered_screen
        assert screen is not None and screen.height == 4  # frame + inline VT help; no menu reserve
        assert "Nexus" in capture.getvalue() and "┌" in capture.getvalue()
        assert "38;2;167;139;250" in capture.getvalue()  # theme purple, not a mock layout
        source = "a" + prompt.default_buffer.text + "z"
        processor = PasteDisplay(editor.blocks)
        transformed = processor.apply_transformation(TransformationInput(
            prompt.app.layout.current_control, Document(source), 0, lambda i: i,
            [("", source)], 80, 24,
        ))
        display = "".join(part[1] for part in transformed.fragments)
        assert display == "a[paste #1 +83 lines]z"
        assert transformed.source_to_display(2) == len(display) - 1
        assert transformed.display_to_source(4) == 1
        assert transformed.display_to_source(len(display)) == len(source)
        pipe.send_text("\r")
        assert await pending == text


async def test_multiple_blocks_and_literal_labels_are_not_global_replacements() -> None:
    first, second = "甲\n" * 20, "乙\n" * 30
    async with editor_session() as (editor, prompt, pipe, pending):
        pipe.send_text("[paste #1 +21 lines] " + paste(first) + " between " + paste(second))
        await until(lambda: len(editor.blocks) == 2)
        assert len(prompt.default_buffer.text) < 100
        pipe.send_text("\r")
        expected = "[paste #1 +21 lines] " + first + " between " + second
        assert await pending == expected
        assert editor.blocks == {} and editor.capture is None
        assert editor.history.get_strings()[-1] == expected


async def test_delete_undo_and_expand_edit_paste_atom() -> None:
    body = "print('中文')\n" * 10
    async with editor_session() as (editor, prompt, pipe, pending):
        pipe.send_text(paste(body))
        await until(lambda: bool(editor.blocks))
        pipe.send_text("\x7f")  # Backspace removes one entire block
        await until(lambda: prompt.default_buffer.text == "")
        pipe.send_text("\x1f")  # Ctrl+_ undo retains the original block payload
        await until(lambda: len(prompt.default_buffer.text) == 1)
        assert editor.expand(prompt.default_buffer.text) == body
        pipe.send_text(F3)
        await until(lambda: prompt.default_buffer.text == body)
        pipe.send_text("EDITED\r")
        assert await pending == body + "EDITED"


async def test_cancel_clears_blocks_and_pending_paste() -> None:
    async with editor_session() as (editor, prompt, pipe, pending):
        pipe.send_text(paste("private draft\n" * 10))
        await until(lambda: bool(editor.blocks))
        pipe.send_text("\x03" + paste("RESIDUAL\n" * 10) + "\r")
        await until(lambda: not editor.blocks and not prompt.default_buffer.text)
        assert not pending.done()
        pipe.send_text("fresh\r")
        assert await pending == "fresh"
        assert editor.history.get_strings() == ["fresh"]


async def test_f2_capture_spans_slow_chunks_and_does_not_submit() -> None:
    async with editor_session() as (editor, prompt, pipe, pending):
        pipe.send_text(F2 + "first\r\n")
        await until(lambda: editor.capture is not None)
        await asyncio.sleep(0.2)
        pipe.send_text("second\r\n")
        await asyncio.sleep(0.2)
        assert not pending.done()
        assert len(prompt.default_buffer.text) == 1
        assert editor.expand(prompt.default_buffer.text) == "first\nsecond\n"
        pipe.send_text(F2)
        await until(lambda: editor.capture is None)
        assert not pending.done()
        pipe.send_text("\r")
        assert await pending == "first\nsecond\n"


async def test_clipboard_and_history_expand_payload_without_recursive_substitution() -> None:
    async with editor_session() as (editor, prompt, pipe, pending):
        pipe.send_text(paste("original\n" * 10))
        await until(lambda: bool(editor.blocks))
        atom = prompt.default_buffer.text
        editor.clipboard.set_data(ClipboardData(atom))
        assert editor.clipboard.get_data().text == "original\n" * 10
        # A literal internal-codepoint pasted as content must remain literal.
        pipe.send_text(paste(atom))
        await until(lambda: len(prompt.default_buffer.text) == 2)
        pipe.send_text("\r")
        assert await pending == "original\n" * 10 + atom
        assert editor.history.get_strings()[-1] == "original\n" * 10 + atom


@pytest.mark.parametrize("prefix,keys,expected", [
    ("/", "\x1b[A", "/exit"),
    ("/res", "\x1b[B", "/resume"),
    ("/memory o", "\x1b[B\x1b[B", "/memory off"),
])
async def test_slash_menu_filter_arrows_enter_selects_before_send(
    prefix: str, keys: str, expected: str,
) -> None:
    async with editor_session() as (_, prompt, pipe, pending):
        pipe.send_text(prefix)
        await until(lambda: prompt.default_buffer.complete_state is not None)
        state = prompt.default_buffer.complete_state
        assert state is not None
        assert all(c.text.startswith(prefix) for c in state.completions)
        if prefix == "/":
            assert {c.text for c in state.completions} == set(COMMANDS)
        pipe.send_text(keys + "\r")
        await until(lambda: prompt.default_buffer.text == expected
                    and prompt.default_buffer.complete_state is None)
        assert not pending.done()
        pipe.send_text("\r")
        assert await pending == expected


async def test_enter_without_selection_chooses_first_command_only() -> None:
    async with editor_session() as (_, prompt, pipe, pending):
        pipe.send_text("/res")
        await until(lambda: prompt.default_buffer.complete_state is not None)
        pipe.send_text("\r")
        await until(lambda: prompt.default_buffer.text == "/resume")
        assert not pending.done()
        pipe.send_text("\r")
        assert await pending == "/resume"


async def test_working_animates_and_stops_before_tools_and_final(monkeypatch: Any) -> None:
    monkeypatch.setenv("TERM", "xterm-256color")
    output = io.StringIO()
    ui = Transcript(Console(file=output, force_terminal=True, no_color=True))
    await ui(event("model_started", {}))
    assert ui.preview is not None and ui.working is not None
    spinner = ui.working
    spinner.start_time = 0
    first, second = spinner.render(0), spinner.render(0.16)
    assert isinstance(first, Text) and isinstance(second, Text)
    assert "Working..." in first.plain and first.plain != second.plain
    assert spinner.style == PURPLE
    live = ui.preview
    await ui(event("tool_started", {"name": "exec_command", "call_id": "c"}))
    assert not live.is_started and ui.working is None
    ui.stop_activity()
    await ui(event("model_started", {}))
    await ui(event("assistant_delta", {"text": "# Done"}))
    live = ui.preview
    assert live is not None
    await ui(event("model_finished", {"finish_reason": "stop"}))
    await ui(event("message", {"role": "assistant", "content": "# Done"}))
    assert not live.is_started and ui.preview is None and ui.working is None
    assert ui.flush_handle is None


async def test_capture_ignores_undo_and_history_edits() -> None:
    async with editor_session() as (editor, prompt, pipe, pending):
        pipe.send_text(F2 + "first")
        await until(lambda: editor.capture is not None)
        pipe.send_text("\x1f\x0b\x1b[A\x14")  # undo, kill-line, history, transpose
        await asyncio.sleep(0.1)
        assert prompt.default_buffer.text == editor.capture
        pipe.send_text(" second" + F2 + "\r")
        assert await pending == "first second"


async def test_clipboard_yank_refolds_without_expanding_literal_atom() -> None:
    async with editor_session() as (editor, prompt, pipe, pending):
        body = "original\n" * 10
        pipe.send_text(paste(body))
        await until(lambda: bool(editor.blocks))
        atom = prompt.default_buffer.text
        pipe.send_text(paste(atom))
        await until(lambda: len(prompt.default_buffer.text) == 2)
        editor.clipboard.set_data(ClipboardData(prompt.default_buffer.text))
        assert editor.clipboard.get_data().text == body + atom
        pipe.send_text("\x19")  # Ctrl+Y uses the same literal-safe paste path
        await until(lambda: len(prompt.default_buffer.text) == 3)
        pipe.send_text("\r")
        assert await pending == body + atom + body + atom


async def test_history_recall_across_prompts_keeps_original_payload() -> None:
    async with editor_session() as (editor, prompt, pipe, pending):
        body = "original 中文\n" * 10
        pipe.send_text(paste(body) + "\r")
        assert await pending == body
        next_read = asyncio.create_task(editor.read())
        try:
            await until(lambda: prompt.app.is_running)
            pipe.send_text("\x1b[A")
            await until(lambda: prompt.default_buffer.text == body)
            assert not editor.blocks
            pipe.send_text("\r")
            assert await next_read == body
        finally:
            if not next_read.done():
                next_read.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await next_read
