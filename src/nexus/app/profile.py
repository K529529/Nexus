"""Opt-in, event-derived developer metrics. No model/context/execution decisions."""

from __future__ import annotations

from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console
from rich.table import Table
from rich.text import Text

from nexus.app.tui import terminal_text
from nexus.core.types import Json, Limits, RuntimeEvent


def integer(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def total(values: list[int | None]) -> int | None:
    return sum(v for v in values if v is not None) if all(v is not None for v in values) else None


@dataclass
class ModelCall:
    step: int | None
    compaction: bool
    attempt: int | None
    finished: bool = False
    error: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    duration_ms: int | None = None


@dataclass
class ToolCall:
    step: int | None
    name: str = "unknown"
    started: bool = False
    ok: bool | None = None
    error_code: str | None = None
    exit_code: int | None = None
    result_bytes: int | None = None
    duration_ms: int | None = None
    truncated: bool | None = None


@dataclass
class RunProfile:
    run_id: str | None = None
    outcome: str = "unknown"
    reason: str | None = None
    duration_ms: int | None = None
    steps: int = 0
    models: list[ModelCall] = field(default_factory=list)
    tools: dict[str, ToolCall] = field(default_factory=dict)
    compactions: int = 0
    projections: list[Json] = field(default_factory=list)

    @property
    def normal_completions(self) -> list[ModelCall]:
        return [m for m in self.models if not m.compaction and m.finished and m.error is None]

    @property
    def context_inputs(self) -> tuple[int | None, int | None, int | None]:
        values = [m.input_tokens for m in self.normal_completions]
        if not values:
            return None, None, None
        peak = (
            max(v for v in values if v is not None) if all(v is not None for v in values) else None
        )
        return values[0], peak, values[-1]

    def tokens(self, field_name: str) -> int | None:
        known = [value for m in self.models if (value := getattr(m, field_name)) is not None]
        return sum(known) if known else None

    @property
    def usage_coverage(self) -> int:
        return sum(
            m.input_tokens is not None
            and m.output_tokens is not None
            and m.total_tokens is not None
            for m in self.models
        )

    @property
    def model_failures(self) -> Counter[str]:
        return Counter(
            m.error or "unfinished" for m in self.models if not m.finished or m.error is not None
        )

    def timeline(self) -> list[int | None]:
        steps = sorted(
            set(range(1, self.steps + 1))
            | {m.step for m in self.models if m.step is not None}
            | {t.step for t in self.tools.values() if t.step is not None}
        )
        return list(steps) if len(steps) <= 10 else [*steps[:3], None, *steps[-7:]]

    def completion(self, step: int) -> ModelCall | None:
        calls = [m for m in self.models if m.step == step and not m.compaction]
        successes = [m for m in calls if m.finished and m.error is None]
        return (successes or calls)[-1] if calls else None


class RunProfiler:
    def __init__(self) -> None:
        self.profile = RunProfile()
        self.pending: ModelCall | None = None

    def consume(self, event: RuntimeEvent) -> None:
        data, kind = event.data, event.kind
        if kind == "run_started":
            self.profile = RunProfile(run_id=event.run_id)
            self.pending = None
        profile = self.profile
        if event.run_id != profile.run_id:
            return
        step = integer(data.get("step"))
        if kind == "model_started":
            call = ModelCall(
                step, data.get("purpose") == "compaction", integer(data.get("attempt"))
            )
            profile.models.append(call)
            self.pending = call
        elif kind == "model_finished" and self.pending is not None:
            call = self.pending
            call.finished = True
            call.error = data.get("error")
            call.duration_ms = integer(data.get("duration_ms"))
            usage = data.get("usage", {})
            if usage.get("source") == "reported":
                call.input_tokens = integer(usage.get("input_tokens"))
                call.output_tokens = integer(usage.get("output_tokens"))
                call.total_tokens = integer(usage.get("total_tokens"))
            self.pending = None
        elif kind == "tool_started":
            profile.tools[data["call_id"]] = ToolCall(step, data["name"], started=True)
        elif kind == "tool_finished":
            tool = profile.tools.setdefault(data["call_id"], ToolCall(step))
            tool.ok = data["ok"]
            tool.error_code = data.get("error_code")
            # Exit codes can be negative; unlike sizes they are not nonnegative counts.
            tool.exit_code = data.get("exit_code") if type(data.get("exit_code")) is int else None
            tool.result_bytes = integer(data.get("result_bytes"))
            tool.duration_ms = integer(data.get("duration_ms"))
            tool.truncated = data.get("truncated")
        elif kind == "context_compacted":
            profile.compactions += 1
        elif kind == "context_projection":
            profile.projections.append(dict(data))
        elif kind == "run_finished":
            profile.outcome = data["outcome"]
            profile.reason = data.get("reason")
            profile.duration_ms = integer(data.get("duration_ms"))
            profile.steps = integer(data.get("steps")) or 0


def count(value: int | None) -> str:
    return "unknown" if value is None else f"{value:,}"


def seconds(value: int | None) -> str:
    return "unknown" if value is None else f"{value / 1000:.1f}s"


def size(value: int | None) -> str:
    if value is None:
        return "unknown"
    return f"{value:,} B" if value < 1024 else f"{value / 1024:.1f} KiB"


def render_profile(profile: RunProfile, console: Console, budget: int, path: Path | None) -> None:
    console.rule("Run Profile")
    summary = Table.grid(padding=(0, 2))

    def row(label: str, value: str) -> None:
        summary.add_row(Text(label), Text(terminal_text(value)))

    row("Outcome", profile.outcome)
    row("Failure reason", profile.reason or "-")
    row("Duration", seconds(profile.duration_ms))
    row("Steps", str(profile.steps))
    row("Model calls", str(len(profile.models)))
    model_failures = profile.model_failures
    failed = sum(model_failures.values())
    row("Successful calls", str(len(profile.models) - failed))
    row("Failed attempts", str(failed))
    row("Retries", str(sum(m.attempt is not None and m.attempt > 1 for m in profile.models)))
    row("Compaction calls", str(sum(m.compaction for m in profile.models)))
    row("Tool calls", str(sum(t.started for t in profile.tools.values())))
    row("Tokens (cumulative)", "")
    for label, key in [
        ("Reported input", "input_tokens"),
        ("Reported output", "output_tokens"),
        ("Reported total", "total_tokens"),
    ]:
        value = profile.tokens(key)
        marker = "≥ " if value is not None and profile.usage_coverage < len(profile.models) else ""
        row(f"  {label}", marker + count(value))
    row("  Usage coverage", f"{profile.usage_coverage} / {len(profile.models)} calls")
    first, peak, final = profile.context_inputs
    row("Active context (normal completions)", "")
    for label, value in [
        ("First input", first),
        ("Peak input", peak),
        ("Final input", final),
        ("Context budget", budget),
    ]:
        row(f"  {label}", count(value))
    row("  Peak / budget", f"{peak / budget:.1%}" if peak is not None and budget > 0 else "unknown")
    row("  Compactions", str(profile.compactions))
    tools = list(profile.tools.values())
    row("Tools", "")
    row("  Result bytes", size(total([t.result_bytes for t in tools])))
    row("  Failed results", str(sum(t.ok is False for t in tools)))
    timed = [t for t in tools if t.started and t.duration_ms is not None]
    slowest = max(timed, key=lambda t: t.duration_ms or 0) if timed else None
    row(
        "  Slowest observed",
        f"{slowest.name} · {seconds(slowest.duration_ms)}" if slowest else "unknown",
    )
    console.print(summary)

    if model_failures:
        table = Table("Model failure breakdown", "Count", box=None)
        for error, amount in sorted(model_failures.items()):
            table.add_row(Text(terminal_text(error)), str(amount))
        console.print(table)

    failures = Counter(t.error_code or "unknown" for t in tools if t.ok is False)
    if failures:
        table = Table("Failure breakdown", "Count", "exec exit codes", box=None)
        for error, amount in sorted(failures.items()):
            exits = Counter(
                t.exit_code
                for t in tools
                if t.ok is False
                and t.name == "exec_command"
                and (t.error_code or "unknown") == error
            )
            codes = ", ".join(
                f"{code if code is not None else 'unknown'} ×{n}" for code, n in exits.items()
            )
            table.add_row(Text(terminal_text(error)), str(amount), codes or "-")
        console.print(table)

    table = Table("Step", "Input", "Output", "Model latency", "Tools", "Tool bytes", box=None)
    for step in profile.timeline():
        if step is None:
            table.add_row("…", "", "", "middle steps omitted", "", "")
            continue
        call = profile.completion(step)
        tool_rows = [t for t in tools if t.step == step]
        latency = seconds(call.duration_ms) if call else "unknown"
        if call and (call.error or not call.finished):
            latency += f" ({call.error or 'unfinished'})"
        table.add_row(
            str(step),
            count(call.input_tokens if call else None),
            count(call.output_tokens if call else None),
            Text(terminal_text(latency)),
            str(sum(t.started for t in tool_rows)),
            size(total([t.result_bytes for t in tool_rows])),
        )
    console.print(table)
    console.print(
        Text(f"Trajectory: {terminal_text(str(path)) if path else 'unknown'}"), soft_wrap=True
    )


class ProfileConsumer:
    """Fan out to diagnostics and the unchanged transcript, isolating diagnostic failures."""

    def __init__(self, consumer: Callable[[RuntimeEvent], Awaitable[None]]) -> None:
        self.consumer = consumer
        self.reset()

    def reset(self) -> None:
        self.profiler = RunProfiler()
        self.failed = False

    async def __call__(self, event: RuntimeEvent) -> None:
        if event.kind == "run_started":
            self.failed = False
        try:
            self.profiler.consume(event)
        except Exception:
            self.failed = True
        await self.consumer(event)

    def render(self, console: Console, limits: Limits, path: Path | None) -> None:
        try:
            if self.failed:
                raise ValueError("Diagnostic collection failed")
            render_profile(
                self.profiler.profile,
                console,
                limits.context_window - limits.max_output_tokens - 1024,
                path,
            )
        except Exception:
            try:
                console.print("Run Profile unavailable (diagnostic error); see session JSONL.")
            except Exception:
                pass


def profile_metrics(profile: RunProfile) -> dict[str, object]:
    """Pure export of existing aggregation; partial reported usage stays a lower bound."""
    first, peak, final = profile.context_inputs
    return {
        "available": profile.run_id is not None,
        "unavailable_reason": None if profile.run_id else "Agent run did not start",
        "steps": profile.steps,
        "duration_ms": profile.duration_ms,
        "model_calls": len(profile.models),
        "retries": sum(m.attempt is not None and m.attempt > 1 for m in profile.models),
        "failed_attempts": sum(profile.model_failures.values()),
        "tool_calls": sum(t.started for t in profile.tools.values()),
        "tool_failures": sum(t.ok is False for t in profile.tools.values()),
        "tool_result_bytes": total([t.result_bytes for t in profile.tools.values()]),
        "usage_coverage": profile.usage_coverage,
        "usage_calls": len(profile.models),
        "usage": {
            name: {
                "reported": profile.tokens(name),
                "coverage": sum(getattr(m, name) is not None for m in profile.models),
                "calls": len(profile.models),
            }
            for name in ("input_tokens", "output_tokens", "total_tokens")
        },
        "first_context": first,
        "peak_context": peak,
        "final_context": final,
        "compaction_calls": sum(m.compaction for m in profile.models),
        "compactions": profile.compactions,
        # Request-local estimates, never provider billing usage or execution output bytes.
        "observation_projection": {
            "requests": len(profile.projections),
            "last": profile.projections[-1] if profile.projections else None,
            "cumulative_full_estimated_tokens": sum(
                p["observation_full_estimated_tokens"] for p in profile.projections
            ),
            "cumulative_projected_estimated_tokens": sum(
                p["observation_projected_estimated_tokens"] for p in profile.projections
            ),
        },
    }
