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
from nexus.core.agent import append_message, run_turn
from nexus.core.context import instructions
from nexus.core.model import ChatModel
from nexus.core.types import Message, RunResult, RuntimeEvent, Session, json_text
from nexus.tools.mcp import connect_servers
from nexus.tools.registry import native_tools


class Conversation:
    def __init__(
        self,
        workspace: Path,
        config: Config,
        consumer: Callable[[RuntimeEvent], Awaitable[None]],
        *,
        home: Path | None = None,
        resume: Path | None = None,
    ) -> None:
        self.workspace = workspace.resolve()
        self.config, self.consumer, self.home = config, consumer, home
        self.session = Session(self.workspace)
        self.writer: SessionLog | None = None
        self.events: Events | None = None
        self.warnings: list[str] = []
        self.resumed = resume is not None
        self.model: ChatModel | None = None
        self.registry = native_tools()
        self.resources = AsyncExitStack()
        self.unavailable: list[str] = []
        self.closed = False
        if resume:
            self.session, self.writer, self.warnings = resume_session(resume, self.workspace, home)

    async def turn(self, text: str) -> RunResult:
        if self.closed:
            raise RuntimeError("Conversation is closed")
        self.session.run_id = None
        prefix = instructions(self.workspace, self.config.limits.shell)
        secret_values = [self.config.model.key()]
        for raw in self.config.mcp_servers.values():
            if isinstance(raw, dict) and isinstance(raw.get("env_from"), dict):
                secret_values.extend(
                    os.environ.get(n, "") for n in raw["env_from"].values() if isinstance(n, str)
                )
        if self.writer is None:
            self.writer = SessionLog.create(
                self.session,
                redact(text, tuple(secret_values)),
                asdict(self.config.model),
                self.home,
            )
        self.events = Events(self.session, self.writer, self.consumer, tuple(secret_values))
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
                    self.registry = native_tools()
                    raise
            fingerprint = hashlib.sha256(
                json_text(
                    {
                        "model": asdict(self.config.model),
                        "tools": [asdict(t.spec) for t in self.registry.values()],
                    }
                ).encode()
            ).hexdigest()
            if self.resumed:
                await self.events(
                    "warning",
                    {
                        "detail": "Resumed with current model and tool schemas; "
                        "old calls remain history and are never replayed."
                    },
                )
                self.resumed = False
            await self.events(
                "configuration",
                {"fingerprint": fingerprint, "model": asdict(self.config.model)},
            )
            if self.unavailable:
                await append_message(
                    self.session,
                    Message(
                        "user",
                        "[Environment fact] MCP servers "
                        + ", ".join(self.unavailable)
                        + " are unavailable in this conversation; their tools did not run.",
                    ),
                    self.events,
                )
            return await run_turn(
                self.session, text, self.model, self.registry, self.events, self.config.limits
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
