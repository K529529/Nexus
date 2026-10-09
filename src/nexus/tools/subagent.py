"""Bounded read-only investigation using the ordinary message-driven loop."""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Callable
from dataclasses import asdict, replace
from pathlib import Path

from nexus.core.agent import run_turn
from nexus.core.types import (
    Emit,
    ExecutionContext,
    Json,
    Limits,
    Message,
    Model,
    Session,
    Tool,
    ToolResult,
    ToolSpec,
)

MAX_CALLS = 2
MAX_STEPS = 6
TIMEOUT_SECONDS = 150
RESULT_BYTES = 6000
EXCLUDED = {".git", ".nexus", ".venv", "node_modules", "__pycache__", ".env"}
INSPECT_SPEC = ToolSpec(
    "inspect_repository",
    "Read-only inspection. path is workspace-relative. mode=list lists files; mode=read returns "
    "a Path header then numbered source lines from start_line (default 1, max 160 lines). "
    "Cite the header path and line number. mode=search finds literal text "
    "(max 80 matches). Narrow path to a module. No execution/writes. Check truncated.",
    {
        "type": "object",
        "properties": {
            "mode": {"type": "string", "enum": ["list", "read", "search"]},
            "path": {"type": "string"},
            "text": {"type": "string"},
            "start_line": {"type": "integer", "minimum": 1},
        },
        "required": ["mode", "path"],
        "additionalProperties": False,
    },
)


def inspect_files(args: Json, context: ExecutionContext) -> ToolResult:
    root = context.workspace.resolve()
    try:
        if args.keys() - {"mode", "path", "text", "start_line"}:
            raise ValueError("Unknown arguments")
        mode, name = args.get("mode"), args.get("path")
        if not isinstance(mode, str) or mode not in {"list", "read", "search"}:
            raise ValueError("Expected mode")
        if not isinstance(name, str):
            raise ValueError("Expected relative path")
        relative = Path(name)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or EXCLUDED.intersection(relative.parts)
        ):
            raise ValueError("Path must stay inside repository")
        target = (root / relative).resolve()
        if not target.is_relative_to(root) or EXCLUDED.intersection(target.relative_to(root).parts):
            raise ValueError("Path escapes repository or accesses excluded files")
        start = args.get("start_line", 1)
        if type(start) is not int or start < 1:
            raise ValueError("start_line must be positive")
        needle = args.get("text", "")
        if not isinstance(needle, str) or (mode == "search" and not needle):
            raise ValueError("Search requires nonempty literal text")
        paths: list[Path] = []
        truncated = False
        if target.is_file():
            paths = [target]
        elif mode != "read" and target.is_dir():
            for directory, dirs, files in os.walk(target, followlinks=False):
                dirs[:] = sorted(
                    d for d in dirs if d not in EXCLUDED and not (Path(directory) / d).is_symlink()
                )
                for file in sorted(files):
                    p = Path(directory) / file
                    if file not in EXCLUDED and not p.is_symlink():
                        paths.append(p)
                        if len(paths) >= 2000:
                            break
                if len(paths) >= 2000:
                    truncated = True
                    break
        else:
            raise ValueError("Path not found or not a readable file")
        lines: list[str] = []
        scanned = 0
        for p in paths:
            label = p.relative_to(root).as_posix()
            if mode == "list":
                lines.append(label)
            else:
                with p.open("rb") as stream:
                    raw = stream.read(262145)
                scanned += len(raw)
                if len(raw) > 262144 or b"\x00" in raw:
                    truncated = True
                else:
                    source = raw.decode("utf-8", errors="replace").splitlines()
                    if mode == "read":
                        lines = [f"Path: {label}"] + [
                            f"{i + 1}: {line}"
                            for i, line in enumerate(source)
                            if start <= i + 1 < start + 160
                        ]
                        truncated |= len(source) >= start + 160
                    else:
                        lines.extend(
                            f"{label}:{i + 1}: {line}"
                            for i, line in enumerate(source)
                            if needle in line
                        )
            if (len(lines) >= 80 and mode != "read") or scanned >= 8_000_000:
                lines = lines[:80]
                truncated = True
                break
        output = "\n".join(lines).encode()
        truncated |= len(output) > context.output_limit_bytes
        return ToolResult(
            context.call_id,
            True,
            {
                "text": output[: context.output_limit_bytes].decode("utf-8", errors="ignore"),
                "truncated": truncated,
            },
            truncated=truncated,
        )
    except (OSError, ValueError) as exc:
        return ToolResult(context.call_id, False, {"detail": str(exc)}, "inspection_failed")


async def inspect_repository(args: Json, context: ExecutionContext, emit: Emit) -> ToolResult:
    return inspect_files(args, context)


def create_spawn_agent_tool(
    parent: Session, model: Model | Callable[[], Model], limits: Limits
) -> Tool:
    async def spawn(args: Json, context: ExecutionContext, emit: Emit) -> ToolResult:
        task = args.get("task")
        if args.keys() != {"task"} or not isinstance(task, str) or not 1 <= len(task) <= 6000:
            return ToolResult(context.call_id, False, {}, "invalid_arguments")
        calls = sum(
            c.name == "spawn_agent"
            for m in parent.messages
            if m.run_id == parent.run_id
            for c in m.tool_calls
        )
        if calls > MAX_CALLS:
            return ToolResult(context.call_id, False, {}, "subagent_call_limit")
        started = time.monotonic()
        child = Session(parent.workspace)
        system = next((m.content for m in parent.messages if m.role == "system"), "")
        child.messages = [
            Message(
                "system",
                system + "\n\nYou are an isolated read-only "
                "investigator. Only inspect_repository is available. Do not edit, execute code, "
                "delegate or plan the whole parent task. Answer the question within 6 turns. "
                "Return only JSON with findings and uncertainties (arrays of strings). "
                "Use at most 4 findings and 2 uncertainties; keep the entire JSON under 1200 "
                "characters. Each finding should cite path:line evidence and one implementation "
                "implication. Omit background explanations and code fences. Distinguish "
                "observations from hypotheses. Finish as soon as the question is answerable; "
                "report unresolved "
                "points as uncertainties instead of exhausting the budget. Return findings before "
                "exhausting steps.",
            )
        ]

        async def child_emit(kind: str, data: Json, *, protocol_data: Json | None = None) -> int:
            if kind in {"assistant_delta", "tool_output_delta"}:
                return 0
            return await emit(
                "subagent_" + kind,
                {
                    **data,
                    "parent_call_id": context.call_id,
                    "child_session_id": child.session_id,
                    "child_run_id": child.run_id,
                    "depth": 1,
                },
                protocol_data=protocol_data,
            )

        child_limits = replace(
            limits,
            max_steps=MAX_STEPS,
            context_window=min(limits.context_window, 24000),
            max_output_tokens=min(limits.max_output_tokens, 2048),
            output_limit_bytes=8000,
        )
        await emit(
            "subagent_started",
            {
                "parent_call_id": context.call_id,
                "depth": 1,
                "limits": asdict(child_limits),
                "timeout_seconds": TIMEOUT_SECONDS,
            },
        )
        current = asyncio.current_task()
        cancelling = current.cancelling() if current else 0
        timed_out = False
        try:
            async with asyncio.timeout(TIMEOUT_SECONDS):
                result = await run_turn(
                    child,
                    task,
                    model() if callable(model) else model,
                    {"inspect_repository": Tool(INSPECT_SPEC, inspect_repository)},
                    child_emit,
                    child_limits,
                )
                if current and current.cancelling() > cancelling and result.outcome == "aborted":
                    raise asyncio.CancelledError
        except TimeoutError:
            timed_out = True
        # A cancelled run still returned a paired trajectory and partial usage.
        raw = result.final_text.encode()
        report: Json = {"findings": [], "uncertainties": ["No structured final report returned"]}
        if len(raw) <= RESULT_BYTES:
            try:
                parsed = json.loads(result.final_text)
                if (
                    isinstance(parsed, dict)
                    and isinstance(parsed.get("findings"), list)
                    and isinstance(parsed.get("uncertainties"), list)
                    and all(
                        isinstance(x, str) for k in ("findings", "uncertainties") for x in parsed[k]
                    )
                ):
                    report = {k: parsed[k] for k in ("findings", "uncertainties")}
            except ValueError:
                pass
        if not report["findings"] and raw:
            report["unstructured_report"] = raw[:RESULT_BYTES].decode("utf-8", errors="ignore")
        data: Json = {
            "report": report,
            "outcome": result.outcome,
            "reason": "timeout" if timed_out else result.reason,
            "steps": result.steps,
            "model_calls": result.model_calls,
            "tool_calls": result.tool_calls,
            "usage": asdict(result.usage),
            "child_session_id": child.session_id,
            "workspace_access": "read_only",
            "depth": 1,
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
        await emit("subagent_finished", {**data, "parent_call_id": context.call_id})
        return ToolResult(
            context.call_id,
            result.outcome == "completed",
            data,
            None if result.outcome == "completed" else "subagent_incomplete",
            duration_ms=data["duration_ms"],
            truncated=len(raw) > RESULT_BYTES,
        )

    return Tool(
        ToolSpec(
            "spawn_agent",
            "Delegate one narrow independent repository investigation to an isolated read-only "
            "sub-context. Include a concrete question, relevant paths and known facts. Returns "
            "cited findings/uncertainties; you own all edits and integration. No writes, shell, "
            "MCP or recursive delegation. At most 2 calls per run, 6 child turns and 150s each. "
            "Costs are additional to your model turns. Use only when it reduces your search work; "
            "do not repeat its searches without a concrete doubt.",
            {
                "type": "object",
                "properties": {"task": {"type": "string", "maxLength": 6000}},
                "required": ["task"],
                "additionalProperties": False,
            },
        ),
        spawn,
    )
