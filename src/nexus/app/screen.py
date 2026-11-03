"""One terminal viewport for conversation output, navigation and the composer.

This is presentation state only. Agent lifetimes, inboxes and history remain in
Conversation/Subagents; switching views never starts or resumes an agent.
"""
from __future__ import annotations

import asyncio
import io
from collections import defaultdict
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from prompt_toolkit.data_structures import Point
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import ANSI, StyleAndTextTuples, to_formatted_text
from prompt_toolkit.layout import ConditionalContainer, HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from rich.console import Console
from rich.text import Text

from nexus.core.types import RuntimeEvent

if TYPE_CHECKING:
    from nexus.app.bootstrap import Conversation
    from nexus.app.input import TaskInput
    from nexus.app.tui import Transcript


class ConversationScreen:
    def __init__(self, editor: TaskInput, console: Console,
                 conversation: Callable[[], Conversation]) -> None:
        self.editor, self.console, self.conversation = editor, console, conversation
        self.session_identity = conversation().session.session_id
        self.buffers: dict[str, list[str]] = defaultdict(list)
        self.formatted: dict[str, StyleAndTextTuples] = {}
        self.previews: dict[str, str] = {}
        self.activities: dict[str, str] = {}
        self.children: dict[str, Transcript] = {}
        self.drafts: dict[str, str] = {}
        self.loaded: set[str] = {"root"}
        self.picker = False
        self.history = False
        self.choice = 0
        self.scroll_line: int | None = None
        self.notice = "F6 选择代理 · PgUp/PgDn 阅读 · Ctrl+End 回到最新"
        self.pending_render: asyncio.TimerHandle | None = None
        self.last_status = ""
        self.original_print = console.print
        self.capture(console, "root")
        editor.screen = self
        app = editor.prompt.app
        app.erase_when_done = True
        app.min_redraw_interval = 0.05
        app.max_render_postpone_time = 0.05
        body = FormattedTextControl(self.body, get_cursor_position=self.body_cursor,
                                    show_cursor=False)
        self.body_window = Window(body, wrap_lines=True, height=Dimension(min=1, weight=1))
        app.layout.container = HSplit([
            Window(FormattedTextControl(self.heading), height=1, style="class:runtime.heading"),
            self.body_window,
            Window(FormattedTextControl(self.panel_text), height=self.panel_height,
                   style="class:runtime", wrap_lines=False),
            Window(FormattedTextControl(lambda: self.notice), height=1,
                   style="class:runtime", wrap_lines=False),
            editor.composer,
            ConditionalContainer(Window(FormattedTextControl(editor.toolbar), height=1),
                                 Condition(lambda: not editor.inline_toolbar)),
        ], height=lambda: Dimension.exact(max(5, app.output.get_size().rows - 1)))

    def capture(self, console: Console, ident: str) -> None:
        printer = console.print

        def write(*objects: Any, **kwargs: Any) -> None:
            with console.capture() as captured:
                printer(*objects, **kwargs)
            self.append(ident, captured.get())

        console.print = write  # type: ignore[method-assign]

    @property
    def selected(self) -> str:
        return self.editor.target_agent

    def append(self, ident: str, text: str) -> None:
        self.sync_session()
        if not text:
            return
        self.buffers[ident].append(text)
        del self.buffers[ident][:-400]
        self.formatted.pop(ident, None)
        if ident == self.selected:
            self.invalidate()

    def invalidate(self, *, immediate: bool = False) -> None:
        if immediate:
            if self.pending_render is not None:
                self.pending_render.cancel()
                self.pending_render = None
            self.editor.prompt.app.invalidate()
        elif self.pending_render is None:
            try:
                self.pending_render = asyncio.get_running_loop().call_later(0.1, self.flush)
            except RuntimeError:
                pass  # Initial synchronous construction; first read paints everything.

    def flush(self) -> None:
        self.pending_render = None
        self.editor.prompt.app.invalidate()

    def refresh_status(self) -> None:
        self.sync_session()
        value = self.heading() + self.panel()
        if value != self.last_status:
            self.last_status = value
            self.invalidate()

    def sync_session(self) -> None:
        ident = self.conversation().session.session_id
        if ident != self.session_identity:
            for child in self.children.values():
                child.stop_activity()
            self.buffers.clear()
            self.formatted.clear()
            self.previews.clear()
            self.activities.clear()
            self.children.clear()
            self.drafts.clear()
            self.loaded = {"root"}
            self.editor.target_agent = "root"
            self.picker, self.scroll_line = False, None
            self.session_identity = ident
            self.notice = "已切换会话 · F6 查看代理"

    def activity(self, ident: str, title: str = "", preview: str = "") -> None:
        changed = (self.activities.get(ident, ""), self.previews.get(ident, "")) != (title, preview)
        self.activities[ident], self.previews[ident] = title, preview
        # Incoming model tokens must not disturb a draft or a scrolled reading
        # position. The newest preview is included on the next user redraw.
        if changed and ident == self.selected and not self.editor.prompt.default_buffer.text:
            self.invalidate()

    def heading(self) -> str:
        if self.picker:
            return " Agents · " + ("历史" if self.history else "当前任务 / 待处理") + " · H 历史"
        if self.selected == "root":
            state = "执行中" if self.editor.live_submit is not None else "就绪"
            activity = self.activities.get("root", "")
            return f" Nexus · 主代理 · {state}" + (f" · {activity}" if activity else "")
        manager = self.conversation().coordinator
        agent = manager.agents.get(self.selected) if manager else None
        if agent is None:
            return " Nexus · 代理已不在当前会话 · Esc 返回主代理"
        mode = "可指导" if agent.accepting else "只读"
        return f" {agent.task_name} · {agent.role} {agent.id[:8]} · {agent.state} · {mode}"

    def choices(self) -> list[str]:
        manager = self.conversation().coordinator
        if manager is None:
            return ["root"]
        current = manager.current_agents()
        ids = [ident for ident in manager.agents if (ident not in current) == self.history]
        return ["root", *ids]

    def body(self) -> StyleAndTextTuples:
        if self.picker:
            manager = self.conversation().coordinator
            lines: StyleAndTextTuples = [("class:runtime",
                "↑/↓ 选择 · Enter 进入会话 · Esc 返回 · H 当前/历史\n\n")]
            for index, ident in enumerate(self.choices()):
                label = "主代理 root"
                if ident != "root" and manager is not None:
                    agent = manager.agents[ident]
                    label = f"{agent.task_name} · {agent.role} {ident[:8]} · {agent.state}"
                lines.append(("class:runtime.heading" if index == self.choice else "",
                              ("› " if index == self.choice else "  ") + label + "\n"))
            return lines
        if self.selected not in self.formatted:
            self.formatted[self.selected] = list(to_formatted_text(
                ANSI("".join(self.buffers[self.selected]))))
        result = list(self.formatted[self.selected])
        preview = self.previews.get(self.selected, "")
        if preview:
            result.append(("class:runtime", "\n" + preview[-3000:]))
        if not result:
            result = [("class:runtime", "等待这位代理的消息。" if self.selected != "root"
                       else "输入任务开始。F6 可查看和切换代理会话。")]
        return result

    def body_cursor(self) -> Point:
        if self.picker:
            return Point(x=0, y=self.choice + 2)
        text = "".join(fragment[1] for fragment in self.body())
        last = text.count("\n")
        return Point(x=len(text.rsplit("\n", 1)[-1]) if self.scroll_line is None else 0,
                     y=last if self.scroll_line is None else min(last, self.scroll_line))

    def page(self, direction: int) -> None:
        last = sum(fragment[1].count("\n") for fragment in self.body())
        info = self.body_window.render_info
        start = self.body_window.vertical_scroll
        step = max(1, (info.window_height if info else 10) // 2)
        self.scroll_line = max(0, min(last, start + direction * step))
        self.body_window.vertical_scroll = self.scroll_line
        if self.scroll_line >= last - (info.window_height if info else 1) + 1:
            self.scroll_line = None
        self.notice = ("阅读历史 · Ctrl+End 回到最新" if self.scroll_line is not None
                       else "已回到最新")
        self.invalidate(immediate=True)

    def panel(self) -> str:
        if self.picker:
            return ""
        if self.selected != "root":
            return "─ " + (self.activities.get(self.selected) or "F6 切换代理 · Esc 返回主代理")
        manager = self.conversation().coordinator
        panel = manager.panel() if manager else ""
        rows = panel.splitlines()
        maximum = self.panel_height()
        if len(rows) > maximum:
            rows = rows[:max(0, maximum - 1)] + ["… F6 查看全部代理"]
        title = self.activities.get("root", "")
        if not rows:
            rows = ["─ " + (title or ("Working" if self.editor.live_submit else "Ready"))]
        return "\n".join(rows)

    def panel_height(self) -> int:
        rows = self.editor.prompt.app.output.get_size().rows
        if self.picker or rows < 12:
            return 0
        if self.selected != "root":
            return 1
        manager = self.conversation().coordinator
        count = len(manager.panel().splitlines()) if manager else 1
        return min(max(1, count), max(1, min(6,
            self.editor.prompt.app.output.get_size().rows // 4)))

    def panel_text(self) -> StyleAndTextTuples:
        result: StyleAndTextTuples = []
        for index, row in enumerate(self.panel().splitlines()):
            style = "class:runtime.heading" if index == 0 else "class:runtime"
            if any(state in row for state in ("failed", "waiting_decision", "interrupted")):
                style = "class:runtime.attention"
            elif "completed" in row:
                style = "class:runtime.complete"
            result.append((style, ("\n" if index else "") + row))
        return result

    def controls(self) -> str:
        if self.picker:
            return "↑↓ 选择 · Enter 进入 · Esc 返回 · H 历史"
        manager = self.conversation().coordinator
        agent = manager.agents.get(self.selected) if manager else None
        if self.editor.live_submit is not None:
            action = "只读" if agent is not None and not agent.accepting else "Enter 指导"
            if self.editor.prompt.app.output.get_size().columns < 80:
                return f"{action} · Tab 排队 · F6 代理 · Esc 返回 · Ctrl+C 取消"
            return (f"→ {self.selected[:8]} · {action} · Tab 排队(主) · F6 代理 · "
                    "Esc 返回 · Ctrl+C 取消本轮")
        if agent is not None:
            return "只读历史 · Esc 返回主代理 · F6 代理 · PgUp 阅读"
        return "Enter 发送 · F6 代理 · PgUp 阅读 · F4 换行"

    def open_picker(self, *, history: bool = False) -> None:
        self.picker, self.history, self.choice = True, history, 0
        self.invalidate(immediate=True)

    async def select(self, ident: str) -> None:
        manager = self.conversation().coordinator
        if ident != "root" and (manager is None or ident not in manager.agents):
            self.notice = "找不到该代理。F6 选择有效代理。"
            self.invalidate(immediate=True)
            return
        buffer = self.editor.prompt.default_buffer
        active = self.editor.prompt.app.is_running and not self.editor.prompt.app.is_done
        self.drafts[self.selected] = (self.editor.expand(buffer.text) if active
                                     else self.editor.saved_draft)
        self.editor.clear()
        self.editor.target_agent = ident
        self.picker, self.scroll_line = False, None
        buffer.document = Document(self.drafts.get(ident, ""), len(self.drafts.get(ident, "")))
        if not active:
            self.editor.saved_draft = buffer.text
        self.notice = "Esc 返回主代理 · F6 切换 · PgUp/PgDn 阅读"
        if ident not in self.loaded and manager is not None:
            agent = manager.agents[ident]
            self.loaded.add(ident)
            if agent.conversation is not None:
                messages = list(agent.conversation.session.messages)
                for message in messages:
                    await self.child_event(ident, RuntimeEvent("message", "", ident, None,
                        {"role": message.role, "content": message.content,
                         "tool_calls": []}))
            else:
                from nexus.app.session import read_records

                def read() -> list[dict[str, Any]]:
                    try:
                        with manager.log_file(agent).open("rb") as stream:
                            rows, _ = read_records(stream)
                    except FileNotFoundError:
                        return []  # Agent creation can precede its first log write.
                    return rows
                for row in await asyncio.to_thread(read):
                    if row["kind"] == "message":
                        await self.child_event(ident, RuntimeEvent("message", row["timestamp"],
                            ident, row.get("run_id"), row["data"]))
            if not agent.accepting:
                self.notice = f"{agent.state} · 只读历史，已停止接收指导 · Esc 返回主代理"
        self.invalidate(immediate=True)

    async def child_event(self, ident: str, event: RuntimeEvent) -> None:
        if ident not in self.loaded:
            return  # Session history is the source until first opened.
        from nexus.app.tui import Transcript
        if ident not in self.children:
            console = Console(file=io.StringIO(), force_terminal=True, legacy_windows=False,
                              width=self.editor.prompt.app.output.get_size().columns)
            self.capture(console, ident)
            ui = Transcript(console)
            ui.editor, ui.agent_id = self.editor, ident
            self.children[ident] = ui
        if event.kind == "message" and event.data["role"] == "user":
            self.children[ident].user_message(event.data.get("content", ""))
        else:
            await self.children[ident](event)

    def inspect(self, text: str) -> None:
        self.notice = text.splitlines()[0] if text else ""
        self.append(self.selected, text + "\n")
        self.invalidate(immediate=True)

    def close(self) -> None:
        for child in self.children.values():
            child.stop_activity()
        if self.pending_render is not None:
            self.pending_render.cancel()
        self.console.print = self.original_print  # type: ignore[method-assign]
        # Preserve the root transcript in ordinary terminal scrollback on exit.
        self.original_print(Text.from_ansi("".join(self.buffers["root"])))
