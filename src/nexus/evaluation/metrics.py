"""Authoritative Day 8 trace-finish collection for evaluations."""

from nexus.domain.observability import TraceRunFinish
from nexus.evaluation.models import EvalMetrics


class InMemoryEvalTraceCollector:
    def __init__(self) -> None:
        self._finishes: dict[str, TraceRunFinish] = {}

    def record_finish(self, finish: TraceRunFinish) -> None:
        self._finishes[finish.context.run_id] = finish

    def get_finish(self, run_id: str) -> TraceRunFinish | None:
        return self._finishes.get(run_id)


def metrics_from_finish(finish: TraceRunFinish) -> EvalMetrics:
    return EvalMetrics(
        agent_steps=finish.step_count,
        llm_calls=finish.llm_call_count,
        tool_calls=finish.tool_call_count,
        replans=finish.replan_count,
        repairs=finish.repair_count,
        latency_ms=finish.duration_ms,
        token_usage=finish.token_usage,
    )
