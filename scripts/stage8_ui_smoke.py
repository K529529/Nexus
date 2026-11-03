"""Offline interaction demo with display fixtures, no model/network/config access.

F6 selects an agent, Enter guides, Tab queues to root, Esc returns. Ctrl+C stops.
--snapshot PATH exports actual prompt_toolkit frames (HTML and cell JSON).
Fixtures demonstrate rendering only; real CLI/Loop coverage lives in tests.
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Literal, cast

from prompt_toolkit import PromptSession
from prompt_toolkit.application import create_app_session, get_app_session
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output.vt100 import Vt100_Output
from rich.console import Console
from rich.style import Style
from rich.terminal_theme import TerminalTheme
from rich.text import Text

from nexus.app.bootstrap import Conversation
from nexus.app.input import TaskInput
from nexus.app.screen import ConversationScreen
from nexus.app.tui import Transcript
from nexus.core.types import Message, RuntimeEvent


class DemoAgents:
    def __init__(self) -> None:
        self.agents = {
            ident: SimpleNamespace(id=ident, task_name=name, role="scout", state="running",
                accepting=True, conversation=SimpleNamespace(session=SimpleNamespace(messages=[
                    Message("user", f"调查{name}，报告主要入口与测试缺口。"),
                    Message("assistant", "已定位入口，正在核对调用链与异常路径。"),
                ])))
            for ident, name in (("a12bc345", "执行流程调查"), ("d67ef890", "测试覆盖调查"))
        }

    def current_agents(self) -> set[str]:
        return set(self.agents)

    def panel(self) -> str:
        active = sum(a.accepting for a in self.agents.values())
        return (f"Async Agents · 当前任务 / 待处理 · {active}/3 active\n"
                "会话累计（主+全部子代理）: 15,766 tok\n" + "\n".join(
                    f"├ {'●' if a.accepting else '✓'} {a.task_name} · scout {a.id} · {a.state}"
                    for a in self.agents.values()))


def export_frame(prompt: PromptSession[str], path: Path) -> None:
    frame = prompt.app.renderer.last_rendered_screen
    assert frame is not None
    text, cells = Text(), []
    for y in range(frame.height):
        for x in range(prompt.app.output.get_size().columns):
            cell = frame.data_buffer[y][x]
            attrs = prompt.app._merged_style.get_attrs_for_style_str(cell.style)
            color = "#" + attrs.color if len(attrs.color or "") == 6 else "#dedee6"
            background = "#" + attrs.bgcolor if len(attrs.bgcolor or "") == 6 else "#1e1f22"
            text.append(cell.char, Style(color=color, bgcolor=background,
                                        bold=attrs.bold, italic=attrs.italic))
            if cell.char and cell.char != " ":
                cells.append({"x": x, "y": y, "text": cell.char, "color": color})
        text.append("\n")
    recording = Console(file=io.StringIO(), record=True,
                        width=prompt.app.output.get_size().columns)
    recording.print(text, end="")
    path.parent.mkdir(parents=True, exist_ok=True)
    html = recording.export_html(inline_styles=True, theme=TerminalTheme(
        (30, 31, 34), (222, 222, 230), [(0, 0, 0)] * 8))
    path.write_text("\n".join(line.rstrip() for line in html.splitlines()) + "\n", encoding="utf-8")
    path.with_suffix(".cells.json").write_text(json.dumps({
        "columns": prompt.app.output.get_size().columns, "rows": frame.height, "cells": cells,
    }, ensure_ascii=False), encoding="utf-8")


async def demo(snapshot: Path | None, size: list[Size]) -> None:
    prompt: PromptSession[str] = PromptSession(output=get_app_session().output)
    editor = TaskInput(prompt)
    console = Console(file=io.StringIO() if snapshot else None, highlight=False,
                      force_terminal=True, legacy_windows=False, width=100)
    manager = DemoAgents()
    conversation = cast(Conversation, SimpleNamespace(
        coordinator=manager, session=SimpleNamespace(session_id="offline-demo")))
    screen = ConversationScreen(editor, console, lambda: conversation)
    ui = Transcript(console)
    ui.editor = editor
    stopped, rendered = asyncio.Event(), asyncio.Event()
    prompt.app.after_render += lambda _: rendered.set()

    async def receive(text: str, mode: Literal["guide", "queue"], target: str) -> None:
        parts = text.split()
        if text == "/exit":
            stopped.set()
        elif text == "/agents":
            screen.open_picker()
        elif len(parts) == 2 and parts[0] in {"/agent", "/agents"}:
            await screen.select(parts[1])
        elif mode == "queue":
            screen.inspect("已排队到主代理（演示）：" + text)
        elif target != "root" and not manager.agents[target].accepting:
            screen.inspect("此代理已完成，当前为只读历史。Esc 返回主代理。")
        else:
            screen.inspect(f"已接收指导 → {target}（演示）：{text}")

    async def save(name: str) -> None:
        assert snapshot is not None
        rendered.clear()
        screen.invalidate(immediate=True)
        await asyncio.wait_for(rendered.wait(), 5)
        export_frame(prompt, snapshot.with_name(snapshot.stem + "-" + name + ".html"))

    try:
        ui.user_message("并行调查执行流程和测试覆盖，最后给出主要风险及建议。")
        console.print("两个 scout 已开始。我会核对模块边界，等待调查结果后汇总。")
        ui.tool_status("Read src/core/agent.py", "✓ · 0.2s", "dim")
        ui.tool_status("Read tests/test_agent.py", "✓ · 0.1s", "dim")
        async with editor.during_run(receive, stopped.set):
            await asyncio.wait_for(rendered.wait(), 5)
            screen.activity("root", "整理主流程", "正在核对消息接收、工具执行和收尾边界。")
            if snapshot is not None:
                prompt.default_buffer.insert_text("请优先检查取消和异常路径")
                await save("root")
                screen.open_picker()
                screen.choice = 1
                await save("picker")
                await screen.select("a12bc345")
                prompt.default_buffer.insert_text("补充一条可稳定复现的测试")
                await save("child")
                manager.agents["a12bc345"].state = "completed"
                manager.agents["a12bc345"].accepting = False
                await screen.child_event("a12bc345", RuntimeEvent("message", "", "", None,
                    {"role": "assistant",
                     "content": "调查完成：取消路径已覆盖，建议补充收尾边界测试。"}))
                await save("completed")
                await screen.select("root")
                size[0] = Size(rows=16, columns=60)
                await save("narrow")
            else:
                for index in range(30):
                    try:
                        await asyncio.wait_for(stopped.wait(), 3)
                        break
                    except TimeoutError:
                        ident = list(manager.agents)[index % 2]
                        if index == 5:
                            manager.agents[ident].state = "completed"
                            manager.agents[ident].accepting = False
                        await screen.child_event(ident, RuntimeEvent("message", "", "", None,
                            {"role": "assistant",
                             "content": f"调查进展 {index + 1}：已核对一条路径。"}))
                        screen.refresh_status()
    finally:
        ui.stop_activity()
        screen.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path)
    arguments = parser.parse_args()
    dimensions = [Size(rows=30, columns=100)]
    if arguments.snapshot:
        with create_pipe_input() as source, create_app_session(
            input=source, output=Vt100_Output(io.StringIO(), lambda: dimensions[0]),
        ):
            asyncio.run(demo(arguments.snapshot, dimensions))
    else:
        asyncio.run(demo(None, dimensions))
