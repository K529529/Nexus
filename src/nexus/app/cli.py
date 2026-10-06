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
from nexus.app.profile import ProfileConsumer
from nexus.app.session import SessionError, list_sessions
from nexus.app.skills import SkillCatalog
from nexus.app.tui import (
    Transcript,
    json_consumer,
    select_session,
    setup,
    show_skills,
    terminal_text,
)
from nexus.core.types import RunResult, Session


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
    if args.command == "skills":
        listing = SkillCatalog(Path.home() / ".nexus" / "skills")
        show_skills(
            Session(Path.cwd(), skill_catalog=listing.items),
            listing.root,
            listing.warnings,
            Console(highlight=False),
        )
        return 0
    if args.command == "eval":
        from nexus.evaluation.cases import CASE_IDS, load_case
        from nexus.evaluation.runner import evaluate

        selected = list(CASE_IDS) if args.all else [args.case]
        for case_id in selected:
            load_case(case_id)
        return await evaluate(selected, load_config())
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
        profile = ProfileConsumer(consumer) if getattr(args, "profile", False) else None
        conversation = Conversation(workspace, config, profile or consumer)
        try:
            if getattr(args, "skill", None):
                await conversation.select_skill(args.skill)
            result = await drive(conversation, args.task)
            if profile:
                profile.render(
                    console,
                    conversation.config.limits,
                    conversation.writer.path if conversation.writer else None,
                )
            return {"completed": 0, "failed": 1, "limited": 3, "aborted": 130}[result.outcome]
        finally:
            try:
                await conversation.close()
            finally:
                if isinstance(consumer, Transcript):
                    consumer.stop_activity()
    assert prompt is not None
    transcript = Transcript(console)
    profile = ProfileConsumer(transcript) if getattr(args, "profile", False) else None
    conversation = Conversation(workspace, config, profile or transcript)
    select = args.command == "resume"
    console.print("Nexus · trusted local execution · /help for commands")
    try:
        if getattr(args, "skill", None):
            await conversation.select_skill(args.skill)
            console.print(Text(f"Skill selected for this session: {args.skill}"))
        while True:
            if select:
                path = await select_session(list_sessions(workspace))
                select = False
                if path is not None:
                    # Opening another history must not discard the current session on failure.
                    same = getattr(conversation, "writer", None)
                    same = same is not None and same.path.resolve() == path.resolve()
                    if same:
                        await conversation.close()
                    try:
                        candidate = Conversation(
                            workspace, load_config(), profile or transcript, resume=path
                        )
                    except (SessionError, OSError, ValueError) as exc:
                        console.print(Text(terminal_text(str(exc)), style="yellow"))
                        if same:
                            conversation = Conversation(
                                workspace, load_config(), profile or transcript
                            )
                        continue
                    await conversation.close()
                    conversation = candidate
                    console.print(
                        "Session opened. Enter a task or continuation; no saved tool is replayed."
                    )
                    if conversation.session.selected_skill:
                        console.print(
                            Text(f"Restored Skill: {conversation.session.selected_skill.name}")
                        )
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
                    "/skills list skills · /skill <name> select · /skill off cancel selection. "
                    "/resume recent sessions · /new new conversation · /exit quit. "
                    "Each ordinary input starts an isolated run; /resume continues the "
                    "selected session's unfinished run. "
                    "Ctrl+C aborts the current turn; at the prompt it clears input."
                )
            elif text == "/new":
                await conversation.close()
                conversation = Conversation(workspace, load_config(), profile or transcript)
            elif text == "/resume":
                select = True
            elif text == "/skills" or text == "/skill":
                conversation.refresh_skills()
                catalog = conversation.catalog
                show_skills(
                    conversation.session,
                    catalog.root if catalog else None,
                    catalog.warnings if catalog else [],
                    console,
                )
            elif text.startswith("/skill "):
                name = text[len("/skill ") :].strip()
                try:
                    await conversation.select_skill(None if name == "off" else name)
                    console.print(
                        Text(
                            "Explicit Skill selection cleared; automatic mode remains available."
                            if name == "off"
                            else f"Skill selected for this session: {name}"
                        )
                    )
                except ValueError as exc:
                    console.print(Text(terminal_text(str(exc)), style="yellow"))
            elif text.startswith("/"):
                console.print(
                    "Unknown command. Use /help; slash commands are not sent to the model."
                )
            else:
                if profile:
                    profile.reset()
                result = await drive(conversation, text)
                if profile:
                    profile.render(
                        console,
                        conversation.config.limits,
                        conversation.writer.path if conversation.writer else None,
                    )
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
    parser.add_argument(
        "--profile", action="store_true", help="Show a developer report after each run"
    )
    parser.add_argument("--skill", help="Select a local Skill for this session")
    subparsers = parser.add_subparsers(dest="command")
    skills = subparsers.add_parser("skills", help="List local Skills without a model connection")
    skills.add_argument("action", nargs="?", choices=["list"], default="list")
    evaluation = subparsers.add_parser("eval", help="Run fixed Next Dev Set V0 cases")
    evaluation.add_argument("case", nargs="?")
    evaluation.add_argument("--all", action="store_true", help="Run all eight cases sequentially")
    execute = subparsers.add_parser("exec", help="Run one coding task")
    execute.add_argument("task")
    execute.add_argument("--skill", default=argparse.SUPPRESS, help="Select a Skill for this task")
    output = execute.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true", help="Public JSONL runtime events")
    output.add_argument(
        "--profile",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Show a developer run report",
    )
    resume = subparsers.add_parser("resume", help="Select a local conversation in this workspace")
    resume.add_argument(
        "--profile",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Show a developer report after each run",
    )
    args = parser.parse_args()
    if args.skill and args.command in {"eval", "resume", "skills"}:
        parser.error("--skill applies to interactive startup or exec; use /skill after resume")
    if args.command == "eval" and bool(args.case) == args.all:
        parser.error("eval requires either a case ID or --all")
    if args.command == "exec" and args.profile and args.json:
        parser.error("--profile and --json are mutually exclusive")
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
