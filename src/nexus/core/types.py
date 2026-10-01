"""Small JSON-facing contracts. No SDK types or workflow state."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import uuid4

Json = dict[str, Any]
Outcome = Literal["completed", "limited", "aborted", "failed"]


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


@dataclass
class ToolCall:
    id: str
    name: str
    arguments_json: str


@dataclass
class Message:
    role: Literal["system", "user", "assistant", "tool"]
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    protocol_data: Json | None = None
    seq: int = 0
    # In-memory copy of the existing JSONL event envelope, never model payload data.
    run_id: str | None = None

    def public(self) -> Json:
        data: Json = {"role": self.role, "content": self.content}
        if self.tool_calls:
            data["tool_calls"] = [asdict(c) for c in self.tool_calls]
        if self.tool_call_id is not None:
            data["tool_call_id"] = self.tool_call_id
        return data

    @classmethod
    def from_data(
        cls,
        data: Json,
        *,
        seq: int = 0,
        protocol_data: Json | None = None,
        run_id: str | None = None,
    ) -> Message:
        return cls(
            role=data["role"],
            content=data.get("content", ""),
            tool_calls=[ToolCall(**c) for c in data.get("tool_calls", [])],
            tool_call_id=data.get("tool_call_id"),
            protocol_data=protocol_data,
            seq=seq,
            run_id=run_id,
        )


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: Json


@dataclass
class ToolResult:
    call_id: str
    ok: bool
    data: Json
    error_code: str | None = None
    duration_ms: int = 0
    truncated: bool = False

    def message(self) -> Message:
        return Message("tool", json_text(asdict(self)), tool_call_id=self.call_id)


@dataclass
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    source: str = "unknown"


@dataclass
class ModelReply:
    message: Message
    finish_reason: str
    usage: Usage = field(default_factory=Usage)


@dataclass
class RuntimeEvent:
    kind: str
    timestamp: str
    session_id: str
    run_id: str | None
    data: Json


class Emit(Protocol):
    async def __call__(
        self,
        kind: str,
        data: Json,
        *,
        protocol_data: Json | None = None,
    ) -> int: ...


class Model(Protocol):
    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        emit: Emit,
    ) -> ModelReply: ...


@dataclass
class ExecutionContext:
    workspace: Path
    call_id: str
    shell: str
    output_limit_bytes: int = 32768


@dataclass
class Tool:
    spec: ToolSpec
    execute: Callable[[Json, ExecutionContext, Emit], Awaitable[ToolResult]]


@dataclass
class Session:
    workspace: Path
    session_id: str = field(default_factory=lambda: uuid4().hex)
    messages: list[Message] = field(default_factory=list)
    run_id: str | None = None
    # Set only by explicit resume; ordinary turns always start an independent run.
    resume_run_id: str | None = None


@dataclass
class Limits:
    max_steps: int = 40
    context_window: int = 32768
    max_output_tokens: int = 8192
    output_limit_bytes: int = 32768
    shell: str = "/bin/sh"


@dataclass
class RunResult:
    outcome: Outcome
    final_text: str = ""
    steps: int = 0
    tool_calls: int = 0
    model_calls: int = 0
    usage: Usage = field(default_factory=Usage)
    duration_ms: int = 0
    reason: str | None = None


class ModelError(Exception):
    """Safe, classified error; never an SDK request/response dump."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        super().__init__(f"{code}: {detail}" if detail else code)


class ToolCancelled(BaseException):
    """Carries the executor's bounded cleanup facts through cancellation."""

    def __init__(self, result: ToolResult) -> None:
        self.result = result
