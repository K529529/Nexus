"""Deterministic, request-only tool observation lifecycle. No persisted lifecycle state."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, replace

from nexus.core.types import Json, Message, Session, json_text

CONTEXT_POLICY = "Context Runtime V0.2.1"
VERSION = "compact-v1"
WORKING_SET_TOKENS = 16384
WORKING_SET_FRACTION = 0.25
SUCCESS_MAX_BYTES = 1024
FAILURE_MAX_BYTES = 2048


def provenance() -> Json:
    return {
        "context_policy": CONTEXT_POLICY,
        "observation_projection": {
            "version": VERSION,
            "working_set_tokens": WORKING_SET_TOKENS,
            "working_set_fraction": WORKING_SET_FRACTION,
            "success_max_bytes": SUCCESS_MAX_BYTES,
            "failure_max_bytes": FAILURE_MAX_BYTES,
        },
    }


def hot_tool_seqs(session: Session, run_id: str) -> set[int]:
    """Only validated ordinary assistant messages are appended to raw Session history."""
    seen_assistant = False
    hot = set()
    for message in reversed(session.messages):
        if message.run_id != run_id:
            continue
        if message.role == "assistant":
            seen_assistant = True
        elif message.role == "tool" and not seen_assistant:
            hot.add(message.seq)
    return hot


def byte_size(content: str) -> int:
    return len(content.encode("utf-8"))


def preview(text: str, budget: int, *, failure: bool = False) -> Json:
    raw = text.encode("utf-8")
    if len(raw) <= budget:
        return {"head": text, "tail": ""}
    # Failed commands spend their first 512 bytes per stream on the tail.
    tail = min(budget, 512) if failure else budget - budget // 2
    head = budget - tail
    return {
        "head": raw[:head].decode("utf-8", errors="ignore"),
        "tail": raw[-tail:].decode("utf-8", errors="ignore") if tail else "",
    }


def compact(message: Message, tool: str) -> Message:
    """Return a new bounded tool message, or the original if parsing/savings fail."""
    try:
        original = json.loads(message.content)
        if (
            not isinstance(original, dict)
            or "projection" in original
            or original.get("call_id") != message.tool_call_id
            or type(original.get("ok")) is not bool
            or type(original.get("truncated")) is not bool
            or not isinstance(original.get("data"), dict)
            or original.get("error_code") is not None
            and not isinstance(original["error_code"], str)
        ):
            return message
        data = original["data"]
        limit = SUCCESS_MAX_BYTES if original["ok"] else FAILURE_MAX_BYTES
        facts = {
            k: data[k]
            for k in ("exit_code", "timed_out", "cancelled", "cleanup_incomplete", "partial")
            if k in data
        }
        output: Json = {
            "projection": VERSION,
            "source_seq": message.seq,
            "tool": tool,
            "ok": original["ok"],
            "error_code": original.get("error_code"),
            "truncated": original["truncated"],
            "source_bytes": byte_size(message.content),
            "preview_omitted": True,
            "data": facts,
        }

        def fits() -> bool:
            return byte_size(json_text(output)) <= limit

        def put(key: str, value: object) -> bool:
            facts[key] = value
            if fits():
                return True
            del facts[key]
            return False

        if not fits():
            return message
        if tool == "apply_patch":
            files = data.get("files", [])
            omitted = data.get("omitted_files", 0)
            if not isinstance(files, list) or type(omitted) is not int or omitted < 0:
                return message
            facts["omitted_files"] = omitted + len(files)
            for key in ("changed_files", "no_changes", "failed_file", "detail"):
                if key in data:
                    put(key, data[key])
            visible: list[Json] = []
            facts["files"] = visible
            if not fits():
                return message
            for item in files:
                if not isinstance(item, dict):
                    return message
                fact = {
                    k: item[k]
                    for k in (
                        "path",
                        "status",
                        "added_lines",
                        "deleted_lines",
                        "before_hash",
                        "after_hash",
                        "truncated",
                    )
                    if k in item
                }
                visible.append(fact)
                if not fits():
                    visible.pop()
                    break
            facts["omitted_files"] = omitted + len(files) - len(visible)
            if "created_directories" in data:
                put("created_directories", data["created_directories"])
        else:
            if "detail" in data:
                # Error facts take priority, while leaving room for both command streams.
                detail = data["detail"]
                if isinstance(detail, str) and byte_size(detail) > 256:
                    detail = preview(detail, 256, failure=not original["ok"])
                put("detail", detail)
            if tool == "exec_command":
                streams = {key: data.get(key, "") for key in ("stdout", "stderr")}
                if any(not isinstance(value, str) for value in streams.values()):
                    return message
            else:
                streams = {"preview": json_text(data)}
            nonempty = [key for key, value in streams.items() if value]
            best: Json | None = None
            low, high = 0, limit
            while low <= high:
                allowance = (low + high) // 2
                count = max(1, len(nonempty))
                for key, value in streams.items():
                    index = nonempty.index(key) if key in nonempty else 0
                    quota = allowance // count + int(index < allowance % count) if value else 0
                    facts[key] = preview(
                        value, quota, failure=tool == "exec_command" and not original["ok"]
                    )
                if fits():
                    best = dict(facts)
                    low = allowance + 1
                else:
                    high = allowance - 1
            if best is None:
                return message
            if any(streams[k] and not (best[k]["head"] or best[k]["tail"]) for k in streams):
                return message
            output["data"] = best
        content = json_text(output)
        if byte_size(content) > limit or byte_size(content) >= byte_size(message.content):
            return message
        return replace(message, content=content)
    except (ValueError, TypeError, OverflowError, RecursionError):
        # Optimization must never turn an opaque/corrupt result into an Agent failure.
        return message


@dataclass
class Projection:
    messages: list[Message]
    diagnostics: Json


def project(
    session: Session,
    logical: list[Message],
    budget: int,
    cost: Callable[[Message], int],
) -> Projection:
    assert session.run_id is not None
    raw = {m.seq: m for m in session.messages if m.run_id == session.run_id and m.role == "tool"}
    # Even if handed a previous preview, always return to original FULL observations.
    full = [raw.get(m.seq, m) if m.role == "tool" else m for m in logical]
    observations = [m for m in full if m.role == "tool"]
    names = {
        c.id: c.name for m in session.messages if m.run_id == session.run_id for c in m.tool_calls
    }
    hot = hot_tool_seqs(session, session.run_id)
    target = min(WORKING_SET_TOKENS, max(0, budget // 4))
    remaining = max(0, target - sum(cost(m) for m in observations if m.seq in hot))
    recent: set[int] = set()
    for message in reversed(observations):
        if message.seq in hot:
            continue
        size = cost(message)
        if size > remaining:
            break
        recent.add(message.seq)
        remaining -= size
    projected = []
    cold = compacted = 0
    for message in full:
        result = message
        if message.role == "tool" and message.seq not in hot | recent:
            cold += 1
            # Without a raw source/name, keeping FULL is safer than guessing provenance.
            if message.seq in raw and message.tool_call_id in names:
                result = compact(message, names[message.tool_call_id])
            compacted += int(result is not message)
        projected.append(result)
    visible = [m for m in projected if m.role == "tool"]
    return Projection(
        projected,
        {
            "version": VERSION,
            "working_set_target_tokens": target,
            "hot_full_count": sum(m.seq in hot for m in observations),
            "recent_full_count": len(recent),
            "cold_count": cold,
            "cold_compacted_count": compacted,
            "cold_kept_full_count": cold - compacted,
            "observation_full_bytes": sum(byte_size(m.content) for m in observations),
            "observation_projected_bytes": sum(byte_size(m.content) for m in visible),
            "observation_full_estimated_tokens": sum(cost(m) for m in observations),
            "observation_projected_estimated_tokens": sum(cost(m) for m in visible),
        },
    )
