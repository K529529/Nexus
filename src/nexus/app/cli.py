"""argparse entry; all task modes invoke the same Conversation/Agent Loop."""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from pathlib import Path

from prompt_toolkit import PromptSession
from rich.console import Console
from rich.text import Text

from nexus import __version__
from nexus.app.bootstrap import Conversation
from nexus.app.config import ConfigError, config_path, load_config, read_toml
from nexus.app.session import SessionError, list_sessions
from nexus.app.tui import Transcript, json_consumer, select_session, setup, terminal_text
from nexus.core.types import RunResult


async def drive(conversation: Conversation, text: str) -> RunResult:
    loop = asyncio.get_running_loop()
    # MCP async contexts must be opened and closed in the same owning task.
    # Keep turns in the CLI task across prompts; SIGINT still cancels the active turn.
    task = asyncio.current_task()
    assert task is not None
    cancelled = False
    active = True
    cancel_delivered = False

    def cancel_turn() -> None:
        nonlocal cancel_delivered
        if active:
            task.cancel()
            cancel_delivered = True

    def interrupt(signum: int, frame: object) -> None:
        nonlocal cancelled
        if not cancelled:
            cancelled = True
            # Wake the selector/proactor even when the signal interrupts an idle wait.
            loop.call_soon_threadsafe(cancel_turn)

    previous = signal.signal(signal.SIGINT, interrupt)
    try:
        return await conversation.turn(text)
    finally:
        active = False
        signal.signal(signal.SIGINT, previous)
        if cancel_delivered:
            task.uncancel()


async def application(args: argparse.Namespace) -> int:
    workspace = Path.cwd().resolve()
    interactive = args.command != "exec"
    if interactive and not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise ConfigError('Interactive mode requires a TTY; use nexus exec "task" [--json].')
    console = Console(highlight=False)
    prompt: PromptSession[str] | None = PromptSession() if interactive else None
    if interactive and not read_toml(config_path()).get("model"):
        assert prompt is not None
        if not await setup(config_path(), prompt.prompt_async, console):
            return 0
    config = load_config()
    if not interactive:
        consumer = json_consumer if args.json else Transcript(console)
        conversation = Conversation(workspace, config, consumer)
        try:
            result = await drive(conversation, args.task)
            return {"completed": 0, "failed": 1, "limited": 3, "aborted": 130}[result.outcome]
        finally:
            try:
                await conversation.close()
            finally:
                if isinstance(consumer, Transcript):
                    consumer.stop_activity()
    assert prompt is not None
    transcript = Transcript(console)
    conversation = Conversation(workspace, config, transcript)
    select = args.command == "resume"
    console.print("Nexus · trusted local execution · /help for commands")
    try:
        while True:
            if select:
                path = await select_session(list_sessions(workspace))
                select = False
                if path is not None:
                    await conversation.close()
                    conversation = Conversation(workspace, load_config(), transcript, resume=path)
                    for message in conversation.session.messages[-6:]:
                        if message.role in {"user", "assistant"} and message.content:
                            console.print(Text(f"{message.role}: {terminal_text(message.content)}"))
            try:
                text = (await prompt.prompt_async("You › ")).strip()
            except KeyboardInterrupt:
                continue
            except EOFError:
                return 0
            if not text:
                continue
            if text == "/exit":
                return 0
            if text == "/help":
                console.print(
                    "/resume select a session · /new new conversation · /exit quit. "
                    "Each ordinary input starts an isolated run; /resume continues the "
                    "selected session's unfinished run. "
                    "Ctrl+C aborts the current turn; at the prompt it clears input."
                )
            elif text == "/new":
                await conversation.close()
                conversation = Conversation(workspace, load_config(), transcript)
            elif text == "/resume":
                select = True
            else:
                result = await drive(conversation, text)
                if result.reason and "session_write_failed" in result.reason:
                    console.print("Session cannot continue; restart and resume the saved history.")
                    return 1
    finally:
        try:
            await conversation.close()
        finally:
            transcript.stop_activity()


def main() -> None:
    parser = argparse.ArgumentParser(prog="nexus", description="Nexus Next local coding agent")
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command")
    execute = subparsers.add_parser("exec", help="Run one coding task")
    execute.add_argument("task")
    execute.add_argument("--json", action="store_true", help="Public JSONL runtime events")
    subparsers.add_parser("resume", help="Select a local conversation in this workspace")
    args = parser.parse_args()
    try:
        code = asyncio.run(application(args))
    except (ConfigError, SessionError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        if isinstance(exc, ConfigError):
            print(
                "Configure ~/.nexus/config.toml with [model] name, context_window, base_url, "
                "and api_key_env. Set the key in that environment variable, not TOML. "
                "Example: $env:NEXUS_MODEL_API_KEY='<key>' (PowerShell) or "
                "export NEXUS_MODEL_API_KEY='<key>' (sh).",
                file=sys.stderr,
            )
        code = 2 if isinstance(exc, ConfigError) else 1
    except KeyboardInterrupt:
        code = 130
    raise SystemExit(code)
