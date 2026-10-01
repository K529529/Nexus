from __future__ import annotations

import asyncio
import json
import os
import shlex
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

from nexus.app.config import default_shell
from nexus.core.types import (
    Emit,
    ExecutionContext,
    Json,
    Message,
    ModelReply,
    ToolCall,
    ToolSpec,
    Usage,
)


class Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, Json]] = []
        self.tool_started = asyncio.Event()
        self.output_started = asyncio.Event()

    async def __call__(
        self,
        kind: str,
        data: Json,
        *,
        protocol_data: Json | None = None,
    ) -> int:
        self.events.append((kind, data))
        if kind == "tool_started":
            self.tool_started.set()
        if kind == "tool_output_delta":
            self.output_started.set()
        return len(self.events)


class ScriptedModel:
    def __init__(self, replies: list[ModelReply]) -> None:
        self.replies = iter(replies)
        self.requests: list[list[Message]] = []

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        emit: Emit,
    ) -> ModelReply:
        self.requests.append(list(messages))
        await emit("model_started", {"attempt": 1})
        reply = next(self.replies)
        await emit("model_finished", {"usage": asdict(reply.usage)})
        return reply


def reply(*calls: ToolCall, text: str = "", finish: str | None = None) -> ModelReply:
    return ModelReply(
        Message("assistant", text, list(calls)),
        finish or ("tool_calls" if calls else "stop"),
        Usage(10, 5, 15, "reported"),
    )


def call(name: str, args: Json, ident: str = "c1") -> ToolCall:
    return ToolCall(ident, name, json.dumps(args))


def python_command(code: str) -> str:
    if os.name == "nt":
        return "& '" + sys.executable.replace("'", "''") + "' -c '" + code.replace("'", "''") + "'"
    return shlex.quote(sys.executable) + " -c " + shlex.quote(code)


@pytest.fixture
def execution(tmp_path: Path) -> ExecutionContext:
    return ExecutionContext(tmp_path, "test-call", default_shell())
