"""Offline UI demo: no models, tools, configuration, logs or network access.

Run with the repository's Python. Ctrl+C stops. --snapshot PATH exports an
actual prompt_toolkit screen to HTML for visual review without a terminal.
The agent data is a display fixture, not evidence of real agent execution.
"""
from __future__ import annotations

import argparse
import asyncio
import io
from pathlib import Path
from typing import Literal

from prompt_toolkit import PromptSession
from prompt_toolkit.application import create_app_session, get_app_session
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output.vt100 import Vt100_Output
from rich.console import Console
from rich.style import Style
from rich.terminal_theme import TerminalTheme
from rich.text import Text

from nexus.app.input import TaskInput
from nexus.app.tui import Transcript
from nexus.core.types import RuntimeEvent

AGENTS = """Async Agents · 当前任务 / 待处理 · 2/4 active
会话累计（主+全部子代理）: ≥15766 tok?
├ ● 执行流程调查 · scout a12bc345 · running
├ ● 测试覆盖调查 · reviewer d67ef890 · running
历史 2 个 · /agents history"""


async def demo(snapshot: Path | None) -> None:
    prompt: PromptSession[str] = PromptSession(output=get_app_session().output)
    editor = TaskInput(prompt)
    editor.agent_panel = lambda: AGENTS
    console = Console(highlight=False, force_terminal=True if snapshot else None)
    ui = Transcript(console)
    ui.editor = editor
    stopped = asyncio.Event()
    rendered = asyncio.Event()
    prompt.app.after_render += lambda _: rendered.set()

    async def receive(text: str, mode: Literal["guide", "queue"], target: str) -> None:
        console.print(Text(f"Received {mode} → {target}: {text}", style="cyan"))

    console.print("Nexus · offline UI demo · simulated agents · Ctrl+C stops", style="dim")
    async with editor.during_run(receive, stopped.set):
        await asyncio.wait_for(rendered.wait(), 5)
        await ui(RuntimeEvent("model_started", "", "demo", "demo", {}))
        await ui(RuntimeEvent("assistant_delta", "", "demo", "demo", {
            "text": "正在检查目录结构；两个子代理在各自任务中继续工作。"}))
        ui.flush()
        if snapshot is not None:
            editor.set_activity("Inspect fixtures", "正在检查目录结构；子代理继续执行各自任务。")
            prompt.default_buffer.insert_text("请先补充测试，再汇总结果")
            rendered.clear()
            prompt.app.invalidate()
            await asyncio.wait_for(rendered.wait(), 5)
            screen = prompt.app.renderer.last_rendered_screen
            assert screen is not None
            text = Text()
            for y in range(screen.height):
                for x in range(prompt.app.output.get_size().columns):
                    cell = screen.data_buffer[y][x]
                    attrs = prompt.app._merged_style.get_attrs_for_style_str(cell.style)
                    text.append(cell.char, Style(
                        color=("#" + attrs.color if len(attrs.color or "") == 6 else None),
                        bgcolor=("#" + attrs.bgcolor if len(attrs.bgcolor or "") == 6 else None),
                        bold=attrs.bold, italic=attrs.italic,
                    ))
                text.append("\n")
            recording = Console(file=io.StringIO(), record=True, width=100)
            recording.print(text, end="")
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            html = recording.export_html(inline_styles=True, theme=TerminalTheme(
                (30, 31, 34), (222, 222, 230), [(0, 0, 0)] * 8))
            await asyncio.to_thread(snapshot.write_text,
                "\n".join(line.rstrip() for line in html.splitlines()) + "\n", encoding="utf-8")
        else:
            # Produce committed output while the same draft remains editable.
            for index in range(30):
                try:
                    await asyncio.wait_for(stopped.wait(), 3)
                    break
                except TimeoutError:
                    ui.tool_status(f"Inspect fixture {index + 1}", "✓ · 0.2s", "dim")
                    editor.set_activity("Inspect fixtures")
    ui.stop_activity()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path)
    arguments = parser.parse_args()
    if arguments.snapshot:
        with create_pipe_input() as source, create_app_session(
            input=source, output=Vt100_Output(io.StringIO(), lambda: Size(rows=30, columns=100)),
        ):
            asyncio.run(demo(arguments.snapshot))
    else:
        asyncio.run(demo(None))
