"""One composition path shared by interactive, exec and evaluation callers."""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import asdict
from pathlib import Path

from nexus.app.config import Config
from nexus.app.events import Events, redact
from nexus.app.memory import MemoryStore
from nexus.app.memory_index import MemoryIndex
from nexus.app.session import SessionLog, resume_session
from nexus.app.skills import SkillCatalog
from nexus.core.agent import run_turn
from nexus.core.compaction import provenance
from nexus.core.context import Context, ContextBuilder, instructions
from nexus.core.model import ChatModel, CompactionModel
from nexus.core.skills import check_active
from nexus.core.tokens import TokenEstimator
from nexus.core.types import Message, ModelError, RunResult, RuntimeEvent, Session, Tool, json_text
from nexus.tools.mcp import connect_servers
from nexus.tools.memory import create_memory_tools
from nexus.tools.registry import default_tools
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
        if resume:
            self.session, self.writer, self.warnings = resume_session(resume, self.workspace, home)
        resolved = registry(self.session) if callable(registry) else registry
        self.base_registry = dict(resolved) if resolved is not None else default_tools(self.session)
        self.registry = dict(self.base_registry)
        # Custom/evaluation registries never inherit the operator's global memory.
        self.memory = (
            MemoryStore((home or Path.home() / ".nexus") / "memory")
            if registry is None and config.memory_enabled
            else None
        )
        self.memory_index = (
            MemoryIndex(self.memory, config.memory_qmd_command) if self.memory is not None else None
        )
        if self.memory is not None and self.memory_index is not None:
            self.base_registry.update(
                create_memory_tools(
                    self.memory,
                    search=self.memory_index.search,
                    changed=self.memory_index.changed,
                )
            )
            self.registry.update(self.base_registry)
        # Custom registries (including fixed eval) opt in explicitly; never inherit host Skills.
        self.catalog = (
            SkillCatalog(
                skill_directory or (home or Path.home() / ".nexus") / "skills",
                lambda value: str(redact(value, self.secret_values())),
            )
            if registry is None or skill_directory is not None
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
        if self.memory_index is not None:
            self.memory_index.start(self.events)
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
                    self.unavailable = await connect_servers(
                        self.config.mcp_servers, self.registry, self.resources, self.events
                    )
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

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            if self.memory_index is not None:
                await self.memory_index.close()
            await self.resources.aclose()
        finally:
            if self.writer:
                self.writer.close()
