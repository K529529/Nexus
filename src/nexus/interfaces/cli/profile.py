"""Deterministic, bounded projection of RuntimeEvents for CLI profiling."""

from __future__ import annotations

import time
from collections import defaultdict
from datetime import datetime

import typer

from nexus.domain.model import ModelCallPhase
from nexus.domain.runtime_events import (
    AgentStepCompleted,
    ErrorOccurred,
    ExecutionPhase,
    FinalResult,
    ModelCallFinished,
    ModelCallStarted,
    PhaseFinished,
    RepairStarted,
    ReplanOccurred,
    RunInterrupted,
    RuntimeEvent,
    TaskStarted,
    ToolFinished,
    ToolStarted,
)

_PHASE_LABELS = (
    ("Repository", (ExecutionPhase.REPOSITORY,)),
    ("Context", (ExecutionPhase.CONTEXT,)),
    ("Planning", (ExecutionPhase.PLANNING,)),
    ("Agent", (ExecutionPhase.AGENT,)),
    ("Validation", (ExecutionPhase.VALIDATION,)),
    ("Repair/Replan", (ExecutionPhase.REPAIR, ExecutionPhase.REPLAN)),
)


class ExecutionProfile:
    """Collect event durations without inspecting prompts, tool inputs or file content."""

    def __init__(self) -> None:
        self._created = time.perf_counter()
        self._segment_start: datetime | None = None
        self._has_terminal = False
        self._total_ms = 0
        self._phase_ms: dict[ExecutionPhase, int] = defaultdict(int)
        self._phase_seen: set[ExecutionPhase] = set()
        self._slow: list[tuple[int, str, int]] = []
        self._sequence = 0
        self.llm_calls = 0
        self.tool_calls = 0
        self.agent_steps = 0
        self.replans = 0
        self.repairs = 0
        self.failure_category: str | None = None

    def observe(self, event: RuntimeEvent) -> None:
        if isinstance(event, TaskStarted):
            self._segment_start = event.timestamp
        elif isinstance(event, (FinalResult, ErrorOccurred, RunInterrupted)):
            self._has_terminal = True
            if self._segment_start is not None:
                self._total_ms += max(
                    0, int((event.timestamp - self._segment_start).total_seconds() * 1000)
                )
                self._segment_start = None
            if isinstance(event, ErrorOccurred):
                self.failure_category = event.failure_category
        elif isinstance(event, PhaseFinished):
            self._phase_seen.add(event.phase)
            self._phase_ms[event.phase] += event.duration_ms
        elif isinstance(event, ModelCallStarted):
            self.llm_calls += 1
            if event.phase is ModelCallPhase.AGENT_STEP:
                self.agent_steps += 1
        elif isinstance(event, ToolStarted):
            self.tool_calls += 1
        elif isinstance(event, ModelCallFinished):
            self._record_slow(event.duration_ms, f"{event.phase.value} model")
        elif isinstance(event, ToolFinished):
            self._record_slow(event.duration_ms, event.tool_name)
        elif isinstance(event, AgentStepCompleted):
            self.agent_steps = max(self.agent_steps, event.step_count)
        elif isinstance(event, ReplanOccurred):
            self.replans = max(self.replans, event.replan_count)
        elif isinstance(event, RepairStarted):
            self.repairs = max(self.repairs, event.repair_count)

    def _record_slow(self, duration_ms: int, label: str) -> None:
        self._sequence += 1
        self._slow.append((duration_ms, label, self._sequence))
        self._slow.sort(key=lambda item: (-item[0], item[1], item[2]))
        del self._slow[8:]

    def lines(self) -> list[str]:
        total = self._total_ms
        if self._segment_start is not None:
            elapsed = datetime.now(self._segment_start.tzinfo) - self._segment_start
            seconds = elapsed.total_seconds()
            total += max(
                0,
                int(seconds * 1000),
            )
        if not self._has_terminal and self._segment_start is None:
            total = max(0, int((time.perf_counter() - self._created) * 1000))
        result = ["Execution Profile", f"Total               {total} ms"]
        for label, phases in _PHASE_LABELS:
            if label == "Repair/Replan" and not any(phase in self._phase_seen for phase in phases):
                continue
            value = (
                f"{sum(self._phase_ms[phase] for phase in phases)} ms"
                if any(phase in self._phase_seen for phase in phases)
                else "unobserved"
            )
            result.append(f"{label:<20}{value}")
        result.extend(
            [
                "",
                f"LLM calls           {self.llm_calls}",
                f"Tool calls          {self.tool_calls}",
                f"Agent steps         {self.agent_steps}",
                f"Replans             {self.replans}",
                f"Repairs             {self.repairs}",
            ]
        )
        if self.failure_category is not None:
            result.append(f"Failure category    {self.failure_category}")
        if self._slow:
            result.extend(["", "Slowest operations"])
            result.extend(f"{label:<24}{duration} ms" for duration, label, _ in self._slow)
        return result

    def render(self) -> None:
        typer.echo("\n".join(self.lines()))
