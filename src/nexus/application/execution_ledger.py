"""In-process Day 4 event and Tool-attempt ledgers shared by composed services."""

from __future__ import annotations

from collections import defaultdict

from nexus.domain.runtime_events import ApprovalRequested, RuntimeEvent
from nexus.domain.tooling import ToolInvocation, ToolResult


class ToolExecutionLedger:
    def __init__(self) -> None:
        self._counts: dict[str, int] = defaultdict(int)
        self._model_counts: dict[str, int] = defaultdict(int)
        self._results: dict[str, list[ToolResult]] = defaultdict(list)

    def begin(self, invocation: ToolInvocation) -> None:
        self._counts[invocation.run_id] += 1

    def record(self, invocation: ToolInvocation, result: ToolResult) -> None:
        self._results[invocation.run_id].append(result)

    def count(self, run_id: str) -> int:
        return self._counts[run_id]

    def begin_model(self, run_id: str) -> None:
        self._model_counts[run_id] += 1

    def model_count(self, run_id: str) -> int:
        return self._model_counts[run_id]

    def results(self, run_id: str) -> tuple[ToolResult, ...]:
        return tuple(self._results[run_id])


class RuntimeEventBuffer:
    def __init__(self) -> None:
        self._events: dict[str, list[RuntimeEvent]] = defaultdict(list)
        self._seen_plan_approvals: set[str] = set()

    async def emit(self, event: RuntimeEvent) -> None:
        if isinstance(event, ApprovalRequested) and event.plan_id is not None:
            if event.approval_id in self._seen_plan_approvals:
                return
            self._seen_plan_approvals.add(event.approval_id)
        self._events[event.run_id].append(event)

    def drain(self, run_id: str) -> tuple[RuntimeEvent, ...]:
        events = tuple(self._events.pop(run_id, ()))
        self._seen_plan_approvals.difference_update(
            event.approval_id
            for event in events
            if isinstance(event, ApprovalRequested) and event.plan_id is not None
        )
        return events
