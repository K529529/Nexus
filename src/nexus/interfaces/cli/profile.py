"""Deterministic, bounded projection of RuntimeEvents for CLI profiling."""

from __future__ import annotations

import re
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

import typer

from nexus.domain.agent_decision import AgentDecisionKind
from nexus.domain.model import ModelCallPhase
from nexus.domain.runtime_events import (
    AgentStepCompleted,
    ErrorOccurred,
    ExecutionPhase,
    FinalResult,
    ModelCallFinished,
    ModelCallStarted,
    PhaseFinished,
    PhaseStarted,
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
_MAX_AGENT_TIMELINE = 30
_MAX_AGENT_TOOL_NAMES = 20
_SAFE_TOOL_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}\Z")


@dataclass(slots=True)
class _AgentStep:
    step_count: int
    kind: AgentDecisionKind
    tool_name: str | None = None
    success: bool | None = None
    duration_ms: int | None = None


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
        self._agent_decisions = {kind: 0 for kind in AgentDecisionKind}
        self._agent_tools: dict[str, int] = {}
        self._other_agent_tools = 0
        self._agent_timeline: list[_AgentStep] = []
        self._pending_agent_action: tuple[str, _AgentStep | None] | None = None
        self._agent_tool_phase = False
        self._active_agent_tool: tuple[str, str, _AgentStep | None] | None = None
        self.llm_calls = 0
        self.tool_calls = 0
        self.agent_steps = 0
        self.replans = 0
        self.repairs = 0
        self.failure_category: str | None = None

    def observe(self, event: RuntimeEvent) -> None:
        if isinstance(event, TaskStarted):
            self._segment_start = event.timestamp
            self._pending_agent_action = None
            self._active_agent_tool = None
            self._agent_tool_phase = False
        elif isinstance(event, (FinalResult, ErrorOccurred, RunInterrupted)):
            self._has_terminal = True
            if self._segment_start is not None:
                self._total_ms += max(
                    0, int((event.timestamp - self._segment_start).total_seconds() * 1000)
                )
                self._segment_start = None
            if isinstance(event, ErrorOccurred):
                self.failure_category = event.failure_category
        elif isinstance(event, PhaseStarted):
            if self._pending_agent_action is not None:
                self._agent_tool_phase = event.phase is ExecutionPhase.AGENT
                if not self._agent_tool_phase:
                    self._pending_agent_action = None
        elif isinstance(event, PhaseFinished):
            self._phase_seen.add(event.phase)
            self._phase_ms[event.phase] += event.duration_ms
            if event.phase is ExecutionPhase.AGENT and self._agent_tool_phase:
                self._pending_agent_action = None
                self._agent_tool_phase = False
                self._active_agent_tool = None
        elif isinstance(event, ModelCallStarted):
            self.llm_calls += 1
            if event.phase is ModelCallPhase.AGENT_STEP:
                self.agent_steps += 1
        elif isinstance(event, ToolStarted):
            self.tool_calls += 1
            if (
                self._agent_tool_phase
                and self._pending_agent_action is not None
                and event.run_id == self._pending_agent_action[0]
            ):
                name = _safe_tool_name(event.tool_name)
                self._count_agent_tool(name)
                step = self._pending_agent_action[1]
                if step is not None:
                    step.tool_name = name
                self._active_agent_tool = (event.run_id, event.invocation_id, step)
                self._pending_agent_action = None
        elif isinstance(event, ModelCallFinished):
            self._record_slow(event.duration_ms, f"{event.phase.value} model")
        elif isinstance(event, ToolFinished):
            self._record_slow(event.duration_ms, _safe_tool_name(event.tool_name))
            active = self._active_agent_tool
            if active is not None and (event.run_id, event.invocation_id) == active[:2]:
                if active[2] is not None:
                    active[2].success = event.success
                    active[2].duration_ms = event.duration_ms
                self._active_agent_tool = None
        elif isinstance(event, AgentStepCompleted):
            self.agent_steps = max(self.agent_steps, event.step_count)
            self._agent_decisions[event.decision_kind] += 1
            step = None
            if len(self._agent_timeline) < _MAX_AGENT_TIMELINE:
                step = _AgentStep(event.step_count, event.decision_kind)
                self._agent_timeline.append(step)
            self._pending_agent_action = (
                (event.run_id, step)
                if event.decision_kind is AgentDecisionKind.TOOL_ACTION
                else None
            )
            self._agent_tool_phase = False
        elif isinstance(event, ReplanOccurred):
            self.replans = max(self.replans, event.replan_count)
        elif isinstance(event, RepairStarted):
            self.repairs = max(self.repairs, event.repair_count)

    def _record_slow(self, duration_ms: int, label: str) -> None:
        self._sequence += 1
        self._slow.append((duration_ms, label, self._sequence))
        self._slow.sort(key=lambda item: (-item[0], item[1], item[2]))
        del self._slow[8:]

    def _count_agent_tool(self, name: str) -> None:
        if name in self._agent_tools:
            self._agent_tools[name] += 1
        elif len(self._agent_tools) < _MAX_AGENT_TOOL_NAMES:
            self._agent_tools[name] = 1
        else:
            self._other_agent_tools += 1

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
        if self._agent_timeline or any(self._agent_decisions.values()):
            result.extend(["", "Agent decisions"])
            result.extend(
                f"{kind.value:<20}{self._agent_decisions[kind]}"
                for kind in AgentDecisionKind
            )
            result.extend(["", "Agent tools"])
            result.extend(
                f"{name:<20}{count}"
                for name, count in sorted(
                    self._agent_tools.items(), key=lambda item: (-item[1], item[0])
                )
            )
            if self._other_agent_tools:
                result.append(f"{'<other tools>':<20}{self._other_agent_tools}")
            if not self._agent_tools and not self._other_agent_tools:
                result.append("(none)")
            result.extend(["", "Agent loop"])
            result.extend(_agent_step_line(step) for step in self._agent_timeline)
            omitted = sum(self._agent_decisions.values()) - len(self._agent_timeline)
            if omitted:
                result.append(f"  ... {omitted} more steps")
        return result

    def render(self) -> None:
        typer.echo("\n".join(self.lines()))


def _safe_tool_name(name: str) -> str:
    return name if _SAFE_TOOL_NAME.fullmatch(name) else "<redacted tool>"


def _agent_step_line(step: _AgentStep) -> str:
    line = f"  {step.step_count:<2} {step.kind.value:<11}"
    if step.kind is not AgentDecisionKind.TOOL_ACTION:
        return line.rstrip()
    if step.tool_name is None:
        return f"{line} unobserved"
    if step.success is None:
        return f"{line} {step.tool_name} incomplete"
    outcome = "PASS" if step.success else "FAIL"
    return f"{line} {step.tool_name} {outcome} {step.duration_ms} ms"
