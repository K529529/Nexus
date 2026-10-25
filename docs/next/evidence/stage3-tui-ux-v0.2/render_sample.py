"""Offline UI fixture: real prompt screen snapshots, synthetic transcript events.

Run from the Stage 3 checkout with PYTHONPATH=src. No model or tool is called.
"""
import asyncio
import io
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.application import create_app_session
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output.color_depth import ColorDepth
from prompt_toolkit.output.vt100 import Vt100_Output
from rich.console import Console
from rich.text import Text

from nexus.app.input import INPUT_STYLE, TaskInput
from nexus.app.tui import Transcript
from nexus.core.types import RuntimeEvent, ToolResult

ROOT = Path(__file__).parent

async def main():
    console = Console(file=io.StringIO(), force_terminal=True, width=88, record=True,
                      color_system="truecolor", legacy_windows=False)
    console.print("OFFLINE UI FIXTURE — no model or tool executed", style="dim")
    output = Vt100_Output(io.StringIO(), lambda: Size(rows=24, columns=88),
                         term="xterm-256color", default_color_depth=ColorDepth.DEPTH_24_BIT)
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=output):
        prompt = PromptSession(output=output)
        editor = TaskInput(prompt)
        pending = asyncio.create_task(editor.read())
        async def wait_for(predicate):
            async with asyncio.timeout(5):
                while not predicate():
                    await asyncio.sleep(0.02)
            await asyncio.sleep(0.2)
        def snapshot(title):
            console.print(title, style="bold")
            screen = prompt.app.renderer.last_rendered_screen
            assert screen is not None
            for y in range(screen.height):
                row = Text()
                for x in range(88):
                    char = screen.data_buffer[y][x]
                    if char.width == 0:
                        continue
                    attrs = INPUT_STYLE.get_attrs_for_style_str(char.style)
                    style = " ".join(s for s in (
                        "#" + attrs.color if attrs.color else "",
                        "on #" + attrs.bgcolor if attrs.bgcolor else "",
                        "bold" if attrs.bold else "",
                        "italic" if attrs.italic else "",
                    ) if s)
                    row.append(char.char, style=style)
                console.print(row, no_wrap=True)
        try:
            await wait_for(lambda: prompt.app.is_running)
            pipe.send_text("\x1b[200~" + "中文任务行\n" * 82 + "结尾" + "\x1b[201~")
            await wait_for(lambda: bool(editor.blocks))
            snapshot("Actual prompt screen: folded paste")
            pipe.send_text("\x03")
            await wait_for(lambda: not editor.blocks)
            pipe.send_text("/")
            await wait_for(lambda: prompt.default_buffer.complete_state is not None)
            pipe.send_text("\x1b[B")
            await asyncio.sleep(0.2)
            snapshot("Actual prompt screen: slash menu")
            pipe.send_text("\x03")
            await wait_for(lambda: not prompt.default_buffer.text)
            pipe.send_text("/exit\r")
            assert await pending == "/exit"
        finally:
            if not pending.done():
                pending.cancel()
                try:
                    await pending
                except asyncio.CancelledError:
                    pass
    ui = Transcript(console)
    async def send(kind, data):
        await ui(RuntimeEvent(kind, "fixture", "offline-ui-fixture", "demo", data))
    await send("message", {"role": "assistant", "content":
        "我先检查输入区和已有测试，再确认粘贴及取消行为。\n工具结果继续按独立状态行展示。",
        "tool_calls": [{"id": "demo", "name": "exec_command", "arguments_json": "{}"}]})
    ui.tools["demo"] = "Inspect git diff"
    await send("message", ToolResult("demo", True, {}, duration_ms=200).message().public())
    await send("message", {"role": "assistant", "content": """## 交互结果示例

**完整原文**在发送时恢复；输入区域显示紧凑的 `paste` 块。

| 操作 | 行为 |
| --- | --- |
| Enter | 发送；候选菜单打开时先选择命令 |
| F2 | 开始/结束安全粘贴 |
| F3 | 展开粘贴块编辑 |

1. 粘贴完整任务。
2. 确认后按 Enter 发送。

> 本页是离线展示夹具，不代表真实任务验收结果。

```python
print("你好，Nexus")
```

```text
src/nexus/app/
├── cli.py
├── input.py
└── tui.py
```
"""})
    (ROOT / "preview.txt").write_text(console.export_text(clear=False), encoding="utf-8")
    console.save_html(str(ROOT / "preview.html"), clear=False, inline_styles=True)
    for name in ("preview.txt", "preview.html"):
        path = ROOT / name
        path.write_text("\n".join(line.rstrip() for line in
                        path.read_text(encoding="utf-8").splitlines()).rstrip() + "\n", encoding="utf-8")

asyncio.run(main())
