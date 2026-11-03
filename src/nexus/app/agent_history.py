"""Read-only projection of the root JSONL and its owned child logs."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from nexus.app.agent_view import AgentUsage, combined_usage
from nexus.app.session import read_records
from nexus.core.types import Json, RuntimeEvent


def child_path(root: Path, relative: str) -> Path:
    base = root.parent.resolve()
    path = (base / relative).resolve()
    parts = path.relative_to(base).parts
    if len(parts) != 3 or parts[1] != "subagents" or path.suffix != ".jsonl":
        raise ValueError("Invalid child log location")
    return path


def read_tree(path: Path) -> tuple[dict[str, list[Json]], dict[str, Json], list[str]]:
    with path.open("rb") as stream:
        root, torn = read_records(stream)
    logs = {"root": root}
    nodes: dict[str, Json] = {}
    warnings = ["damaged_tail: incomplete final root record ignored"] if torn else []
    for row in root:
        if row["kind"] in {"agent_created", "agent_state"}:
            nodes[row["data"]["agent_id"]] = row["data"]
    for ident, node in nodes.items():
        try:
            relative = node.get("log_path") or f"{root[0]['session_id']}/subagents/{ident}.jsonl"
            with child_path(path, relative).open("rb") as stream:
                rows, torn = read_records(stream)
            logs[ident] = rows
            if torn:
                warnings.append(f"Agent {ident}: incomplete tail")
        except (OSError, ValueError, RuntimeError):
            logs[ident] = []
            warnings.append(f"Agent {ident}: log unavailable")
    return logs, nodes, warnings


def intervals(rows: list[Json]) -> list[Json]:
    active: dict[str, Json] = {}
    result: list[Json] = []
    for row in rows:
        kind, data = row["kind"], row["data"]
        if kind == "run_started":
            active.clear()  # Never stretch an operation across a process interruption.
        if kind in {"tool_started", "model_started"}:
            key = "tool:" + data["call_id"] if kind == "tool_started" else "model"
            active[key] = row
        elif kind in {"tool_finished", "model_finished"}:
            key = "tool:" + data["call_id"] if kind == "tool_finished" else "model"
            start = active.pop(key, None)
            if start is not None:
                result.append({"kind": "tool" if key.startswith("tool:") else "model",
                    "name": start["data"].get("name", "model"),
                    "start": start["timestamp"], "end": row["timestamp"],
                    "run_id": row.get("run_id"), "call_id": data.get("call_id"),
                    "ok": data.get("ok", not data.get("error"))})
    return result


def tree_facts(path: Path) -> Json:
    logs, nodes, warnings = read_tree(path)
    agents, ledgers = [], []
    purposes = {"main": AgentUsage(), "compaction": AgentUsage()}
    retries = failures = compactions = 0
    result_bytes: list[int | None] = []
    for ident, rows in logs.items():
        ledger = AgentUsage()
        purpose = "main"
        for row in rows:
            event = RuntimeEvent(row["kind"], row["timestamp"], row["session_id"],
                                 row.get("run_id"), row["data"])
            ledger.observe(event)
            if event.kind == "model_started":
                purpose = "compaction" if event.data.get("purpose") == "compaction" else "main"
                retries += int(event.data.get("attempt", 1) > 1)
            purposes[purpose].observe(event)
            failures += int(event.kind == "model_finished" and bool(event.data.get("error")))
            compactions += int(event.kind == "context_compacted")
            if event.kind == "tool_finished":
                value = event.data.get("result_bytes")
                result_bytes.append(value if type(value) is int else None)
        if not rows:
            ledger.started = 1  # Missing log is unknown usage, never free execution.
        ledgers.append(ledger)
        node = nodes.get(ident, {"agent_id": "root", "parent_id": None, "role": "root",
                                "depth": 0, "assignment": logs["root"][0]["data"].get("title", "")})
        last_start = max((r["seq"] for r in rows if r["kind"] == "run_started"), default=0)
        last_end = next((r for r in reversed(rows) if r["kind"] == "run_finished"
                         and r["seq"] > last_start), None)
        state = last_end["data"].get("outcome", "unknown") if last_end else "interrupted"
        settlement = next((r for r in reversed(rows) if r["kind"] == "agent_execution_finished"
                           and r["seq"] > last_start
                           and r["data"].get("execution_id") == node.get("execution_id")), None)
        if settlement is not None:
            state = settlement["data"]["result"]["outcome"]
        elif ident != "root":
            from nexus.core.inbox import Inbox
            pending = Inbox()
            pending.restore(rows)
            if pending.guides():
                state = "interrupted"
        if node.get("state") in {"cleanup_failed", "recovery_failed"}:
            state = node["state"]
        # Public bodies only. Private provider continuation fields never enter HTML.
        events = [{"kind": r["kind"], "timestamp": r["timestamp"], "data": r["data"]}
                  for r in rows if r["kind"] in {"message", "tool_started", "tool_finished",
                    "agent_message", "input_received", "input_delivered", "warning"}]
        agents.append({**node, "state": state, "usage": ledger.public(),
                       "timeline": intervals(rows), "events": events})
    root_finishes = [r["data"].get("duration_ms") for r in logs["root"]
                     if r["kind"] == "run_finished"]
    root_starts = sum(r["kind"] == "run_started" for r in logs["root"])
    duration = sum(root_finishes) if len(root_finishes) == root_starts and root_finishes and all(
        type(value) is int for value in root_finishes
    ) else None
    usage = combined_usage(ledgers)
    metrics = {"retries": retries, "failed_attempts": failures + usage["requests"] - usage[
        "finished"], "compactions": compactions, "compaction_calls": purposes["compaction"].started,
        "tool_result_bytes": sum(v for v in result_bytes if v is not None)
        if all(v is not None for v in result_bytes) else None,
        "usage_by_purpose": {name: {"calls": value.started,
            "reported_calls": value.public()["total_tokens_coverage"],
            **{key: value.public()[key + "_known"] if value.public()[key + "_coverage"] else None
               for key in ("input_tokens", "output_tokens", "total_tokens")}}
            for name, value in purposes.items()}}
    return {"agents": agents, "usage": usage, "duration_ms": duration, "metrics": metrics,
            "warnings": warnings, "delegated": bool(nodes), "cost": None}


def elapsed_ms(start: str, end: str) -> float:
    delta = datetime.fromisoformat(end) - datetime.fromisoformat(start)
    return max(0, delta.total_seconds() * 1000)
