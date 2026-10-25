"""Offline display fixture, not evidence of a model/tool execution.

Run from the Stage 3 checkout with PYTHONPATH=src.
"""
import asyncio
import io
from pathlib import Path

from rich.console import Console
from rich.text import Text

from nexus.app.profile import InteractionSummary
from nexus.app.tui import Transcript
from nexus.core.types import RuntimeEvent, ToolResult


async def main():
    root = Path(__file__).parent
    console = Console(file=io.StringIO(), force_terminal=True, width=88, record=True,
                      color_system="truecolor", legacy_windows=False)
    ui = Transcript(console)
    summary = InteractionSummary(ui)

    async def send(kind, data):
        await summary(RuntimeEvent(kind, "fixture", "offline-ui-fixture", "demo", data))

    console.print(Text("OFFLINE UI FIXTURE — no model or tool was executed", style="dim"))
    console.print(Text("You › 检查目录结构，说明修改并列出验证结果", style="bold"))
    await send("run_started", {})
    await send("message", {"role": "assistant", "content":
        "我先检查目录和已有测试，再确认这次交互修改涉及的边界。\n"
        "工具的真实结果仍会按独立状态行展示。",
        "tool_calls": [{"id": "demo", "name": "exec_command", "arguments_json": "{}"}]})
    ui.tools["demo"] = "Inspect git diff"
    # Static export omits cursor-erased live frames; actual Live behavior is tested.
    console.record = False
    await send("tool_started", {"call_id": "demo", "name": "exec_command",
                               "arguments_json": '{"command":"git diff"}'})
    await send("tool_finished", {"call_id": "demo", "ok": True, "duration_ms": 200})
    ui.stop_activity()
    console.record = True
    await send("message", ToolResult("demo", True, {}, duration_ms=200).message().public())
    await send("message", {"role": "assistant", "content": """## 交互结果示例

**多行输入** 保留缩进和空行，使用 `Esc` 后按 `Enter` 提交。

| 操作 | 行为 |
| --- | --- |
| Enter | 插入换行 |
| Ctrl+C | 清稿、取消运行或退出空提示符 |

1. 粘贴完整任务。
2. 确认内容后提交。

> 这是离线展示夹具，表格不代表真实任务验收结果。

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
    summary.render(console)
    (root / "preview.txt").write_text(console.export_text(clear=False), encoding="utf-8")
    console.save_html(str(root / "preview.html"), clear=False, inline_styles=True)
    for name in ("preview.txt", "preview.html"):
        target = root / name
        target.write_text("\n".join(line.rstrip() for line in
                                  target.read_text(encoding="utf-8").splitlines()) + "\n",
                          encoding="utf-8")


asyncio.run(main())
