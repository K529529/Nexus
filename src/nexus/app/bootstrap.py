"""One composition path shared by interactive, exec and evaluation callers."""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path

from nexus.app import memory_daily
from nexus.app.config import Config
from nexus.app.events import Events, redact
from nexus.app.memory import MemoryStore
from nexus.app.memory_context import root_id, snapshot
from nexus.app.memory_index import MemoryIndex
from nexus.app.memory_setup import DependencyBootstrap
from nexus.app.session import SessionError, SessionLog, resume_session
from nexus.app.skills import SkillCatalog
from nexus.core.agent import run_turn
from nexus.core.compaction import provenance
from nexus.core.context import Context, ContextBuilder, instructions
from nexus.core.model import ChatModel, CompactionModel
from nexus.core.security import SecurityGateway
from nexus.core.skills import check_active
from nexus.core.tokens import TokenEstimator
from nexus.core.types import (
    Json,
    Message,
    ModelError,
    RunResult,
    RuntimeEvent,
    Session,
    Tool,
    json_text,
)
from nexus.tools.mcp import connect_servers
from nexus.tools.memory import create_memory_tools
from nexus.tools.registry import default_tools, permission_tool
from nexus.tools.skills import create_load_skill_tool


class Conversation:
    def __init__(
        self,
        workspace: Path,
        config: Config,
        consumer: Callable[[RuntimeEvent], Awaitable[None]],
        *,
        home: Path | None = None,
        resume: Path | None = None,
        registry: dict[str, Tool] | Callable[[Session], dict[str, Tool]] | None = None,
        environment_display: tuple[str, str] | None = None,
        skill_directory: Path | None = None,
    ) -> None:
        self.workspace = workspace.resolve()
        self.config, self.consumer, self.home = config, consumer, home
        self.gateway = SecurityGateway(config.security)
        if config.security.mode != "local":
            # No Node import/process in Local; restricted startup stays lazy.
            from nexus.tools.sandbox import SRTBackend

            self.gateway.backend = SRTBackend(config.security)
        self.session = Session(self.workspace)
        self.writer: SessionLog | None = None
        self.events: Events | None = None
        self.warnings: list[str] = []
        self.resumed = resume is not None
        self.model: ChatModel | None = None
        self.compaction_model: CompactionModel | None = None
        compact = config.compaction_model
        self.context = Context(
            config.limits,
            compaction_window=compact.context_window if compact else 128000,
            summary_tokens=compact.max_output_tokens if compact else 4096,
            secrets=self.secret_values(),
            main_estimator=TokenEstimator(
                config.model.name,
                config.model.base_url,
                reasoning_effort=config.model.reasoning_effort,
                enable_thinking=config.model.enable_thinking,
            ),
            compaction_estimator=TokenEstimator(
                compact.name,
                compact.base_url,
                reasoning_effort=compact.reasoning_effort,
                enable_thinking=compact.enable_thinking,
            )
            if compact
            else None,
        )
        if config.compaction_model is None:
            self.warnings.append(
                "Missing [compaction_model]: automatic and /compact summarization require "
                "an independent model and API-key environment variable. Legacy configuration "
                "can run while context fits; no main-model fallback is used."
            )
        self.environment_display = environment_display
        self.resources = AsyncExitStack()
        self.unavailable: list[str] = []
        self.closed = False
        self.exit_summary_attempted = False
        if resume:
            self.session, self.writer, self.warnings = resume_session(resume, self.workspace, home)
            if not config.memory_enabled and (
                (self.session.memory_snapshot or {}).get("content")
                or any(
                    call.name
                    in {
                        "memory_read",
                        "memory_write",
                        "memory_search",
                        "memory_forget",
                        "memory_restore",
                        "scratchpad",
                    }
                    for message in self.session.messages
                    for call in message.tool_calls
                )
            ):
                self.writer.close()
                raise SessionError(
                    "Memory is OFF. This saved session contains memory context. "
                    "Use a new session, or /memory on before resuming it; history is preserved."
                )
        if config.security.mode != "local" and not config.security.allow_trusted_skills:
            # Historical Skill selections are context, not authorization to reload a source.
            self.session.selected_skill = None
            self.session.loaded_skills = ()
        resolved = registry(self.session) if callable(registry) else registry
        self.base_registry = dict(resolved) if resolved is not None else default_tools(self.session)
        if config.security.mode != "local" and resolved is None:
            self.base_registry["request_permissions"] = permission_tool()
        self.registry = dict(self.base_registry)
        # Custom/evaluation registries never inherit the operator's global memory.
        self.memory = (
            MemoryStore((home or Path.home() / ".nexus") / "memory")
            if registry is None and config.memory_enabled
            else None
        )
        self.memory_index = (
            MemoryIndex(
                self.memory,
                config.memory_qmd_command,
                prepare=DependencyBootstrap(home / "dependencies" if home else None).ensure
                if config.memory_qmd_command == ("qmd",)
                else None,
            )
            if self.memory is not None and config.security.mode == "local"
            else None
        )
        if self.memory is not None and self.memory_index is not None:
            self.context.before_compact = self.before_compact
            self.base_registry.update(
                create_memory_tools(
                    self.memory,
                    search=self.memory_index.search,
                    changed=self.memory_index.changed,
                    invalidate=self.refresh_memory,
                )
            )
            self.registry.update(self.base_registry)
        elif self.memory is not None:
            # Explicit per-call host service only: no index, download, snapshot or daily writer.
            self.base_registry.update(create_memory_tools(self.memory))
            self.registry.update(self.base_registry)
        # Custom registries (including fixed eval) opt in explicitly; never inherit host Skills.
        self.catalog = (
            SkillCatalog(
                skill_directory or (home or Path.home() / ".nexus") / "skills",
                lambda value: str(redact(value, self.secret_values())),
            )
            if (registry is None or skill_directory is not None)
            and (config.security.mode == "local" or config.security.allow_trusted_skills)
            else None
        )
        self.skill_tool: Tool | None = None
        self.refresh_skills()

    def secret_values(self) -> tuple[str, ...]:
        values = [os.environ.get(self.config.model.api_key_env, "")]
        if self.config.compaction_model is not None:
            values.append(os.environ.get(self.config.compaction_model.api_key_env, ""))
        for raw in self.config.mcp_servers.values():
            if isinstance(raw, dict) and isinstance(raw.get("env_from"), dict):
                values.extend(
                    os.environ.get(n, "") for n in raw["env_from"].values() if isinstance(n, str)
                )
        return tuple(values)

    def ensure_events(self, title: str) -> None:
        if self.closed:
            raise RuntimeError("Conversation is closed")
        if self.writer is None:
            self.writer = SessionLog.create(
                self.session,
                redact(title, self.secret_values()),
                asdict(self.config.model),
                self.home,
            )
        self.events = Events(self.session, self.writer, self.consumer, self.secret_values())

    async def start_memory(self) -> None:
        """Start once per Conversation; dependency failure never blocks the coding loop."""
        if self.memory_index is not None:
            self.ensure_events("Memory startup")
            assert self.events is not None
            self.memory_index.start(self.events)

    def refresh_skills(self) -> None:
        if self.catalog is None:
            return
        self.catalog.refresh()
        self.session.skill_catalog = self.catalog.items
        if self.skill_tool is not None:
            self.base_registry.pop("load_skill", None)
            self.registry.pop("load_skill", None)
            self.skill_tool = None
        if self.catalog.items:
            if "load_skill" in self.registry:
                raise ValueError("load_skill conflicts with an existing tool")
            self.skill_tool = create_load_skill_tool(self.session, self.catalog.load)
            self.base_registry["load_skill"] = self.registry["load_skill"] = self.skill_tool

    async def select_skill(self, name: str | None) -> None:
        if self.catalog is None and name is not None:
            raise ValueError("Skills are disabled for this execution environment")
        value = self.catalog.load(name) if name is not None and self.catalog else None
        loaded = (
            self.session.loaded_skills if self.session.skills_run_id == self.session.run_id else ()
        )
        check_active(tuple({s.name: s for s in (*loaded, *((value,) if value else ()))}.values()))
        self.ensure_events("Skill selection")
        assert self.events is not None
        run_id = self.session.run_id
        self.session.run_id = None
        try:
            await self.events("skill_selected", {"skill": asdict(value) if value else None})
            self.session.selected_skill = value
        finally:
            self.session.run_id = run_id

    async def turn(self, text: str) -> RunResult:
        if self.closed:
            raise RuntimeError("Conversation is closed")
        self.session.run_id = None
        prefix = instructions(
            self.workspace,
            self.config.limits.shell,
            display=self.environment_display,
            response_language=self.config.response_language,
        )
        self.config.model.key()  # Validate credentials only when actually running a task.
        self.ensure_events(text)
        assert self.events is not None
        prefix += await self.memory_prefix()
        await self.start_memory()
        for warning in self.warnings:
            await self.events("warning", {"detail": warning})
        self.warnings.clear()
        previous = next((m.content for m in self.session.messages if m.role == "system"), None)
        if previous != prefix:
            if previous is not None:
                await self.events(
                    "warning", {"detail": "Current project/environment instructions changed."}
                )
            seq = await self.events("instructions", {"content": prefix})
            self.session.messages = [Message("system", prefix, seq=seq)] + [
                m for m in self.session.messages if m.role != "system"
            ]
        try:
            if self.model is None:
                self.model = ChatModel(self.config.model)
                self.resources.push_async_callback(self.model.close)
                try:
                    if self.config.compaction_model is not None and self.compaction_model is None:
                        self.compaction_model = CompactionModel(self.config.compaction_model)
                        self.resources.push_async_callback(self.compaction_model.close)
                    if self.config.security.mode == "local":
                        self.unavailable = await connect_servers(
                            self.config.mcp_servers, self.registry, self.resources, self.events
                        )
                    else:
                        # Connecting stdio MCP can itself spawn arbitrary host processes.
                        self.unavailable = list(self.config.mcp_servers)
                except BaseException:
                    # A partially opened conversation must not leak client/process handles.
                    await self.resources.aclose()
                    self.model = None
                    self.compaction_model = None
                    self.registry = dict(self.base_registry)
                    raise
            fingerprint = hashlib.sha256(
                json_text(
                    {
                        "model": asdict(self.config.model),
                        "compaction_model": asdict(self.config.compaction_model)
                        if self.config.compaction_model
                        else None,
                        "response_language": self.config.response_language,
                        "security": asdict(self.config.security),
                        "tools": [asdict(t.spec) for t in self.registry.values()],
                        **provenance(),
                    }
                ).encode()
            ).hexdigest()
            if self.resumed:
                await self.events(
                    "warning",
                    {
                        "detail": (
                            "Resuming the unfinished run with current instructions/model/tools. "
                            if self.session.resume_run_id
                            else "Next input starts a new run with continuous session history. "
                        )
                        + "Saved tool calls are never replayed."
                    },
                )
                self.resumed = False
            if self.catalog is not None and (self.catalog.items or self.catalog.warnings):
                await self.events(
                    "skill_catalog", {"skills": [asdict(s) for s in self.catalog.items]}
                )
                for warning in self.catalog.warnings:
                    await self.events("warning", {"detail": warning})
            await self.events(
                "configuration",
                {
                    "fingerprint": fingerprint,
                    "model": asdict(self.config.model),
                    "compaction_model": asdict(self.config.compaction_model)
                    if self.config.compaction_model
                    else None,
                    "response_language": self.config.response_language,
                    "security": asdict(self.config.security),
                    **provenance(),
                },
            )
            environment = (
                "[Environment fact] MCP servers "
                + ", ".join(self.unavailable)
                + " are unavailable in this conversation; their tools did not run."
                if self.unavailable
                else ""
            )
            if self.config.security.mode != "local":
                environment += (
                    f"\n[Execution mode] {self.config.security.mode}. "
                    "Shell/patch run through SRT; initialization errors never fall back to Local. "
                    "No fallback to local execution. "
                    "MCP and background memory indexing are disabled."
                )
            result = await run_turn(
                self.session,
                text,
                self.model,
                self.registry,
                self.events,
                self.config.limits,
                environment=environment,
                compaction_model=self.compaction_model,
                context=self.context,
                gateway=self.gateway,
            )
            if result.outcome == "completed":
                try:
                    await self.compact(force=False)
                except ModelError as exc:
                    await self.events(
                        "warning",
                        {
                            "detail": (
                                f"Between-run compaction unavailable ({exc.code}); "
                                "history retained."
                            )
                        },
                    )
            return result
        except asyncio.CancelledError:
            result = RunResult("aborted", reason="startup_cancelled")
            await self.events("run_finished", asdict(result))
            return result

    async def compact(self, *, force: bool = True) -> bool:
        """Idle maintenance: no new run, main-model request, MCP connection or tool execution."""
        if self.closed:
            raise RuntimeError("Conversation is closed")
        if not self.session.messages:
            return False
        self.ensure_events("Context compaction")
        assert self.events is not None
        if self.compaction_model is None and self.config.compaction_model is not None:
            self.compaction_model = CompactionModel(self.config.compaction_model)
            self.resources.push_async_callback(self.compaction_model.close)
        old_run = self.session.run_id
        old_budget = self.context.execution_budget
        self.session.run_id = None
        self.context.execution_budget = None
        try:
            return await self.context.prepare(
                self.session,
                ContextBuilder().build_active_context(self.session),
                [t.spec for t in self.registry.values()],
                self.compaction_model,
                self.events,
                force=force,
                reason="manual" if force else "between_runs",
            )
        except asyncio.CancelledError:
            # Context owns the failure event; maintenance must not create a second receipt.
            return False
        finally:
            self.session.run_id = old_run
            self.context.execution_budget = old_budget

    async def memory_prefix(self, *, refresh: bool = False) -> str:
        if self.memory is None or self.config.security.mode != "local":
            return ""
        assert self.events is not None
        previous = self.session.memory_snapshot
        try:
            epoch = self.memory.epoch()
            identity = root_id(self.memory)
            if (
                not refresh
                and previous is not None
                and previous["epoch"] == epoch
                and previous["root_id"] == identity
            ):
                return str(previous["content"])
            if not refresh and previous is None and self.resumed:
                # Old sessions never had a snapshot; preserve that original absence on resume.
                value = {
                    "version": 1,
                    "content": "",
                    "tokens": 0,
                    "epoch": epoch,
                    "root_id": identity,
                    "reason": "legacy_resume",
                }
            else:
                value = snapshot(self.memory, self.context.main_estimator)
        except (OSError, ValueError) as exc:
            # A broken memory file cannot prevent an ordinary coding task. Never reuse stale data.
            value = {
                "version": 1,
                "content": "",
                "tokens": 0,
                "epoch": "unavailable",
                "root_id": root_id(self.memory),
                "reason": "unavailable",
            }
            await self.events("warning", {"detail": f"Memory snapshot unavailable: {exc}"})
        value = redact(value, self.secret_values())
        await self.events("memory_snapshot", value)
        self.session.memory_snapshot = value
        return str(value["content"])

    async def refresh_memory(self, *, force: bool = False) -> None:
        """Refresh on explicit invalidation or an actual compaction boundary."""
        if self.memory is None or self.events is None:
            return
        previous = self.session.memory_snapshot
        suffix = await self.memory_prefix(refresh=force)
        if previous == self.session.memory_snapshot:
            return
        current = next((m for m in self.session.messages if m.role == "system"), None)
        if current is None:
            return
        base = current.content
        old_suffix = str(previous["content"]) if previous else ""
        if old_suffix and base.endswith(old_suffix):
            base = base[: -len(old_suffix)]
        seq = await self.events("instructions", {"content": base + suffix})
        self.session.messages = [Message("system", base + suffix, seq=seq)] + [
            m for m in self.session.messages if m.role != "system"
        ]
        self.context.reset_calibration()

    async def before_compact(self) -> None:
        if self.memory is None or self.events is None or self.config.security.mode != "local":
            return
        try:
            now = datetime.now().astimezone()
            text = memory_daily.handoff(self.memory, self.session.session_id, now)
            if text:
                self.memory.write(memory_daily.daily_name(now), redact(text, self.secret_values()))
                if self.memory_index is not None:
                    self.memory_index.changed()
        except (OSError, ValueError) as exc:
            await self.events("warning", {"detail": f"Memory handoff skipped: {exc}"})
        finally:
            await self.refresh_memory(force=True)

    async def summarize_exit(self, reason: str) -> None:
        """Called only after interactive terminal teardown, never by ordinary close."""
        if (
            self.closed
            or self.exit_summary_attempted
            or self.memory is None
            or self.config.security.mode != "local"
            or reason not in {"slash-exit", "eof"}
        ):
            return
        self.exit_summary_attempted = True
        started = time.monotonic()
        model: ChatModel | None = None
        try:
            prepared = memory_daily.exit_request(
                self.session,
                self.context.main_estimator,
                self.config.limits.context_window,
                self.secret_values(),
            )
            if prepared is None:
                return
            request, budget = prepared
            self.ensure_events("Memory exit summary")
            assert self.events is not None
            await self.events(
                "memory_daily",
                {"status": "summarizing", "timeout_seconds": memory_daily.EXIT_TIMEOUT},
                publish=False,
            )
            config = replace(
                self.config.model,
                reasoning_effort="low",
                max_output_tokens=min(
                    self.config.model.max_output_tokens, memory_daily.EXIT_OUTPUT_TOKENS
                ),
                request_timeout_seconds=memory_daily.EXIT_TIMEOUT,
            )

            async def quiet(kind: str, data: Json, **_: object) -> int:
                # No assistant stream, tool state, or main-run profile mutations.
                return 0

            async with asyncio.timeout(memory_daily.EXIT_TIMEOUT):
                model = ChatModel(config)
                reply = await model.complete(request, [], quiet)
            text = str(redact(reply.message.content, self.secret_values())).strip()
            written = (
                not reply.message.tool_calls
                and reply.finish_reason == "stop"
                and memory_daily.valid_summary(text)
            )
            if written:
                now = datetime.now().astimezone()
                self.memory.write(
                    memory_daily.daily_name(now),
                    memory_daily.summary_entry(text, reason, self.session.session_id, now),
                )
                if self.memory_index is not None:
                    self.memory_index.changed()
            await self.events(
                "memory_daily",
                {
                    "status": "written" if written else "skipped",
                    "reason": reason if written else "empty_or_invalid_summary",
                    "model": config.name,
                    "input_budget": budget,
                    "estimated_input_tokens": self.context.main_estimator.count(request, []),
                    "usage": asdict(reply.usage),
                    "duration_ms": int((time.monotonic() - started) * 1000),
                },
                publish=False,
            )
        except Exception as exc:
            # Daily is best effort; a network/disk/log failure must not trap exit.
            if self.events is not None:
                try:
                    await self.events(
                        "memory_daily",
                        {
                            "status": "skipped",
                            "reason": type(exc).__name__,
                            "model": self.config.model.name,
                            "duration_ms": int((time.monotonic() - started) * 1000),
                        },
                        publish=False,
                    )
                    await self.events(
                        "warning",
                        {"detail": f"Memory exit summary skipped: {type(exc).__name__}: {exc}"},
                        publish=False,
                    )
                except Exception:
                    pass
        finally:
            if model is not None:
                try:
                    async with asyncio.timeout(1):
                        await model.close()
                except Exception:
                    pass

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            try:
                if self.gateway.backend is not None:
                    close_backend = getattr(self.gateway.backend, "close", None)
                    if close_backend is not None:
                        await close_backend()
            finally:
                try:
                    if self.memory_index is not None:
                        await self.memory_index.close()
                finally:
                    await self.resources.aclose()
        finally:
            if self.writer:
                self.writer.close()
