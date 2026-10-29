"""The single model → tools → model loop. Completion belongs to the model."""

from __future__ import annotations

import asyncio
import time
from dataclasses import asdict
from uuid import uuid4

from nexus.core.context import Context, ContextBuilder, execution_budget
from nexus.core.durable import prepared, remember_risk, save_result
from nexus.core.handoff import make_handoff
from nexus.core.security import SecurityGateway
from nexus.core.stagnation import GUIDANCE, StagnationDetector
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
    compaction_model: Model | None = None,
    context: Context | None = None,
    gateway: SecurityGateway | None = None,
) -> RunResult:
    gateway = gateway or SecurityGateway()
    started = time.monotonic()
    first_message = len(session.messages)
    session.run_id = session.resume_run_id or uuid4().hex
    if session.plan is not None and session.plan.run_id != session.run_id:
        session.plan = None
    if session.skills_run_id != session.run_id:
        session.loaded_skills = ()
        session.skills_run_id = session.run_id
    detector = StagnationDetector(session)
    pending_guidance: str | None = None
    result = RunResult("failed")
    pending: list[ToolCall] = []
    active: ToolCall | None = None
    known_cancel: ToolResult | None = None
    usages: list[Usage] = []
    saved_results: dict[str, ToolResult] = {}

    async def observed(kind: str, data: Json, *, protocol_data: Json | None = None) -> int:
        if kind in {
            "model_started",
            "model_finished",
            "tool_started",
            "tool_finished",
            "plan_updated",
            "skill_loaded",
            "stagnation_nudge",
        }:
            # A recovered PREPARED call runs before the first new model step.
            # Internal plan/skill audit records still require a positive step.
            data = {**data, "step": max(1, result.steps)}
        if kind == "model_started" and data.get("purpose") != "compaction":
            result.model_calls += 1
        if kind == "model_finished" and "usage" in data and data.get("purpose") != "compaction":
            usages.append(Usage(**data["usage"]))
        seq = await emit(kind, data, protocol_data=protocol_data)
        if kind == "tool_checkpoint" and "result" in data:
            value = ToolResult(**data["result"])
            saved_results[value.call_id] = value
        return seq

    async def task_events(kind: str, data: Json, *, protocol_data: Json | None = None) -> int:
        if kind == "model_started" and pending_guidance:
            data = {**data, "stagnation_nudge_included": True}
        return await observed(kind, data, protocol_data=protocol_data)

    async def record_tool(value: ToolResult) -> None:
        if value.call_id not in saved_results:
            await save_result(value, observed)
        call = next(
            (
                c
                for m in reversed(session.messages)
                for c in m.tool_calls
                if c.id == value.call_id and m.run_id == session.run_id
            ),
            None,
        )
        remember_risk(session, value, call)
        message = value.message()
        await append_message(session, message, observed)
        await observed(
            "tool_finished",
            {
                "call_id": value.call_id,
                "ok": value.ok,
                "error_code": value.error_code,
                "duration_ms": value.duration_ms,
                "message_seq": session.messages[-1].seq,
                "result_bytes": len(message.content.encode("utf-8")),
                "exit_code": value.data.get("exit_code"),
                "truncated": value.truncated,
            },
        )

    async def dispatch(call: ToolCall) -> ToolResult:
        await observed("tool_started", {"call_id": call.id, "name": call.name,
                                        "arguments_json": call.arguments_json})
        result.tool_calls += 1
        return await gateway.execute(
            call, registry, session,
            ExecutionContext(session.workspace, call.id, limits.shell,
                             limits.output_limit_bytes), observed,
        )

    try:
        await observed("run_started", {"workspace": str(session.workspace),
                                       "execution_mode": gateway.policy.mode})
        session.resume_run_id = None
        # Finish the interrupted message group before adding any new user message.
        pending = list(session.pending_tools)
        session.pending_tools.clear()
        while pending:
            active = pending.pop(0)
            value = session.recovered_results.pop(active.id, None)
            if value is None:
                value = await dispatch(active)
            await record_tool(value)
            active = None
            if value.error_code == "cleanup_incomplete":
                raise RuntimeError("cleanup_incomplete")
        if environment:
            await append_message(session, Message("user", environment), observed)
        await append_message(session, Message("user", user_text), observed)
        context = context or Context(limits)
        builder = ContextBuilder()

        def projected_context() -> tuple[list[Message], Json]:
            assert session.run_id is not None
            logical = builder.build_active_context(session, session.run_id)
            projection = context.project(session, logical)
            return projection.messages, projection.diagnostics

        for step in range(1, limits.max_steps + 1):
            result.steps = step
            context.begin_step()
            context.execution_budget = execution_budget(step, limits.max_steps)
            specs = [t.spec for t in registry.values()]
            active_context, _ = projected_context()
            await context.prepare(
                session,
                active_context,
                specs,
                compaction_model,
                observed,
                guidance=pending_guidance,
            )
            active_context, diagnostics = projected_context()
            active_context = context.task_request(active_context, pending_guidance)
            context.check(active_context, specs)
            await observed(
                "context_projection",
                {
                    **diagnostics,
                    **context.diagnostics(active_context, specs),
                    **context.performance(),
                    "performance_scope": "step_before_request_cumulative",
                    "step": step,
                },
            )
            try:
                reply = await model.complete(active_context, specs, task_events)
            except ModelError as exc:
                if exc.code != "context_limit":
                    raise
                normal_context, _ = projected_context()
                await context.prepare(
                    session,
                    normal_context,
                    specs,
                    compaction_model,
                    observed,
                    force=True,
                    guidance=pending_guidance,
                    reason="provider_overflow",
                )
                active_context, diagnostics = projected_context()
                active_context = context.task_request(active_context, pending_guidance)
                context.check(active_context, specs)
                await observed(
                    "context_projection",
                    {
                        **diagnostics,
                        **context.diagnostics(active_context, specs),
                        **context.performance(),
                        "performance_scope": "step_before_request_cumulative",
                        "step": step,
                    },
                )
                reply = await model.complete(active_context, specs, task_events)
            diagnostics = context.diagnostics(active_context, specs)
            estimated = diagnostics["estimated_context_tokens"]
            context.observe(reply, active_context, specs)
            await observed(
                "context_usage",
                {
                    **context.performance(),
                    "performance_scope": "step_after_usage_cumulative",
                    "step": step,
                    "estimated_context_tokens": estimated,
                    "calibrated_context_tokens": diagnostics["calibrated_context_tokens"],
                    "token_estimator": diagnostics["token_estimator"],
                    "reported_input_tokens": reply.usage.input_tokens,
                    "reported_output_tokens": reply.usage.output_tokens,
                    "cumulative_usage_tokens": sum(u.total_tokens or 0 for u in usages)
                    if usages and all(u.total_tokens is not None for u in usages)
                    else None,
                    "usage_scope": "main_model_execution_window",
                    "estimation_error_tokens": (
                        estimated - reply.usage.input_tokens
                        if reply.usage.input_tokens is not None
                        else None
                    ),
                },
            )
            valid_reply(reply.message, reply.finish_reason)
            prior_ids = {
                c.id for m in session.messages if m.run_id == session.run_id for c in m.tool_calls
            }
            if any(call.id in prior_ids for call in reply.message.tool_calls):
                raise ModelError("duplicate_tool_id")
            pending_guidance = None
            await append_message(session, reply.message, observed)
            if not reply.message.tool_calls:
                result.outcome, result.final_text = "completed", reply.message.content
                break
            pending = list(reply.message.tool_calls)
            for call in pending:
                await observed("tool_checkpoint", prepared(call))
            batch: list[tuple[ToolCall, ToolResult]] = []
            while pending:
                active = pending.pop(0)
                value = await dispatch(active)
                if value.call_id != active.id:
                    raise RuntimeError("Tool returned mismatched call_id")
                await record_tool(value)
                batch.append((active, value))
                active = None
                if value.error_code == "cleanup_incomplete":
                    raise RuntimeError("cleanup_incomplete")
            pending = []
            nudge = detector.observe_completed_step(
                session, batch, step=step, max_steps=limits.max_steps
            )
            if nudge is not None:
                await observed("stagnation_nudge", nudge)
                pending_guidance = GUIDANCE
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
                    saved_results.get(active.id)
                    or known_cancel
                    or ToolResult(
                        active.id,
                        False,
                        {"detail": "Check actual state before any retry."},
                        "interrupted_unknown",
                    )
                )
        for call in pending:
            await record_tool(session.recovered_results.pop(
                call.id, ToolResult(call.id, False, {}, "not_executed")))
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
            for field in ("input_tokens", "output_tokens", "total_tokens", "cached_input_tokens")
        },
        source="reported"
        if complete_usage and all(u.source == "reported" for u in usages)
        else "unknown",
    )
    try:
        if result.outcome != "completed":
            result.handoff = make_handoff(session, result, limits.max_steps, first_message)
        await observed("run_finished", asdict(result))
        assert session.run_id is not None
        session.run_outcomes[session.run_id] = {
            "outcome": result.outcome,
            "reason": result.reason,
            "handoff": result.handoff,
        }
    except Exception:
        result.outcome, result.reason = "failed", "session_write_failed; recovery incomplete"
    return result
