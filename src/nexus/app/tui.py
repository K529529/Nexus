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
from rich.console import Console
from rich.syntax import Syntax
from rich.text import Text

from nexus.app.config import ConfigError, read_toml
from nexus.core.types import Json, RuntimeEvent, json_text

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
        # Only label an unambiguous single-file read; compound shell stays "Run".
        read = re.fullmatch(r"(?:Get-Content|cat)\s+([^\s;|&<>`$]+)", command, re.I)
        return short_line(f"Read {read[1]}" if read else f"Run {command}")
    return short_line("Apply patch" if data["name"] == "apply_patch" else data["name"])


class Transcript:
    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console(no_color="NO_COLOR" in os.environ, highlight=False)
        self.buffer = ""
        self.last_flush = 0.0
        self.assistant_streamed = False

    def flush(self) -> None:
        if self.buffer:
            self.console.print(Text(terminal_text(self.buffer)), end="", soft_wrap=True)
            self.buffer = ""
        self.last_flush = time.monotonic()

    async def __call__(self, event: RuntimeEvent) -> None:
        data, kind = event.data, event.kind
        if kind == "tool_output_delta":
            # Keep tool observations intact for the model/logs, out of the transcript.
            return
        if kind == "assistant_delta":
            self.assistant_streamed = True
            self.buffer += data["text"]
            if len(self.buffer) >= 2048 or time.monotonic() - self.last_flush >= 0.05:
                self.flush()
            return
        self.flush()
        if kind == "model_started":
            self.assistant_streamed = False
            self.console.print(Text("\nNexus · generating…", style="dim"))
            self.last_flush = 0.0
        elif kind == "tool_started":
            self.console.print(Text(f"\n› {tool_title(data)}", style="cyan"))
            self.last_flush = 0.0
        elif kind == "message" and data["role"] == "assistant":
            if data["content"] and not self.assistant_streamed:
                self.console.print(Text(terminal_text(data["content"])))
            else:
                self.console.print()
        elif kind == "message" and data["role"] == "tool":
            value = json.loads(data["content"])
            result = value["data"]
            files = result.get("files", [])
            for file in files[:3]:
                label = (
                    f"{file['status']}: {file['path']} "
                    f"(+{file['added_lines']} -{file['deleted_lines']})"
                )
                self.console.print(Text(short_line(label)))
                self.console.print(Syntax(output_preview(file["diff"], lines=6), "diff"))
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
            status = "ok" if value["ok"] else str(value["error_code"] or "failed")
            if "exit_code" in result:
                status += f" · exit={result['exit_code']}"
            if result.get("partial"):
                status += " · partial changes"
            if result.get("side_effects") == "unknown":
                status += " · side effects unknown"
            if value["truncated"]:
                status += " · tool result truncated"
            style = "dim" if value["ok"] else "yellow"
            self.console.print(Text(f"  {status} · {value['duration_ms']} ms", style=style))
        elif kind in {"warning", "context_compacted"}:
            self.console.print(
                Text(
                    terminal_text(data.get("detail", "Earlier context compacted.")), style="yellow"
                )
            )
        elif kind == "run_finished":
            total = data["usage"].get("total_tokens")
            suffix = data.get("reason") or ""
            label = (
                f"\n{data['outcome']} · tokens {total if total is not None else 'unknown'} "
                f"· {data['duration_ms'] / 1000:.1f}s {suffix}"
            )
            self.console.print(Text(label, style="dim"))


async def json_consumer(event: RuntimeEvent) -> None:
    print(json_text(asdict(event)), flush=True)


async def select_session(items: list[Json]) -> Path | None:
    if not items:
        Console().print("No sessions in this workspace.")
        return None
    selected = 0
    bindings = KeyBindings()

    def text() -> str:
        start = max(0, selected - 6)
        rows = ["Resume · ↑/↓ select · Enter open · Esc cancel"]
        for i in range(start, min(len(items), start + 12)):
            item = items[i]
            rows.append(
                ("› " if i == selected else "  ")
                + terminal_text(
                    f"{item['title']} · {item['project']} · {item['updated']} · {item['status']}"
                )
            )
        return "\n".join(rows)

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
