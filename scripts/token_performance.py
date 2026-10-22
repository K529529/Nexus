"""Offline tokenizer/Agent Step/history split benchmark; never creates an API client.

uv run python scripts/token_performance.py --output artifacts/token-performance
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import hashlib
import json
import platform
import statistics
import sys
import time
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from typing import Any

from nexus.core.agent import run_turn
from nexus.core.compaction import split_history, summary_input
from nexus.core.context import Context, execution_budget
from nexus.core.tokens import TokenEstimator, qwen_template, tokenizer
from nexus.core.types import (
    Emit,
    Json,
    Limits,
    Message,
    ModelReply,
    Session,
    Tool,
    ToolSpec,
    Usage,
)

UNIT = (
    "Read-only inspection: tests have NOT RUN; do not merge main.\n"
    "中文历史：保留用户限制与未完成任务，工具成功不等于测试通过。\n"
    "def validate(value: int) -> bool:\n    return value >= 0\n"
    '{"exit_code": 1, "status": "FAILED", "path": "src/check.py"}\n'
)
SPEC = ToolSpec(
    "inspect",
    "Read a file without modifying it.",
    {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    },
)


def estimators() -> tuple[TokenEstimator, TokenEstimator]:
    return (
        TokenEstimator("deepseek-flash", "https://api.deepseek.com", reasoning_effort="high"),
        TokenEstimator(
            "Qwen/Qwen3.5-35B-A3B", "https://api.siliconflow.cn/v1", enable_thinking=False
        ),
    )


def context() -> Context:
    main, compact = estimators()
    return Context(Limits(context_window=128000), main_estimator=main, compaction_estimator=compact)


def history(characters: int) -> list[Message]:
    text = (UNIT * (characters // len(UNIT) + 1))[:characters]
    messages = [Message("system", "Inspect historical evidence. Preserve task constraints.", seq=1)]
    for offset in range(0, len(text), 3000):
        messages.extend(
            [
                Message(
                    "user",
                    f"Inspect chunk {offset // 3000}; do not merge main.",
                    seq=len(messages) + 1,
                    run_id="frozen",
                ),
                Message(
                    "assistant",
                    text[offset : offset + 3000],
                    seq=len(messages) + 2,
                    run_id="frozen",
                ),
            ]
        )
    return messages


def request(ctx: Context, messages: list[Message]) -> list[Message]:
    ctx.execution_budget = execution_budget(1, 40)
    logical = messages + [Message("user", "Continue.")]
    return ctx.task_request(ctx.with_state(Session(Path.cwd()), logical))


def freeze(target: int) -> tuple[list[Message], list[Message], int]:
    ctx = context()
    low, high = 1, target * 8
    while low < high:
        middle = (low + high) // 2
        if ctx.main_estimator.raw(request(ctx, history(middle)), [SPEC]) < target:
            low = middle + 1
        else:
            high = middle
    messages = history(low)
    projected = request(ctx, messages)
    return messages, projected, ctx.main_estimator.raw(projected, [SPEC])


class OfflineModel:
    async def complete(
        self, messages: list[Message], tools: list[ToolSpec], emit: Emit
    ) -> ModelReply:
        await emit("model_started", {"attempt": 1})
        await emit("model_finished", {"usage": asdict(Usage()), "duration_ms": 0})
        return ModelReply(Message("assistant", "Offline benchmark complete."), "stop", Usage())


async def no_tool(*_: Any) -> Any:
    raise AssertionError("Benchmark never executes a tool")


async def measure(
    target: int, repeat: int, messages: list[Message], projected: list[Message]
) -> Json:
    ctx = context()
    total = ctx.main_estimator.raw(projected, [SPEC])
    single = ctx.main_estimator.timing_since()
    ctx.begin_step()
    start = time.perf_counter()
    prefix, recent = split_history(messages[1:], 20000, count=ctx.main_estimator.count)
    split_ms = (time.perf_counter() - start) * 1000
    split_timing = ctx.main_estimator.timing_since()
    session = Session(Path.cwd(), messages=deepcopy(messages))
    compact_request = summary_input(session, prefix or messages[1:], 4096, ())
    compact_tokens = ctx.compaction_estimator.raw(compact_request, [])
    compact_timing = ctx.compaction_estimator.timing_since()
    events: list[tuple[str, Json]] = []

    async def emit(kind: str, data: Json, **_: Any) -> int:
        events.append((kind, data))
        return max(m.seq for m in messages) + len(events)

    start = time.perf_counter()
    result = await run_turn(
        session,
        "Continue.",
        OfflineModel(),
        {"inspect": Tool(SPEC, no_tool)},
        emit,
        Limits(context_window=128000),
        context=ctx,
    )
    elapsed = (time.perf_counter() - start) * 1000
    assert result.outcome == "completed" and result.steps == 1
    assert not any(k.startswith("compaction_") for k, _ in events)
    final = next(d for k, d in events if k == "context_usage")
    before = next(d for k, d in events if k == "context_projection")
    assert before["estimated_context_tokens"] == total
    return {
        "target_tokens": target,
        "actual_tokens": total,
        "repeat": repeat,
        "message_count": len(projected),
        "single_count": single,
        "step_token_count_ms": final["token_count_ms"],
        "step_token_count_calls": final["token_count_calls"],
        "step_before_request_ms": before["token_count_ms"],
        "step_wall_ms_with_offline_stub": elapsed,
        "history_split_ms": split_ms,
        "history_split_counting": split_timing,
        "prefix_messages": len(prefix),
        "recent_messages": len(recent),
        "compaction_input_tokens": compact_tokens,
        "compaction_counting": compact_timing,
        "compaction_source": "eligible_prefix"
        if prefix
        else "whole_history_probe_only_no_eligible_prefix",
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    fixtures = {str(n): freeze(n) for n in [8000, 64000, 110000]}
    inputs = {
        k: {
            "history": [asdict(m) for m in h],
            "request": [asdict(m) for m in r],
            "actual_tokens": n,
        }
        for k, (h, r, n) in fixtures.items()
    }
    encoded = json.dumps(inputs, ensure_ascii=False, sort_keys=True).encode()
    (args.output / "inputs.json.gz").write_bytes(gzip.compress(encoded, mtime=0))
    # Explicit cold resource-load probes, separately from steady-state measurements.
    cold = []
    for profile, counter in zip(["deepseek", "qwen"], estimators(), strict=True):
        tokenizer.cache_clear()
        qwen_template.cache_clear()
        counter.raw(fixtures["8000"][1], [SPEC] if profile == "deepseek" else [])
        cold.append({"profile": profile, **counter.timing_since()})
    main_counter, compact_counter = estimators()
    main_counter.raw([Message("user", "warmup")], [])
    compact_counter.raw([Message("user", "warmup")], [])
    rows = []
    for target, (messages, projected, _) in fixtures.items():
        for repeat in range(1, 4):
            row = await measure(int(target), repeat, messages, projected)
            rows.append(row)
            print(json.dumps(row), flush=True)
            with (args.output / "measurements.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(row) + "\n")
    summaries = {}
    for target in [8000, 64000, 110000]:
        subset = [r for r in rows if r["target_tokens"] == target]
        summaries[str(target)] = {
            key: statistics.median(r[key] for r in subset)
            for key in ["step_token_count_ms", "step_token_count_calls", "history_split_ms"]
        }
        summaries[str(target)]["single_count_ms"] = statistics.median(
            r["single_count"]["token_count_ms"] for r in subset
        )
        summaries[str(target)]["compaction_count_ms"] = statistics.median(
            r["compaction_counting"]["token_count_ms"] for r in subset
        )
    report = {
        "platform": platform.platform(),
        "python": sys.version,
        "input_sha256": hashlib.sha256(encoded).hexdigest(),
        "paid_api_calls": 0,
        "clock": "time.perf_counter",
        "repeats": 3,
        "cold_8k": cold,
        "warm_medians": summaries,
        "notes": [
            "History split includes token counting; do not add its time again.",
            "Step includes all seven local counts and no API network waits.",
            "Legacy Runtime Facts byte-budget estimates and scope hashing "
            "are outside raw-count timing.",
            "Synthetic inputs, local measurements, no production latency claim.",
        ],
    }
    (args.output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    asyncio.run(main())
