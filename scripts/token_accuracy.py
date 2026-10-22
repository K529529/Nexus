"""Offline rescore of frozen P1 counting requests and unmodified provider receipts.

Usage: uv run python scripts/token_accuracy.py artifacts/final-p1
No network, API keys, agent execution, fitting or request retries.
"""

from __future__ import annotations

import json
import math
import statistics
import sys
from pathlib import Path
from typing import Any

from nexus.core.tokens import TokenEstimator, estimate, wire_message, wire_tools
from nexus.core.types import Message, ToolCall, ToolSpec, json_text


def inputs(payload: dict[str, Any]) -> tuple[list[Message], list[ToolSpec]]:
    messages = [
        Message(
            m["role"],
            m["content"],
            tool_calls=[
                ToolCall(c["id"], c["function"]["name"], c["function"]["arguments"])
                for c in m.get("tool_calls", [])
            ],
            tool_call_id=m.get("tool_call_id"),
            protocol_data={"fields": {"reasoning_content": m["reasoning_content"]}}
            if "reasoning_content" in m
            else None,
        )
        for m in payload["messages"]
    ]
    tools = [
        ToolSpec(t["function"]["name"], t["function"]["description"], t["function"]["parameters"])
        for t in payload.get("tools", [])
    ]
    return messages, tools


def metrics(rows: list[dict[str, Any]], column: str) -> dict[str, Any]:
    errors = [(r[column] - r["reported"]) / r["reported"] for r in rows]
    absolute = sorted(abs(e) for e in errors)
    return {
        "n": len(rows),
        "mean_signed_relative_error": statistics.mean(errors),
        "mean_absolute_relative_error": statistics.mean(absolute),
        "p90_absolute_relative_error": absolute[math.ceil(len(absolute) * 0.9) - 1],
        "max_overestimate_factor": max(1, max(r[column] / r["reported"] for r in rows)),
        "max_underestimate_factor": max(1, max(r["reported"] / r[column] for r in rows)),
        "within_15_percent": sum(e <= 0.15 for e in absolute),
        "within_25_percent": sum(e <= 0.25 for e in absolute),
    }


def main() -> None:
    root = Path(sys.argv[1])
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    receipts = {
        r["id"]: r
        for r in map(
            json.loads, (root / "responses.jsonl").read_text(encoding="utf-8").splitlines()
        )
    }
    rows: list[dict[str, Any]] = []
    estimators: dict[str, TokenEstimator] = {}
    old_states: dict[str, tuple[str, int, int]] = {}
    for sample in manifest["rows"]:
        receipt = receipts[sample["id"]]
        if not receipt.get("usage"):
            continue
        profile, payload = sample["profile"], sample["payload"]
        model = estimators.setdefault(
            profile,
            TokenEstimator(
                payload["model"],
                "https://api.deepseek.com"
                if profile == "deepseek"
                else "https://api.siliconflow.cn/v1",
                reasoning_effort=payload.get("reasoning_effort"),
                enable_thinking=payload.get("enable_thinking"),
            ),
        )
        messages, tools = inputs(payload)
        raw_old = estimate(messages, tools)
        scope = (
            json_text(wire_tools(tools))
            + json_text([wire_message(m) for m in messages if m.role == "system"])
            + json_text(
                sorted({(m.role, bool(m.tool_calls), bool(m.protocol_data)) for m in messages})
            )
        )
        old_scope, last_estimate, last_report = old_states.get(profile, ("", 0, 0))
        calibrated_old = (
            math.ceil(raw_old * last_report / last_estimate)
            if last_estimate and scope == old_scope
            else raw_old
        )
        reported = receipt["usage"]["prompt_tokens"]
        model.set_scope(messages, tools)
        rows.append(
            {
                "id": sample["id"],
                "profile": profile,
                "category": sample["category"],
                "request_sha256": sample["sha256"],
                "reported": reported,
                "old_raw": raw_old,
                "old_calibrated": calibrated_old,
                "new_raw": model.raw(messages, tools),
                "new_calibrated": model.count(messages, tools),
                "usage": receipt["usage"],
                "duration_seconds": receipt["duration_seconds"],
            }
        )
        model.observe(reported, messages, tools)
        old_states[profile] = (scope, raw_old, reported)
    summaries = {}
    for profile in estimators:
        for group, subset in [
            (
                "short_and_mixed",
                [r for r in rows if r["profile"] == profile and r["category"] != "boundary"],
            ),
            (
                "boundary",
                [r for r in rows if r["profile"] == profile and r["category"] == "boundary"],
            ),
            (
                "real_history_replay",
                [
                    r
                    for r in rows
                    if r["profile"] == profile and r["category"] == "real-history-replay"
                ],
            ),
        ]:
            summaries[f"{profile}/{group}"] = {
                key: metrics(subset, key)
                for key in ["old_raw", "old_calibrated", "new_raw", "new_calibrated"]
            }
    result = {
        "summaries": summaries,
        "rows": rows,
        "failed_attempts": [r for r in receipts.values() if not r.get("usage")],
    }
    (root / "measurements.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summaries, indent=2))


if __name__ == "__main__":
    main()
