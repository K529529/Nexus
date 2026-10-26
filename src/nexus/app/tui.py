"""A scrolling transcript, async prompt, and small inline resume selector."""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from pathlib import Path

from prompt_toolkit.application import Application
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import Window
from prompt_toolkit.layout.controls import FormattedTextControl
from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.spinner import Spinner
from rich.syntax import Syntax
from rich.text import Text
from rich.theme import Theme

from nexus.app.config import ConfigError, read_toml
from nexus.app.input import PURPLE
from nexus.core.skills import active_skills
from nexus.core.types import Json, RuntimeEvent, Session, SkillInfo, json_text

ANSI = re.compile(r"\x1b\][^\x07]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]")


def terminal_text(text: str) -> str:
    clean = ANSI.sub("", text)
    return "".join(c for c in clean if c in "\n\t" or c.isprintable())


def short_line(text: str, width: int = 100) -> str:
    text = " ".join(terminal_text(text).split())
    return text if len(text) <= width else text[: width - 1] + "…"


def output_preview(text: str, *, lines: int = 4, tail: bool = False) -> str:
    rows = terminal_text(text).splitlines()
    visible = rows[-lines:] if tail else rows[:lines]
    preview = "\n".join(short_line(row) for row in visible)
    if len(rows) > lines:
        preview = "…\n" + preview if tail else preview + "\n…"
    return preview


def tool_title(data: Json) -> str:
    try:
        args = json.loads(data.get("arguments_json", "{}"))
    except ValueError:
        args = {}
    if data["name"] == "exec_command" and isinstance(args, dict):
        command = str(args.get("command", "")).strip()
        # Conservative display hints only, never shell parsing or execution policy.
        if not command or len(command) > 160 or re.search(r"[;|&<>`$\n\r]", command):
            return "Run shell command"
        parts = command.split(maxsplit=1)
        head, rest = parts[0].lower(), parts[1] if len(parts) > 1 else ""
        if head in {"get-content", "cat", "sed", "head", "tail"}:
            return short_line(f"Read {rest}")
        if head in {"select-string", "rg", "grep"}:
            return short_line(f"Search {rest}")
        if re.match(r"^(?:pytest|uv\s+run\s+pytest)(?:\s|$)", command, re.I):
            return short_line(f"Test {command}")
        if re.match(r"^git\s+(?:status|diff|log)(?:\s|$)", command, re.I):
            return short_line(f"Inspect {command}")
        return short_line(f"Run {command}")
    return short_line("Apply patch" if data["name"] == "apply_patch" else data["name"])


class Transcript:
    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console(no_color="NO_COLOR" in os.environ, highlight=False)
        self.buffer = ""
        self.last_flush = 0.0
        self.assistant_streamed = False
        self.tools: dict[str, str] = {}
        self.patch_previews: dict[tuple[str | None, str], set[int]] = {}
        self.activity: Live | None = None
        self.preview: Live | None = None
        self.preview_tail = ""
        self.finish_reason: str | None = None
        self.flush_handle: asyncio.TimerHandle | None = None
        self.working: Spinner | None = None

    @property
    def live_output(self) -> bool:
        return self.console.is_terminal and not self.console.is_dumb_terminal

    def stop_preview(self) -> None:
        if self.flush_handle is not None:
            self.flush_handle.cancel()
            self.flush_handle = None
        if self.preview is not None:
            self.preview.stop()
            self.preview = None
        self.working = None

    def stop_activity(self) -> None:
        self.stop_preview()
        if self.activity is not None:
            self.activity.stop()
            self.activity = None

    def tool_status(self, title: str, status: str, style: str) -> None:
        # Reserve terminal cells for status so successful calls stay on one physical row.
        suffix = Text(f"  {status}", style=style)
        label = Text(f"› {title}", style="cyan")
        label.truncate(max(1, self.console.width - suffix.cell_len), overflow="ellipsis")
        self.console.print(label + suffix, overflow="ellipsis", no_wrap=True)

    def flush(self) -> None:
        if self.flush_handle is not None:
            self.flush_handle.cancel()
            self.flush_handle = None
        if self.buffer:
            if self.live_output:
                # Bounded temporary content, classified only after a committed message.
                self.preview_tail = (self.preview_tail + self.buffer)[-4000:]
                rows = self.preview_tail.splitlines()[-10:]
                self.show_preview(Text("\n".join(rows), style="#b8b3c4 italic"))
            else:
                self.console.print(Text(terminal_text(self.buffer)), end="", soft_wrap=True)
            self.buffer = ""
        self.last_flush = time.monotonic()

    def show_preview(self, text: Text) -> None:
        if self.preview is None:
            self.working = Spinner("dots", text=Text("Working...", style="dim"), style=PURPLE)
            self.preview = Live(
                Group(self.working, text), console=self.console, transient=True,
                auto_refresh=True, refresh_per_second=8, vertical_overflow="ellipsis",
            )
            self.preview.start(refresh=True)
        else:
            assert self.working is not None
            self.preview.update(Group(self.working, text), refresh=False)

    def user_message(self, content: str) -> None:
        """A bounded, recognizable local echo; never changes the submitted message."""
        clean = terminal_text(content)
        width = min(100, self.console.width)
        # Bound both wrapping work and physical rows, while preserving useful whitespace.
        lines = Text(clean[:1200], tab_size=4).wrap(self.console, max(1, width - 4))
        preview = Text("\n").join(lines[:6])
        if len(clean) > 1200 or len(lines) > 6:
            preview.append(
                f"\n… {len(content.splitlines()):,} lines · {len(content):,} characters",
                style="dim",
            )
        self.console.print()
        if self.live_output and width >= 20:
            self.console.print(Panel(
                preview, title=Text("你", style="bold " + PURPLE), title_align="left",
                border_style=PURPLE, style="on #211b2e", box=box.ROUNDED,
                width=width, padding=(0, 1),
            ))
        else:
            self.console.print(Text("你 ›", style="bold " + PURPLE))
            self.console.print(preview)
        self.console.print()

    def assistant_message(self, data: Json) -> None:
        self.stop_preview()
        self.buffer = ""
        content = terminal_text(data.get("content", ""))
        if self.live_output:
            if content and (data.get("tool_calls") or self.finish_reason == "tool_calls"):
                # These are public Assistant Content, never private reasoning_content.
                self.console.print()
                self.console.print(Text("Thinking", style="#ece9f4 italic"))
                # Wrap before prefixing so every physical line retains its gutter.
                lines = Text(content).wrap(self.console, max(1, self.console.width - 2))
                block = Text(style="#b8b3c4 italic")
                for i, line in enumerate(lines):
                    if i:
                        block.append("\n")
                    block.append("│ ", style="#82798e")
                    block.append_text(line)
                self.console.print(block)
                self.console.print()
            elif content:
                with self.console.use_theme(Theme({
                    "markdown.h1": "bold " + PURPLE,
                    "markdown.h2": "bold " + PURPLE,
                    "markdown.h3": "bold " + PURPLE,
                    "markdown.link": "underline " + PURPLE,
                })):
                    self.console.print(Markdown(content, code_theme="monokai", style="#e5e7eb"))
        elif not self.assistant_streamed and content:
            self.console.print(Text(content))
        elif self.assistant_streamed:
            self.console.print()
        self.assistant_streamed = False
        self.preview_tail = ""

    def patch_preview(self, file: Json) -> None:
        label = (
            f"› {file['status'].capitalize()} {file['path']} "
            f"(+{file['added_lines']} -{file['deleted_lines']})"
        )
        self.console.print(Text(short_line(label)))
        if file.get("diff"):
            self.console.print(Syntax(output_preview(file["diff"], lines=6), "diff"))
        if file.get("omitted") or file.get("truncated"):
            self.console.print(Text("  … audit diff omitted/truncated", style="dim"))

    async def __call__(self, event: RuntimeEvent) -> None:
        data, kind = event.data, event.kind
        if kind == "tool_output_delta":
            # Keep tool observations intact for the model/logs, out of the transcript.
            return
        if kind == "assistant_delta":
            self.assistant_streamed = True
            self.buffer += terminal_text(data["text"])
            if self.live_output:
                self.buffer = self.buffer[-4000:]
            elapsed = time.monotonic() - self.last_flush
            if elapsed >= 0.1:
                self.flush()
            elif self.live_output and self.flush_handle is None:
                self.flush_handle = asyncio.get_running_loop().call_later(0.1 - elapsed, self.flush)
            return
        # Do not redraw the same preview for every usage/diagnostic event.
        if not self.live_output:
            self.flush()
        if kind == "approval_requested":
            self.stop_activity()
        elif kind == "memory_daily":
            status = data["status"]
            detail = (
                f"Preparing daily summary (up to {data['timeout_seconds']:g}s)…"
                if status == "summarizing" else f"Daily summary {status}."
            )
            self.console.print(Text(detail, style="dim"))
        elif kind == "model_started":
            self.stop_activity()
            self.assistant_streamed = False
            self.preview_tail = ""
            self.buffer = ""
            self.finish_reason = None
            self.last_flush = 0.0
            if self.live_output:
                self.show_preview(Text(""))
        elif kind == "model_finished":
            self.finish_reason = data.get("finish_reason")
        elif kind == "tool_started":
            self.stop_activity()
            title = tool_title(data)
            self.tools[data.get("call_id", "")] = title
            if self.console.is_terminal and not self.console.is_dumb_terminal:
                self.activity = Live(
                    Text(f"› {title} …", style="dim", overflow="ellipsis", no_wrap=True),
                    console=self.console,
                    transient=True,
                    auto_refresh=False,
                )
                self.activity.start(refresh=True)
            self.last_flush = 0.0
        elif kind == "patch_diff":
            self.stop_activity()
            key = (event.run_id, data["call_id"])
            previews = self.patch_previews.setdefault(key, set())
            index = data["file_index"]
            if 1 <= index <= 3 and index not in previews:
                self.patch_preview(data)
                previews.add(index)
        elif kind == "message" and data["role"] == "assistant":
            self.assistant_message(data)
        elif kind == "message" and data["role"] == "tool":
            self.stop_activity()
            value = json.loads(data["content"])
            result = value["data"]
            title = self.tools.pop(value["call_id"], "Tool")
            status = "✓" if value["ok"] else "✗ " + str(value["error_code"] or "failed")
            if not value["ok"] and "exit_code" in result:
                status += f" · exit {result['exit_code']}"
            status += f" · {value['duration_ms'] / 1000:.1f}s"
            self.tool_status(title, status, "dim" if value["ok"] else "yellow")
            key = (event.run_id, value["call_id"])
            previews = self.patch_previews.pop(key, set())
            if previews:
                omitted = max(0, result.get("changed_files", 0) - len(previews))
            else:
                # Old JSONL observations can still be rendered when no audit event
                # preceded them. New results have metadata but no diff body.
                files = result.get("files", [])
                for file in files[:3]:
                    self.patch_preview(file)
                omitted = max(0, len(files) - 3) + result.get("omitted_files", 0)
            if omitted:
                self.console.print(Text(f"  … {omitted} more changed files", style="dim"))
            if not value["ok"]:
                detail = (
                    result.get("detail")
                    or result.get("stderr")
                    or result.get("stdout")
                    or "\n".join(result.get("text", []))
                )
                if detail:
                    self.console.print(Text(output_preview(detail, tail=True), style="yellow"))
            notices = []
            if result.get("partial"):
                notices.append("partial changes")
            if result.get("side_effects") == "unknown":
                notices.append("side effects unknown")
            if value["truncated"]:
                notices.append("tool result truncated")
            if notices:
                self.console.print(Text("  " + " · ".join(notices), style="yellow"))
        elif kind == "context_projection":
            if "calibrated_context_tokens" in data:
                self.console.print(
                    Text(
                        f"上下文约 {data['calibrated_context_tokens']:,} tokens · "
                        f"压缩阈值 {data['compaction_trigger']:,}",
                        style="#b8b3c4 italic",
                    )
                )
        elif kind == "compaction_started":
            self.stop_activity()
            self.console.print(
                Text(f"正在压缩会话历史 · 之前约 {data['tokens_before']:,} tokens", style="dim")
            )
        elif kind == "compaction_usage":
            incoming = data.get("reported_input_tokens")
            outgoing = data.get("reported_output_tokens")
            self.console.print(
                Text(
                    f"压缩模型用量 · 输入 {incoming if incoming is not None else 'unknown'} · "
                    f"输出 {outgoing if outgoing is not None else 'unknown'}",
                    style="dim",
                )
            )
        elif kind == "context_compacted" and data.get("scope") == "session":
            self.stop_activity()
            self.console.print(
                Text(
                    f"压缩完成 · {data['tokens_before']:,} → {data['tokens_after']:,} tokens · "
                    f"近期历史 {data['recent_tokens_kept']:,} · 摘要 {data['summary_tokens']:,} · "
                    f"{data['duration_ms'] / 1000:.1f}s",
                    style="dim",
                )
            )
        elif kind == "memory_setup":
            self.console.print(Text(terminal_text(str(data.get("detail", ""))), style="dim"))
        elif kind == "memory_index" and data.get("status") == "ready":
            self.console.print("Memory Ready", style="dim")
        elif kind == "memory_index" and data.get("status") in {"failed", "unavailable"}:
            self.console.print(
                Text(
                    "记忆检索暂不可用；Markdown 读写仍可使用。/memory 查看状态。 "
                    + short_line(str(data.get("detail", ""))),
                    style="yellow",
                )
            )
        elif kind in {"warning", "context_compacted", "compaction_failed"}:
            self.stop_activity()
            self.console.print(
                Text(
                    terminal_text(data.get("detail", "Earlier context compacted.")), style="yellow"
                )
            )
        elif kind == "run_finished":
            self.stop_activity()
            if self.live_output and self.assistant_streamed:
                partial = (self.preview_tail + self.buffer)[-4000:]
                if partial:
                    self.console.print(Text(
                        "Interrupted preview (not a final answer):\n" + partial,
                        style="#b8b3c4 italic",
                    ))
            elif self.assistant_streamed:
                self.console.print()
            self.buffer = ""
            self.preview_tail = ""
            self.assistant_streamed = False
            self.tools.clear()
            self.patch_previews.clear()
            handoff = data.get("handoff")
            if handoff:
                self.console.print(
                    Text(
                        "本轮已停止，任务尚未完成最终交付；工具成功不等于任务已验证。",
                        style="yellow",
                    )
                )
                for fact in handoff.get("recent_tools", []):
                    status = "成功" if fact.get("ok") else str(fact.get("error_code") or "失败")
                    suffix = (
                        "；结果后尚无模型回复" if fact.get("no_subsequent_model_response") else ""
                    )
                    self.console.print(
                        Text(
                            short_line(
                                f"  {fact['tool']} · {status} · "
                                f"日志 seq {fact['source_seq']}{suffix}"
                            )
                        )
                    )
                for item in handoff.get("pending_plan", []):
                    self.console.print(Text(short_line("  待办（模型记录）：" + item["step"])))
                self.console.print(
                    Text(
                        "使用 /resume 或 nexus resume 选择本会话后继续；先检查实际状态，"
                        "尤其是副作用未知的操作。恢复不会自动重放工具。",
                        style="dim",
                    )
                )
            total = data["usage"].get("total_tokens")
            suffix = data.get("reason") or ""
            label = (
                f"\n{data['outcome']} · tokens {total if total is not None else 'unknown'} "
                f"· {data['duration_ms'] / 1000:.1f}s {suffix}"
            )
            self.console.print(Text(label, style="dim"))


async def json_consumer(event: RuntimeEvent) -> None:
    print(json_text(asdict(event)), flush=True)


def show_skills(session: Session, root: Path | None, warnings: list[str], console: Console) -> None:
    selected = session.selected_skill
    console.print(Text(f"Skills: {root}" if root else "Skills disabled for this environment"))
    console.print(
        Text("Explicit selection: " + (selected.name if selected else "off (automatic mode)"))
    )
    active = {s.name: s for s in active_skills(session)}
    catalog = {s.name: s for s in session.skill_catalog}
    for value in active.values():
        catalog.setdefault(value.name, SkillInfo(value.name, value.description))
    if not catalog:
        console.print("No skills installed. Add <name>/SKILL.md to the directory above.")
    for name, info in catalog.items():
        state = (
            "selected for session"
            if selected and name == selected.name
            else ("loaded in last/current run" if name in active else "available")
        )
        if name in active and name not in {s.name for s in session.skill_catalog}:
            state += "; saved snapshot, source unavailable"
        console.print(Text(f"  {name} [{state}] - {short_line(info.description)}"))
    for warning in warnings:
        console.print(Text(terminal_text(warning), style="yellow"))
    console.print("/skill <name> select or replace; /skill off cancel selection; /skills refresh.")
    console.print("Off restores automatic selection; model-loaded skills apply only to their run.")


async def select_session(items: list[Json]) -> Path | None:
    if not items:
        Console().print("No sessions in this workspace.")
        return None
    selected = 0
    bindings = KeyBindings()

    def text() -> str:
        start = (selected // 9) * 9
        rows = [
            "Recent sessions (newest first) · 1-9 open · up/down browse · Enter open · Esc cancel",
            "Completed/idle: new task; interrupted/limited/failed/aborted: continue saved run.",
        ]
        for i in range(start, min(len(items), start + 9)):
            item = items[i]
            rows.append(
                ("› " if i == selected else "  ")
                + terminal_text(
                    f"{i - start + 1}. {short_line(item['title'], 64)} | "
                    f"{item['updated'][:19].replace('T', ' ')} UTC | {item['status']} | "
                    f"{Path(item['path']).stem[:8]}"
                )
            )
        rows.append(f"Showing {start + 1}-{min(len(items), start + 9)} of {len(items)} sessions")
        return "\n".join(rows)

    for digit in "123456789":

        @bindings.add(digit)
        def numbered(event: KeyPressEvent) -> None:
            index = (selected // 9) * 9 + int(event.data) - 1
            if index < len(items):
                event.app.exit(result=Path(items[index]["path"]))

    @bindings.add("up")
    def up(event: KeyPressEvent) -> None:
        nonlocal selected
        selected = max(0, selected - 1)

    @bindings.add("down")
    def down(event: KeyPressEvent) -> None:
        nonlocal selected
        selected = min(len(items) - 1, selected + 1)

    @bindings.add("enter")
    def enter(event: KeyPressEvent) -> None:
        event.app.exit(result=Path(items[selected]["path"]))

    @bindings.add("escape")
    @bindings.add("c-c")
    def cancel(event: KeyPressEvent) -> None:
        event.app.exit(result=None)

    app: Application[Path | None] = Application(
        layout=Layout(Window(FormattedTextControl(text), wrap_lines=True)),
        key_bindings=bindings,
        full_screen=False,
    )
    return await app.run_async()


def save_model_config(path: Path, model: Json, original: str) -> None:
    current = path.read_text(encoding="utf-8") if path.exists() else ""
    if current != original:
        raise ConfigError("Configuration changed during setup; file was not overwritten")
    section = "[model]\n" + "".join(
        f"{key} = {json.dumps(value, ensure_ascii=False)}\n"
        for key, value in model.items()
        if value is not None
    )
    pattern = r"(?ms)^\s*\[model\][^\n]*\n.*?(?=^\s*\[|\Z)"
    updated, count = re.subn(pattern, lambda _: section + "\n", original, count=1)
    if not count:
        updated = original.rstrip() + "\n\n" + section
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, name = tempfile.mkstemp(prefix=".config-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(updated)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


async def setup(
    path: Path,
    ask: Callable[[str], Awaitable[str]],
    console: Console,
) -> bool:
    read_toml(path)
    original = await asyncio.to_thread(
        lambda: path.read_text(encoding="utf-8") if path.exists() else ""
    )
    console.print("Configure " + str(path) + ". Enter an API key variable name, not its value.")
    try:
        name = (await ask("Model name: ")).strip()
        base = (
            await ask("Base URL [https://api.openai.com/v1]: ")
        ).strip() or "https://api.openai.com/v1"
        window = int((await ask("Context window tokens: ")).strip())
        key_env = (
            await ask("API key variable [NEXUS_MODEL_API_KEY]: ")
        ).strip() or "NEXUS_MODEL_API_KEY"
        effort = (await ask("Reasoning effort [omit]: ")).strip() or None
        if not name or window <= 9216 or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key_env):
            raise ConfigError("Invalid model/window/environment variable; configuration not saved")
        model = {
            "name": name,
            "base_url": base,
            "context_window": window,
            "api_key_env": key_env,
            "reasoning_effort": effort,
        }
        console.print(Text(json.dumps(model, ensure_ascii=False, indent=2)))
        if (await ask("Save this user configuration? [y/N]: ")).strip().lower() != "y":
            return False
        save_model_config(path, model, original)
        return True
    except (EOFError, KeyboardInterrupt):
        return False
    except ValueError as exc:
        raise ConfigError("Invalid setup input; configuration not saved") from exc
