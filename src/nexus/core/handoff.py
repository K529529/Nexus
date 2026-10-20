"""Execution-window receipts: observable facts, never a task-success evaluator."""

from __future__ import annotations

import json

from nexus.core.conversation import excerpt
from nexus.core.observations import hot_tool_seqs
from nexus.core.types import Json, RunResult, Session

NOTICE = (
    "Previous execution window stopped before a final answer. Tool success is not task "
    "verification. Inspect current state before retrying interrupted/unknown operations; "
    "saved calls must not be replayed. The plan is model-reported and may be incomplete."
)


def make_handoff(session: Session, result: RunResult, max_steps: int, after: int) -> Json:
    messages = [m for m in session.messages[after:] if m.run_id == session.run_id]
    names = {c.id: c.name for m in messages for c in m.tool_calls}
    results = [m for m in messages if m.role == "tool"]
    hot = hot_tool_seqs(session, session.run_id or "")
    recent = []
    unknown_count = 0
    for message in results:
        try:
            value = json.loads(message.content)
            data = value.get("data", {})
            unknown = (
                value.get("error_code") in {"interrupted_unknown", "cleanup_incomplete"}
                or data.get("side_effects") == "unknown"
                or data.get("cleanup_incomplete") is True
            )
            unknown_count += bool(unknown)
            fact = {
                "source_seq": message.seq,
                "tool": excerpt(names.get(message.tool_call_id or "", "unknown"), 160),
                "ok": value.get("ok"),
                "error_code": excerpt(str(value["error_code"]), 160)
                if value.get("error_code")
                else None,
                "no_subsequent_model_response": message.seq in hot,
                "facts": {
                    k: data[k]
                    for k in (
                        "exit_code",
                        "changed_files",
                        "partial",
                        "timed_out",
                        "cancelled",
                        "cleanup_incomplete",
                        "side_effects",
                    )
                    if k in data
                    and (data[k] is None or type(data[k]) in {bool, int} or data[k] == "unknown")
                },
            }
            recent.append(fact)
        except (ValueError, TypeError, AttributeError):
            unknown_count += 1
    plan = session.plan.items if session.plan and session.plan.run_id == session.run_id else ()
    pending = [item for item in plan if item.status != "completed"]
    return {
        "notice": NOTICE,
        "outcome": result.outcome,
        "reason": result.reason,
        "steps_used": result.steps,
        "max_steps": max_steps,
        "tool_calls": result.tool_calls,
        "recent_tools": recent[-5:],
        "omitted_tools": max(0, len(recent) - 5),
        "unknown_effects_count": unknown_count,
        "pending_plan": [{"step": excerpt(p.step, 300), "status": p.status} for p in pending[:5]],
        "omitted_pending_plan": max(0, len(pending) - 5),
        "completed_plan_items": sum(p.status == "completed" for p in plan),
        "verification": "not_assessed_by_runtime",
    }
