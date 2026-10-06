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
from nexus.app.session import SessionLog, resume_session
from nexus.app.skills import SkillCatalog
from nexus.core.agent import run_turn
from nexus.core.context import instructions
from nexus.core.model import ChatModel
from nexus.core.observations import provenance
from nexus.core.skills import check_active
from nexus.core.types import Message, RunResult, RuntimeEvent, Session, Tool, json_text
from nexus.tools.mcp import connect_servers
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
        self.environment_display = environment_display
        self.resources = AsyncExitStack()
        self.unavailable: list[str] = []
        self.closed = False
        if resume:
            self.session, self.writer, self.warnings = resume_session(resume, self.workspace, home)
        resolved = registry(self.session) if callable(registry) else registry
        self.base_registry = dict(resolved) if resolved is not None else default_tools(self.session)
        self.registry = dict(self.base_registry)
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
            self.workspace, self.config.limits.shell, display=self.environment_display
        )
        self.config.model.key()  # Validate credentials only when actually running a task.
        self.ensure_events(text)
        assert self.events is not None
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
                    self.unavailable = await connect_servers(
                        self.config.mcp_servers, self.registry, self.resources, self.events
                    )
                except BaseException:
                    # A partially opened conversation must not leak client/process handles.
                    await self.resources.aclose()
                    self.model = None
                    self.registry = dict(self.base_registry)
                    raise
            fingerprint = hashlib.sha256(
                json_text(
                    {
                        "model": asdict(self.config.model),
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
                            else "No unfinished run boundary; next input starts an isolated run. "
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
                {"fingerprint": fingerprint, "model": asdict(self.config.model), **provenance()},
            )
            environment = (
                "[Environment fact] MCP servers "
                + ", ".join(self.unavailable)
                + " are unavailable in this conversation; their tools did not run."
                if self.unavailable
                else ""
            )
            return await run_turn(
                self.session,
                text,
                self.model,
                self.registry,
                self.events,
                self.config.limits,
                environment=environment,
            )
        except asyncio.CancelledError:
            result = RunResult("aborted", reason="startup_cancelled")
            await self.events("run_finished", asdict(result))
            return result

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            await self.resources.aclose()
        finally:
            if self.writer:
                self.writer.close()
