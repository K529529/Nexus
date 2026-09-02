"""In-process Day 4 event and Tool-attempt ledgers shared by composed services."""

from __future__ import annotations

from collections import defaultdict

from nexus.domain.runtime_events import ApprovalRequested, RuntimeEvent
from nexus.domain.tooling import ToolInvocation, ToolResult


class ToolExecutionLedger:
    def __init__(self) -> None:
        self._counts: dict[str, int] = defaultdict(int)
        self._model_counts: dict[str, int] = defaultdict(int)
        self._step_counts: dict[str, int] = defaultdict(int)
        self._repair_counts: dict[str, int] = defaultdict(int)
        self._results: dict[str, list[ToolResult]] = defaultdict(list)
        self._restored_runs: set[str] = set()

    def restore(
        self,
        run_id: str,
        *,
        tool_call_count: int,
        model_call_count: int,
        step_count: int,
        repair_count: int,
        tool_results: tuple[ToolResult, ...],
    ) -> None:
        """Merge one durable checkpoint snapshot into this process-local ledger."""

        if run_id not in self._restored_runs:
            self._counts[run_id] += tool_call_count
            self._model_counts[run_id] += model_call_count
            self._step_counts[run_id] += step_count
            self._repair_counts[run_id] += repair_count
            current_results = self._results[run_id]
            checkpoint_ids = {item.invocation_id for item in tool_results}
            self._results[run_id] = [
                *tool_results,
                *(
                    item
                    for item in current_results
                    if item.invocation_id not in checkpoint_ids
                ),
            ]
            self._restored_runs.add(run_id)
            return

        self._counts[run_id] = max(self._counts[run_id], tool_call_count)
        self._model_counts[run_id] = max(
            self._model_counts[run_id], model_call_count
        )
        self._step_counts[run_id] = max(self._step_counts[run_id], step_count)
        self._repair_counts[run_id] = max(self._repair_counts[run_id], repair_count)
        known_ids = {item.invocation_id for item in self._results[run_id]}
        missing_results = [
            item for item in tool_results if item.invocation_id not in known_ids
        ]
        if missing_results:
            self._results[run_id] = [*missing_results, *self._results[run_id]]

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

    def begin_step(self, run_id: str) -> None:
        self._step_counts[run_id] += 1

    def step_count(self, run_id: str) -> int:
        return self._step_counts[run_id]

    def begin_repair(self, run_id: str) -> None:
        self._repair_counts[run_id] += 1

    def repair_count(self, run_id: str) -> int:
        return self._repair_counts[run_id]

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
