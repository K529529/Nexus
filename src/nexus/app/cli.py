"""argparse entry; all task modes invoke the same Conversation/Agent Loop."""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from typing import cast

from prompt_toolkit import PromptSession
from prompt_toolkit.application import get_app_session
from rich.console import Console
from rich.text import Text

from nexus import __version__
from nexus.app.bootstrap import Conversation
from nexus.app.config import (
    Config,
    ConfigError,
    config_path,
    load_config,
    memory_settings,
    read_toml,
    set_memory_enabled,
    validate_acl_authority,
)
from nexus.app.input import INPUT_HELP, TaskInput
from nexus.app.memory import MemoryStore
from nexus.app.memory_index import MemoryIndex, installed_command
from nexus.app.memory_setup import DependencyBootstrap
from nexus.app.profile import InteractionSummary, ProfileConsumer
from nexus.app.session import SessionError, list_sessions
from nexus.app.skills import SkillCatalog
from nexus.app.tui import (
    Transcript,
    confirm_workspace_acl,
    json_consumer,
    select_permissions,
    select_session,
    setup,
    show_skills,
    terminal_text,
)
from nexus.core.security import Mode
from nexus.core.types import Json, ModelError, RunResult, Session, json_text


async def authorize_workspace(
    workspace: Path, config: Config, *, interactive: bool
) -> Config | None:
    # Local/RO retain lazy execution and do not depend on Native consent/runtime.
    if sys.platform != "win32" or config.security.mode != "workspace-write":
        return config
    from nexus.app.acl_consent import ensure_workspace_consent

    policy = await ensure_workspace_consent(
        workspace,
        config.security,
        confirm=confirm_workspace_acl if interactive else None,
    )
    return replace(config, security=policy) if policy is not None else None


def memory_status(conversation: Conversation | None = None) -> Json:
    enabled, configured_command = memory_settings()
    store = MemoryStore(Path.home() / ".nexus" / "memory")
    index = conversation.memory_index if conversation else None
    snapshot = conversation.session.memory_snapshot if conversation else None
    raw = store.read(".index.json")
    try:
        saved = json.loads(raw) if raw else {"status": "not_started"}
    except ValueError:
        saved = {"status": "invalid", "detail": "Cannot parse saved .index.json"}
    return {
        "configured_enabled": enabled,
        "active_enabled": conversation.memory is not None if conversation else None,
        "search_state": "disabled"
        if (conversation.memory is None if conversation else not enabled)
        else index.state.get("status", "not_started")
        if index
        else "no_active_connection",
        "root": str(store.root),
        "qmd_configured_command": configured_command,
        "qmd_detected_command": installed_command(),
        "qmd_active_command": index.command if index else None,
        "pending": bool(store.read(".dirty")),
        "index": index.state if index else saved,
        "index_source": "active_conversation" if index else "last_saved_status",
        "snapshot": {
            "present": bool(snapshot and snapshot.get("content")),
            "tokens": (snapshot or {}).get("tokens", 0),
        },
    }


async def memory_command(action: str) -> int:
    if action == "status":
        print(json_text(memory_status()))
        return 0
    enabled = action == "on"
    set_memory_enabled(enabled)
    print(
        f"Memory {'ON' if enabled else 'OFF'} saved. Applies to new Sessions; "
        "other running processes are unchanged."
    )
    if not enabled:
        return 0
    _, configured = memory_settings()
    index = MemoryIndex(
        MemoryStore(Path.home() / ".nexus" / "memory"),
        configured,
        prepare=DependencyBootstrap().ensure if configured == ("qmd",) else None,
    )

    async def progress(kind: str, data: Json, **kwargs: object) -> int:
        if kind == "memory_setup":
            print(terminal_text(str(data.get("detail", ""))), flush=True)
        return 0

    try:
        index.start(progress)
        await index.wait_ready()
        print("Memory Ready")
        return 0
    except (OSError, RuntimeError, TimeoutError) as exc:
        print("Memory ON is saved; search temporarily unavailable: " + terminal_text(str(exc)))
        return 1
    finally:
        await index.close()


async def drive(
    conversation: Conversation,
    text: str,
    *,
    compact: bool = False,
    editor: TaskInput | None = None,
) -> RunResult:
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
    old_approver = conversation.gateway.approver if editor is not None else None
    if editor is not None:
        from nexus.app.events import redact
        from nexus.core.security import ApprovalDecision, ApprovalRequest

        async def approve(request: ApprovalRequest) -> ApprovalDecision:
            # Transcript spinners are stopped by the approval_requested event.
            preview = str(redact(request.arguments_preview, conversation.secret_values()))
            if len(preview) > 2000:
                preview = preview[:2000] + "\n[Preview truncated; review the full tool call.]"
            Console(highlight=False).print(
                Text(
                    terminal_text(
                        str(
                            redact(
                                f"Approval: {request.tool_name}\n{preview}\n"
                                f"Scope: {request.capability_scope}\n"
                                "Type allow, deny or cancel, then Enter (Ctrl+C cancels).",
                                conversation.secret_values(),
                            )
                        )
                    )
                )
            )
            choice = await editor.approve()
            return ApprovalDecision(
                request.request_id,
                "allow_once" if choice == "allow" else "cancel" if choice == "cancel" else "deny",
            )

        conversation.gateway.approver = approve
    try:
        guard = editor.busy(lambda: interrupt(signal.SIGINT, None)) if editor else nullcontext()
        with guard:
            if compact:
                await conversation.compact()
                return RunResult("completed")
            return await conversation.turn(text)
    finally:
        if editor is not None:
            conversation.gateway.approver = old_approver
        active = False
        signal.signal(signal.SIGINT, previous)
        if cancel_delivered:
            task.uncancel()


async def application(args: argparse.Namespace) -> int:
    if args.command == "memory":
        return await memory_command(getattr(args, "action", "status"))
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
        eval_config = load_config()
        if getattr(args, "permission_mode", None) is not None:
            eval_config = replace(
                eval_config, security=replace(eval_config.security, mode=args.permission_mode)
            )
        return await evaluate(selected, eval_config)
    workspace_input = Path.cwd()
    workspace = workspace_input.resolve()
    interactive = args.command != "exec"
    if interactive and not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise ConfigError('Interactive mode requires a TTY; use nexus exec "task" [--json].')
    console = Console(highlight=False)
    # Explicit output keeps our bindings active even under TERM=dumb; otherwise
    # PromptSession silently substitutes its separate single-line dumb prompt.
    prompt: PromptSession[str] | None = (
        PromptSession(output=get_app_session().output) if interactive else None
    )
    if interactive and not read_toml(config_path()).get("model"):
        assert prompt is not None
        if not await setup(config_path(), prompt.prompt_async, console):
            return 0
    permission_mode = getattr(args, "permission_mode", None)

    def current_config(*, require_key: bool = True) -> Config:
        loaded = load_config() if require_key else load_config(require_key=False)
        if permission_mode is not None:
            loaded = replace(loaded, security=replace(loaded.security, mode=permission_mode))
        validate_acl_authority(loaded.security)
        return loaded

    authorized = await authorize_workspace(
        workspace_input, current_config(), interactive=interactive
    )
    if authorized is None:
        console.print("Workspace ACL consent denied/cancelled. No command executed.")
        return 0
    config = authorized
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
    summary = InteractionSummary(profile or transcript)
    editor = TaskInput(prompt)
    conversation = Conversation(workspace, config, summary)
    select = args.command == "resume"
    exit_reason: str | None = None
    console.print(f"Nexus · execution mode: {config.security.mode} · /help for commands")
    console.print(
        Text(
            INPUT_HELP,
            style="dim",
        )
    )
    console.print(
        Text(
            "No bracketed paste? Press F2 before pasting, F2 to finish, then Enter to send.",
            style="dim",
        )
    )
    try:
        await conversation.start_memory()
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
                        candidate = Conversation(workspace, current_config(), summary, resume=path)
                    except (SessionError, OSError, ValueError) as exc:
                        console.print(Text(terminal_text(str(exc)), style="yellow"))
                        if same:
                            conversation = Conversation(workspace, current_config(), summary)
                        continue
                    await conversation.close()
                    conversation = candidate
                    await conversation.start_memory()
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
                draft = await editor.read()
                text = draft.strip()
            except KeyboardInterrupt:
                exit_reason = "ctrl-c"
                return 0
            except EOFError:
                exit_reason = "eof"
                return 0
            if not text:
                continue
            if text == "/exit":
                exit_reason = "slash-exit"
                return 0
            if text == "/help":
                console.print(
                    "/skills list skills · /skill <name> select · /skill off cancel selection. "
                    "/resume recent sessions · /compact summarize history · "
                    "/memory [on|off|status] · /permissions view or change execution mode · "
                    "/new new conversation · /exit quit. "
                    "Each input starts a new run with continuous session history; "
                    "/resume continues the "
                    "selected session's unfinished run. "
                    "Enter sends; Ctrl+J/F4 inserts a newline. F2 toggles safe paste capture; "
                    "F3 expands a paste for editing. "
                    "Ctrl+C cancels the run, clears a draft, or exits an empty prompt. "
                    "Input typed during execution/cleanup is discarded."
                )
            elif text == "/permissions":
                backend = conversation.gateway.backend
                selected_mode = await select_permissions(
                    conversation.config.security.mode,
                    getattr(backend, "status", "local"),
                    getattr(backend, "detail", "Commands run as your current user."),
                )
                if selected_mode is not None and selected_mode != conversation.config.security.mode:
                    try:
                        proposed = current_config()
                        proposed = replace(
                            proposed,
                            security=replace(proposed.security, mode=cast(Mode, selected_mode)),
                        )
                        validate_acl_authority(proposed.security)
                        authorized = await authorize_workspace(
                            workspace_input,
                            proposed,
                            interactive=True,
                        )
                    except (ConfigError, OSError) as exc:
                        console.print(Text(terminal_text(str(exc)), style="yellow"))
                        continue
                    if authorized is None:
                        console.print("Workspace ACL consent denied/cancelled. Mode unchanged.")
                        continue
                    # The command runs only while the editor is idle. Closing first releases
                    # the old sandbox; no pending approvals or historical grants are copied.
                    await conversation.close()
                    permission_mode = selected_mode
                    config = authorized
                    conversation = Conversation(workspace, config, summary)
                    await conversation.start_memory()
                    console.print(
                        Text(
                            f"Mode: {selected_mode}. New conversation; previous history saved. "
                            "Mode is session-only; approved ACL roots are remembered. "
                            "Backend initializes on first tool call."
                        )
                    )
            elif text == "/new":
                await conversation.close()
                conversation = Conversation(workspace, current_config(), summary)
                await conversation.start_memory()
            elif text == "/resume":
                select = True
            elif text == "/compact":
                try:
                    await drive(conversation, "", compact=True, editor=editor)
                except (ModelError, ConfigError) as exc:
                    console.print(Text(terminal_text(str(exc)), style="yellow"))
            elif text == "/memory" or text.startswith("/memory "):
                action = text.removeprefix("/memory").strip() or "status"
                if action not in {"status", "on", "off"}:
                    console.print("Use /memory [on|off|status].")
                    continue
                if action == "status":
                    console.print(Text(json_text(memory_status(conversation))))
                    continue
                enabled = action == "on"
                try:
                    updated_config = replace(
                        current_config(require_key=False), memory_enabled=enabled
                    )
                    set_memory_enabled(enabled)
                except (OSError, ConfigError) as exc:
                    console.print(Text(terminal_text(str(exc)), style="yellow"))
                    continue
                retry = (
                    enabled
                    and conversation.memory_index is not None
                    and (conversation.memory_index.state.get("status") in {"failed", "unavailable"})
                )
                if enabled != conversation.config.memory_enabled or retry:
                    # A fresh boundary prevents old tool results/summaries leaking memory while OFF.
                    await conversation.close()
                    conversation = Conversation(workspace, updated_config, summary)
                    await conversation.start_memory()
                    console.print(
                        f"Memory {'ON' if enabled else 'OFF'} saved. New Session opened; "
                        "effective for the next Run. Previous history is preserved."
                    )
                else:
                    await conversation.start_memory()
                    console.print(
                        f"Memory {'ON' if enabled else 'OFF'} saved; already effective "
                        "in this Session."
                    )
                if enabled:
                    console.print(
                        "Search becomes available when Memory Ready is shown; "
                        "coding remains available."
                    )
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
                transcript.user_message(draft)
                result = await drive(conversation, draft, editor=editor)
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
        # prompt_async has already restored terminal input; stop any Rich live
        # output before the bounded summary. Session switches use plain close().
        transcript.stop_activity()
        previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            with editor.busy(lambda: None):
                try:
                    if exit_reason is not None:
                        # Preserve the existing once-only, bounded Memory exit hook.
                        # The interaction statistics below make no model request.
                        await conversation.summarize_exit(exit_reason)
                finally:
                    await conversation.close()
        finally:
            signal.signal(signal.SIGINT, previous)
            summary.render(console)


def main() -> None:
    parser = argparse.ArgumentParser(prog="nexus", description="Nexus Next local coding agent")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "--profile", action="store_true", help="Show a developer report after each run"
    )
    parser.add_argument("--skill", help="Select a local Skill for this session")
    subparsers = parser.add_subparsers(dest="command")
    memory = subparsers.add_parser("memory", help="Inspect memory/index state without a model call")
    memory.add_argument("action", choices=["on", "off", "status"], default="status", nargs="?")
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
    for target in (parser, execute, resume, evaluation):
        target.add_argument(
            "--permission-mode",
            choices=["local", "read-only", "workspace-write"],
            default=argparse.SUPPRESS,
            help="Temporary execution mode; overrides user security.mode",
        )
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
                "Configure ~/.nexus/config.toml with [model] name, base_url, "
                "and api_key_env. Set the key in that environment variable, not TOML. "
                "context_window defaults to 128000 in each model section. "
                "Example: $env:NEXUS_MODEL_API_KEY='<key>' (PowerShell) or "
                "export NEXUS_MODEL_API_KEY='<key>' (sh).",
                file=sys.stderr,
            )
        code = 2 if isinstance(exc, ConfigError) else 1
    except KeyboardInterrupt:
        code = 130
    raise SystemExit(code)
