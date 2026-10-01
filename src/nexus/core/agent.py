"""The single model → tools → model loop. Completion belongs to the model."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict
from uuid import uuid4

from nexus.core.context import Context, ContextBuilder
from nexus.core.types import (
    Emit,
    ExecutionContext,
    Json,
    Limits,
    Message,
    Model,
    ModelError,
    RunResult,
    Session,
    Tool,
    ToolCall,
    ToolCancelled,
    ToolResult,
    Usage,
)


async def append_message(session: Session, message: Message, emit: Emit) -> None:
    message.run_id = session.run_id
    message.seq = await emit("message", message.public(), protocol_data=message.protocol_data)
    session.messages.append(message)


def valid_reply(message: Message, finish: str) -> None:
    if message.role != "assistant" or finish not in {"stop", "tool_calls"}:
        raise ModelError("invalid_finish", finish)
    if message.tool_calls:
        ids = [c.id for c in message.tool_calls]
        if any(not c.id.strip() or not c.name.strip() for c in message.tool_calls) or len(
            set(ids)
        ) != len(ids):
            raise ModelError("invalid_tool_ids")
    elif finish != "stop" or not message.content.strip():
        raise ModelError("empty_or_invalid_final")


async def run_turn(
    session: Session,
    user_text: str,
    model: Model,
    registry: dict[str, Tool],
    emit: Emit,
    limits: Limits,
    *,
    environment: str = "",
) -> RunResult:
    started = time.monotonic()
    session.run_id = session.resume_run_id or uuid4().hex
    result = RunResult("failed")
    pending: list[ToolCall] = []
    active: ToolCall | None = None
    known_cancel: ToolResult | None = None
    usages: list[Usage] = []

    async def observed(kind: str, data: Json, *, protocol_data: Json | None = None) -> int:
        if kind == "model_started":
            result.model_calls += 1
        if kind == "model_finished" and "usage" in data:
            usages.append(Usage(**data["usage"]))
        return await emit(kind, data, protocol_data=protocol_data)

    async def record_tool(value: ToolResult) -> None:
        await append_message(session, value.message(), observed)
        await observed(
            "tool_finished",
            {
                "call_id": value.call_id,
                "ok": value.ok,
                "error_code": value.error_code,
                "duration_ms": value.duration_ms,
                "message_seq": session.messages[-1].seq,
            },
        )

    try:
        await observed("run_started", {"workspace": str(session.workspace)})
        session.resume_run_id = None
        if environment:
            await append_message(session, Message("user", environment), observed)
        await append_message(session, Message("user", user_text), observed)
        context = Context(limits)
        builder = ContextBuilder()
        for step in range(1, limits.max_steps + 1):
            result.steps = step
            specs = [t.spec for t in registry.values()]
            active_context = builder.build_active_context(session, session.run_id)
            context.check(active_context, specs)
            reply = await model.complete(active_context, specs, observed)
            context.observe(reply, active_context, specs)
            valid_reply(reply.message, reply.finish_reason)
            prior_ids = {
                c.id for m in session.messages if m.run_id == session.run_id for c in m.tool_calls
            }
            if any(call.id in prior_ids for call in reply.message.tool_calls):
                raise ModelError("duplicate_tool_id")
            await append_message(session, reply.message, observed)
            if not reply.message.tool_calls:
                result.outcome, result.final_text = "completed", reply.message.content
                break
            pending = list(reply.message.tool_calls)
            while pending:
                active = pending.pop(0)
                await observed(
                    "tool_started",
                    {
                        "call_id": active.id,
                        "name": active.name,
                        "arguments_json": active.arguments_json,
                    },
                )
                result.tool_calls += 1
                try:
                    args = json.loads(active.arguments_json)
                    if not isinstance(args, dict):
                        raise ValueError("arguments must be an object")
                except (ValueError, TypeError):
                    value = ToolResult(active.id, False, {}, "invalid_arguments")
                else:
                    tool = registry.get(active.name)
                    if tool is None:
                        value = ToolResult(active.id, False, {}, "unknown_tool")
                    else:
                        value = await tool.execute(
                            args,
                            ExecutionContext(
                                session.workspace,
                                active.id,
                                limits.shell,
                                limits.output_limit_bytes,
                            ),
                            observed,
                        )
                if value.call_id != active.id:
                    raise RuntimeError("Tool returned mismatched call_id")
                await record_tool(value)
                active = None
                if value.error_code == "cleanup_incomplete":
                    raise RuntimeError("cleanup_incomplete")
            pending = []
        else:
            result.outcome, result.reason = "limited", "max_steps"
    except ToolCancelled as exc:
        known_cancel = exc.result
        result.outcome = "failed" if exc.result.data.get("cleanup_incomplete") else "aborted"
        result.reason = exc.result.error_code
    except asyncio.CancelledError:
        result.outcome, result.reason = "aborted", "user_abort"
    except ModelError as exc:
        if exc.code == "context_limit":
            result.outcome = "limited"
        result.reason = str(exc)
    except Exception as exc:
        # Concrete SDK errors never enter this path; avoid request/header dumps.
        result.reason = f"runtime_error:{type(exc).__name__}"
    try:
        if active is not None:
            # If the result was already saved, never append a second tool result.
            paired = any(
                m.role == "tool" and m.tool_call_id == active.id and m.run_id == session.run_id
                for m in session.messages
            )
            if not paired:
                await record_tool(
                    known_cancel
                    or ToolResult(
                        active.id,
                        False,
                        {"detail": "Check actual state before any retry."},
                        "interrupted_unknown",
                    )
                )
        for call in pending:
            await record_tool(ToolResult(call.id, False, {}, "not_executed"))
    except Exception:
        result.outcome, result.reason = "failed", "session_write_failed; recovery incomplete"
    result.duration_ms = int((time.monotonic() - started) * 1000)
    complete_usage = bool(usages) and len(usages) == result.model_calls
    result.usage = Usage(
        **{
            field: (
                sum(getattr(u, field) for u in usages)
                if complete_usage and all(getattr(u, field) is not None for u in usages)
                else None
            )
            for field in ("input_tokens", "output_tokens", "total_tokens")
        },
        source="reported"
        if complete_usage and all(u.source == "reported" for u in usages)
        else "unknown",
    )
    try:
        await observed("run_finished", asdict(result))
    except Exception:
        result.outcome, result.reason = "failed", "session_write_failed; recovery incomplete"
    return result
