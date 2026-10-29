"""Tool checkpoints, scoped by run and call; no workflow scheduler."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict
from pathlib import Path

from nexus.core.types import Emit, Json, Session, ToolCall, ToolResult


def sync_directory(path: Path) -> None:
    # Python's Windows directory handles do not support fsync. File data still
    # uses FlushFileBuffers via os.fsync; metadata power-loss guarantees differ.
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def call_digest(call: ToolCall) -> str:
    return hashlib.sha256((call.name + "\0" + call.arguments_json).encode()).hexdigest()


def digest(arguments: Json) -> str:
    """The existing Gateway argument digest, shared with recovery matching."""
    return hashlib.sha256(
        json.dumps(arguments, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
    ).hexdigest()


def risk_identity(call: ToolCall | None, arguments_digest: str | None = None) -> Json:
    if call is not None and arguments_digest is None:
        try:
            arguments = json.loads(call.arguments_json)
            if isinstance(arguments, dict):
                arguments_digest = digest(arguments)
        except (ValueError, TypeError):
            pass  # Retain the risk even when legacy arguments cannot be matched.
    return {"name": call.name if call else "unknown", "arguments_digest": arguments_digest}


def prepared(call: ToolCall) -> Json:
    return {
        "call_id": call.id,
        "phase": "PREPARED",
        "call": asdict(call),
        "call_digest": call_digest(call),
    }


def remember_risk(session: Session, value: ToolResult, call: ToolCall | None) -> None:
    if (
        value.error_code in {"interrupted_unknown", "cleanup_incomplete"}
        or value.data.get("execution_state") in {"UNKNOWN", "interrupted_unknown"}
        or value.data.get("cleanup_incomplete")
    ):
        key = f"{session.run_id or ''}:{value.call_id}"
        session.unresolved_effects[key] = {
            "run_id": session.run_id,
            "call_id": value.call_id,
            **risk_identity(call),
        }


async def save_result(value: ToolResult, emit: Emit) -> None:
    # The complete (already output-budgeted) result is the checkpoint, not a status flag.
    phase = "UNKNOWN" if value.error_code == "interrupted_unknown" else "COMPLETED"
    await emit(
        "tool_checkpoint", {"call_id": value.call_id, "phase": phase, "result": asdict(value)}
    )
