"""Rebuild tool boundaries from JSONL; inspect patch effects without writing files."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import TYPE_CHECKING

from nexus.core.durable import call_digest
from nexus.core.types import Json, RuntimeEvent, Session, ToolCall, ToolResult
from nexus.tools.patch import MAX_BYTES, _hash, checked_path
from nexus.tools.patch_format import PatchError

if TYPE_CHECKING:
    from nexus.app.session import SessionLog


def patch_evidence(records: list[Json], session: Session) -> Json:
    manifest = next((r["data"] for r in reversed(records) if r["kind"] == "patch_manifest"), None)
    planned = [r["data"] for r in records if r["kind"] == "patch_diff"]
    files = manifest["files"] if manifest is not None else planned
    checks = []
    for fact in files:
        state = "conflict"
        actual = None
        try:
            path = checked_path(session.workspace, fact["path"])
            if path.exists() and (not path.is_file() or path.stat().st_size > MAX_BYTES):
                raise ValueError("Not a bounded regular file")
            actual = _hash(path.read_bytes() if path.exists() else None)
            if actual == fact["after_hash"]:
                state = "applied"
            elif actual == fact["before_hash"]:
                state = "not_applied"
        except (OSError, ValueError, KeyError, PatchError):
            pass
        checks.append(
            {
                "path": fact.get("path"),
                "state": state,
                "actual_hash": actual,
                "before_hash": fact.get("before_hash"),
                "after_hash": fact.get("after_hash"),
            }
        )
    states = {f["state"] for f in checks}
    overall = (next(iter(states)) if len(states) == 1 else "partial") if checks else "unknown"
    if "conflict" in states:
        overall = "conflict"
    return {
        "state": overall,
        "complete_manifest": manifest is not None,
        "files": checks,
        "detail": "Point-in-time hashes only; no patch replay or process-liveness claim.",
    }


def recover_tools(records: list[Json], session: Session, writer: SessionLog) -> list[str]:
    from nexus.app.session import SessionError, now

    checkpoints: dict[tuple[str | None, str], Json] = {}
    calls = {(m.run_id, c.id): c for m in session.messages for c in m.tool_calls}
    for record in records:
        if record["kind"] != "tool_checkpoint":
            continue
        data = record["data"]
        key = (record.get("run_id"), data.get("call_id"))
        call = calls.get(key)
        phase = data.get("phase")
        prior = checkpoints.get(key)
        if call is None or phase not in {"PREPARED", "DISPATCHING", "COMPLETED", "UNKNOWN"}:
            raise SessionError("Invalid tool checkpoint association/phase")
        if phase == "PREPARED":
            if prior is not None or data.get("call") != asdict(call):
                raise SessionError("Invalid PREPARED checkpoint")
        elif phase == "DISPATCHING":
            if prior is None or prior["phase"] != "PREPARED":
                raise SessionError("DISPATCHING without PREPARED")
        else:
            try:
                if set(data["result"]) != {
                    "call_id",
                    "ok",
                    "data",
                    "error_code",
                    "duration_ms",
                    "truncated",
                }:
                    raise ValueError("Incomplete result")
                saved = ToolResult(**data["result"])
                if (
                    saved.call_id != call.id
                    or type(saved.ok) is not bool
                    or not isinstance(saved.data, dict)
                    or type(saved.truncated) is not bool
                    or type(saved.duration_ms) is not int
                ):
                    raise ValueError("Invalid complete result")
            except (KeyError, ValueError, TypeError):
                raise SessionError("Invalid checkpoint result") from None
            if prior and "result" in prior and prior["result"] != data["result"]:
                raise SessionError("Conflicting checkpoint results")
        checkpoints[key] = data

    # Risks survive normal turns, compaction, and subsequent resumes. They are never
    # cleared by a model statement or by a one-call approval for a new operation.
    for message in session.messages:
        if message.role != "tool":
            continue
        try:
            raw_result = json.loads(message.content)
        except ValueError:
            continue  # legacy tool messages can be plain text
        if not isinstance(raw_result, dict):
            continue
        data = raw_result.get("data", {})
        if not isinstance(data, dict):
            continue
        if (
            raw_result.get("error_code") in {"interrupted_unknown", "cleanup_incomplete"}
            or data.get("execution_state") in {"UNKNOWN", "interrupted_unknown"}
            or data.get("cleanup_incomplete")
        ):
            call = calls.get((message.run_id, message.tool_call_id or ""))
            session.unresolved_effects[f"{message.run_id or ''}:{message.tool_call_id}"] = {
                "run_id": message.run_id,
                "call_id": message.tool_call_id,
                "name": call.name if call else "unknown",
            }

    pending: list[ToolCall] = []
    pending_run: str | None = None
    for message in session.messages:
        if message.tool_calls:
            pending, pending_run = list(message.tool_calls), message.run_id
        elif message.role == "tool" and pending:
            pending.pop(0)
    deferred = False
    for call in pending:
        checkpoint = checkpoints.get((pending_run, call.id), {})
        evidence = [
            r
            for r in records
            if r.get("run_id") == pending_run and r["data"].get("call_id") == call.id
        ]
        phase = checkpoint.get("phase", "UNKNOWN")
        value: ToolResult | None = None
        if "result" in checkpoint:
            value = ToolResult(**checkpoint["result"])
        elif phase == "PREPARED" and checkpoint.get("call_digest") == call_digest(call):
            deferred = True
        else:
            # Redacted arguments cannot be safely reexecuted, even when PREPARED.
            security = [
                r
                for r in evidence
                if r["kind"]
                in {"tool_policy", "approval_requested", "approval_resolved", "tool_execution"}
            ]
            not_executed = phase == "PREPARED" or (
                not checkpoint
                and bool(security)
                and not any(r["kind"] == "tool_execution" for r in security)
            )
            data = {
                "execution_state": "not_executed" if not_executed else "UNKNOWN",
                "detail": "Interrupted call; inspect effects before any new operation. "
                "No prior one-time approval is restored.",
            }
            if any(r["kind"] in {"patch_manifest", "patch_diff"} for r in evidence):
                data["patch_recovery"] = patch_evidence(evidence, session)
            value = ToolResult(
                call.id, False, data, "not_executed" if not_executed else "interrupted_unknown"
            )
        if value is not None and (
            value.error_code in {"interrupted_unknown", "cleanup_incomplete"}
            or value.data.get("cleanup_incomplete")
            or value.data.get("execution_state") in {"UNKNOWN", "interrupted_unknown"}
        ):
            session.unresolved_effects[f"{pending_run or ''}:{call.id}"] = {
                "run_id": pending_run,
                "call_id": call.id,
                "name": call.name,
            }
        writer.append(
            RuntimeEvent(
                "tool_recovery",
                now(),
                session.session_id,
                pending_run,
                {
                    "call_id": call.id,
                    "phase": phase,
                    "action": "continue_prepared" if value is None else "restore",
                    "patch_recovery": value.data.get("patch_recovery") if value else None,
                },
            )
        )
        if deferred:
            session.pending_tools.append(call)
            if value is not None:
                session.recovered_results[call.id] = value
        else:
            assert value is not None
            message = value.message()
            message.run_id = pending_run
            message.seq = writer.append(
                RuntimeEvent("message", now(), session.session_id, pending_run, message.public())
            )
            session.messages.append(message)
    if session.pending_tools:
        session.resume_run_id = pending_run
    return (
        [
            "Interrupted calls reconciled. Verified PREPARED calls await the next resumed turn; "
            "unknown effects are not replayed and approvals are not restored."
        ]
        if pending
        else []
    )
