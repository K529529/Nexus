from __future__ import annotations

import asyncio
import io
from typing import Any

import pytest
from prompt_toolkit.cursor_shapes import CursorShape
from prompt_toolkit.data_structures import Size
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.layout.processors import TransformationInput
from prompt_toolkit.output.vt100 import Vt100_Output
from prompt_toolkit.utils import get_cwidth
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from nexus.app.input import PasteDisplay
from nexus.app.tui import Transcript, terminal_text
from tests.test_cli_ux import event, until
from tests.test_input_v02 import editor_session, paste


@pytest.mark.parametrize("text", ["你好", "a中文b", "中é文", "中文👋abc"])
async def test_cjk_cells_cursor_and_backspace_in_framed_editor(text: str) -> None:
    captured = io.StringIO()
    output = Vt100_Output(captured, lambda: Size(rows=24, columns=60), term="xterm-256color")
    async with editor_session(output) as (editor, prompt, pipe, pending):
        pipe.send_text(text)
        await until(lambda: prompt.default_buffer.text == text)
        await until(lambda: prompt.app.renderer.last_rendered_screen is not None
                    and text in captured.getvalue())
        screen = prompt.app.renderer.last_rendered_screen
        assert screen is not None
        assert screen.height == 3
        cursor = screen.get_cursor_position(prompt.app.layout.current_window)
        assert (cursor.x, cursor.y) == (10 + get_cwidth(text), 1)
        assert prompt.app.cursor.get_cursor_shape(prompt.app) == CursorShape.BLINKING_BEAM
        # Ordinary CJK text must pass through without a custom index mapping.
        fragments: StyleAndTextTuples = [("", text)]
        transformed = PasteDisplay(editor.blocks).apply_transformation(TransformationInput(
            prompt.app.layout.current_control, Document(text), 0, lambda i: i,
            fragments, 60, 24,
        ))
        assert transformed.fragments is fragments
        pipe.send_text("\x1b[D\x7f")  # left, then delete the preceding codepoint
        await until(lambda: prompt.default_buffer.text == text[:-2] + text[-1])
        pipe.send_text("\r")
        assert await pending == text[:-2] + text[-1]


async def test_idle_input_does_not_repaint_or_grow_with_terminal_rows() -> None:
    captured = io.StringIO()
    size = [Size(rows=24, columns=60)]
    output = Vt100_Output(captured, lambda: size[0], term="xterm-256color")
    async with editor_session(output) as (_, prompt, pipe, pending):
        pipe.send_text("中文")
        await until(lambda: "中文" in captured.getvalue())
        await asyncio.sleep(0.2)
        before = captured.getvalue()
        await asyncio.sleep(0.2)  # no committed keys while the host may show IME candidates
        assert captured.getvalue() == before
        assert prompt.app.refresh_interval == 0
        size[0] = Size(rows=40, columns=60)
        prompt.app.invalidate()
        await asyncio.sleep(0.2)
        screen = prompt.app.renderer.last_rendered_screen
        assert screen is not None and screen.height == 3
        pipe.send_text("\r")
        assert await pending == "中文"


async def test_cjk_cursor_after_folded_paste_and_resize() -> None:
    captured = io.StringIO()
    size = [Size(rows=24, columns=60)]
    output = Vt100_Output(captured, lambda: size[0], term="xterm-256color")
    body = "待修改段落\n" * 20
    async with editor_session(output) as (editor, prompt, pipe, pending):
        pipe.send_text("前" + paste(body) + "后")
        await until(lambda: bool(editor.blocks))
        await until(lambda: "后" in captured.getvalue())
        screen = prompt.app.renderer.last_rendered_screen
        assert screen is not None
        cursor = screen.get_cursor_position(prompt.app.layout.current_window)
        assert cursor.x == 10 + 2 + len(next(iter(editor.blocks.values())).label) + 2
        size[0] = Size(rows=24, columns=20)
        prompt.app.invalidate()
        await asyncio.sleep(0.2)
        screen = prompt.app.renderer.last_rendered_screen
        assert screen is not None
        cursor = screen.get_cursor_position(prompt.app.layout.current_window)
        assert 0 <= cursor.x < 20 and cursor.y >= 1
        pipe.send_text("\r")
        assert await pending == "前" + body + "后"


@pytest.mark.parametrize("width", [18, 40, 110])
@pytest.mark.parametrize("terminal", [False, True])
def test_user_echo_is_distinct_bounded_literal_and_keeps_original(
    monkeypatch: Any, width: int, terminal: bool,
) -> None:
    monkeypatch.setenv("TERM", "xterm-256color")
    output = io.StringIO()
    console = Console(file=output, width=width, force_terminal=terminal, no_color=True)
    ui = Transcript(console)
    draft = "**这不是模型回复**\n    保留缩进\n" + "额外内容\n" * 100
    original = draft
    rendered: list[Any] = []
    printer = console.print

    def capture(*args: Any, **kwargs: Any) -> None:
        rendered.extend(args)
        printer(*args, **kwargs)

    monkeypatch.setattr(console, "print", capture)
    ui.user_message(draft)
    result = terminal_text(output.getvalue())
    assert "你" in result and "You" not in result
    assert "**这不是模型回复**" in result.replace("\n", "")
    assert "characters" in result or "charact" in result  # very narrow terminal wraps footer
    assert result.count("额外内容") <= 4
    assert draft == original
    panels = [item for item in rendered if isinstance(item, Panel)]
    assert len(panels) == (1 if terminal and width >= 20 else 0)
    if panels:
        assert panels[0].border_style == "#a78bfa" and panels[0].style == "on #211b2e"


async def test_thinking_hierarchy_preserves_public_content_and_tool_rows(monkeypatch: Any) -> None:
    monkeypatch.setenv("TERM", "xterm-256color")
    output = io.StringIO()
    console = Console(file=output, force_terminal=True, width=50,
                      color_system="truecolor", legacy_windows=False)
    ui = Transcript(console)
    rendered: list[Any] = []
    printer = console.print

    def capture(*args: Any, **kwargs: Any) -> None:
        rendered.extend(args)
        printer(*args, **kwargs)

    monkeypatch.setattr(console, "print", capture)
    content = "先确认目录，再读取当前实现。" * 8
    await ui(event("message", {"role": "assistant", "content": content,
                              "tool_calls": [{"id": "c"}],
                              "protocol_data": {"reasoning_content": "PRIVATE"}}))
    titles = [x for x in rendered if isinstance(x, Text) and x.plain.strip() == "Thinking"]
    assert len(titles) == 1 and titles[0].style == "#ece9f4 italic"
    blocks = [x for x in rendered if isinstance(x, Text) and "│" in x.plain]
    assert len(blocks) == 1 and blocks[0].style == "#b8b3c4 italic"
    assert all(line.startswith("│ ") for line in blocks[0].plain.splitlines())
    assert "".join(line[2:] for line in blocks[0].plain.splitlines()) == content
    assert "PRIVATE" not in output.getvalue()
    before = output.tell()
    ui.tool_status("Read src/app.py", "✓ · 0.2s", "dim")
    assert terminal_text(output.getvalue()[before:]) == "› Read src/app.py  ✓ · 0.2s\n"


async def test_complete_frame_returns_on_each_prompt() -> None:
    captured = io.StringIO()
    output = Vt100_Output(captured, lambda: Size(rows=24, columns=60), term="xterm-256color")
    async with editor_session(output) as (editor, prompt, pipe, pending):
        for turn in range(2):
            if turn:
                pending = asyncio.create_task(editor.read())
                await until(lambda: prompt.app.is_running)
            try:
                await asyncio.sleep(0.2)
                screen = prompt.app.renderer.last_rendered_screen
                assert screen is not None and screen.height == 3
                assert screen.data_buffer[0][0].char == "┌"
                assert screen.data_buffer[0][59].char == "┐"
                assert screen.data_buffer[2][0].char == "└"
                assert screen.data_buffer[2][59].char == "┘"
                assert screen.data_buffer[1][59].char == "│"
                pipe.send_text(f"第{turn}轮\r")
                assert await pending == f"第{turn}轮"
            finally:
                if not pending.done():
                    pending.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await pending


def test_summary_readable_styles_preserve_zero_call_semantics(monkeypatch: Any) -> None:
    from nexus.app.profile import InteractionSummary

    async def consume(value: Any) -> None:
        pass

    out = io.StringIO()
    console = Console(file=out, width=100, record=True, force_terminal=True,
                      color_system="truecolor", legacy_windows=False)
    summary = InteractionSummary(consume)
    summary.render(console)
    summary.render(console)
    result = terminal_text(out.getvalue())
    assert result.count("Nexus powering down") == 1
    assert "N/A" in result and "0%" not in result
    html = console.export_html(inline_styles=True)
    assert "#8bdc65" in html and "#f2f2f2" in html and "#e5e7eb" in html


@pytest.mark.parametrize("columns", [60, 80, 120, 205])
async def test_reworked_frame_leaves_room_for_host_ime_preedit(
    monkeypatch: Any, columns: int,
) -> None:
    monkeypatch.setenv("INTELLIJ_TERMINAL_COMMAND_BLOCKS_REWORKED", "1")
    captured = io.StringIO()
    size = [Size(rows=24, columns=columns)]
    output = Vt100_Output(captured, lambda: size[0], term="xterm-256color")
    async with editor_session(output) as (editor, prompt, pipe, pending):
        pipe.send_text("中文")
        await until(lambda: "中文" in captured.getvalue())
        screen = prompt.app.renderer.last_rendered_screen
        assert screen is not None and screen.height == 3
        right = min(columns, 100, max(40, columns - 32)) - 1
        assert screen.data_buffer[0][right].char == "┐"
        assert screen.data_buffer[1][right].char == "│"
        assert screen.data_buffer[2][right].char == "┘"
        # Model the documented HOST inline-inlay overflow, not an IME event sent
        # to Python: before this fix a full-width row + this preedit exceeded cols.
        preedit_cells = get_cwidth("fen'xi'xiang'mu")
        assert right + 1 + preedit_cells <= columns
        assert all(cell.char == " " for x, cell in screen.data_buffer[1].items() if x > right)
        cursor = screen.get_cursor_position(prompt.app.layout.current_window)
        assert (cursor.x, cursor.y) == (14, 1)
        assert prompt.default_buffer.text == "中文" and not pending.done()
        # The same wrapper adapts to resize, without rebuilding input state.
        size[0] = Size(rows=24, columns=columns + 20)
        prompt.app.invalidate()
        await asyncio.sleep(0.2)
        screen = prompt.app.renderer.last_rendered_screen
        assert screen is not None
        right = min(columns + 20, 100, max(40, columns - 12)) - 1
        assert screen.data_buffer[1][right].char == "│"
        pipe.send_text("\r")
        assert await pending == "中文"


async def test_other_terminal_keeps_full_width_frame(monkeypatch: Any) -> None:
    monkeypatch.delenv("INTELLIJ_TERMINAL_COMMAND_BLOCKS_REWORKED", raising=False)
    captured = io.StringIO()
    output = Vt100_Output(captured, lambda: Size(rows=24, columns=80), term="xterm-256color")
    async with editor_session(output) as (_, prompt, pipe, pending):
        pipe.send_text("hello")
        await until(lambda: "hello" in captured.getvalue())
        screen = prompt.app.renderer.last_rendered_screen
        assert screen is not None and screen.data_buffer[1][79].char == "│"
        pipe.send_text("\r")
        assert await pending == "hello"
