"""Manual sanitized mapping from Nexus telemetry to the LangSmith client API."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from nexus.domain.observability import (
    TelemetryEvent,
    TelemetryEventType,
    TraceRunFinish,
    TraceRunStart,
    telemetry_payload_dict,
)


class LangSmithClient(Protocol):
    def create_run(
        self, name: str, inputs: dict[str, Any], run_type: str, **kwargs: Any
    ) -> None: ...

    def update_run(self, run_id: object, **kwargs: Any) -> None: ...

    def flush(self, timeout: float | None = None) -> None: ...

    def close(self, timeout: float | None = None) -> None: ...


class LangSmithTracer:
    """Expose no provider objects and submit only pre-redacted safe values."""

    def __init__(
        self,
        client: LangSmithClient,
        *,
        project: str,
        flush_timeout_seconds: float = 2.0,
    ) -> None:
        self._client = client
        self._project = project
        self._flush_timeout_seconds = flush_timeout_seconds
        self._root_events: dict[str, list[dict[str, object]]] = {}
        self._dotted_orders: dict[str, dict[str, str]] = {}
        self._parent_ids: dict[str, dict[str, str]] = {}

    async def start_run(self, start: TraceRunStart) -> None:
        context = start.context
        root_id = UUID(context.execution_id)
        root_dotted_order = _dotted_segment(context.started_at, root_id)
        self._root_events[context.execution_id] = []
        self._dotted_orders[context.execution_id] = {
            context.execution_id: root_dotted_order
        }
        self._parent_ids[context.execution_id] = {}
        await asyncio.to_thread(
            self._client.create_run,
            "nexus.run",
            {},
            "chain",
            id=root_id,
            trace_id=root_id,
            dotted_order=root_dotted_order,
            project_name=self._project,
            start_time=context.started_at,
            outputs={},
            extra={
                "metadata": {
                    "nexus.schema_version": "1.0",
                    "nexus.trace_id": context.trace_id,
                    "nexus.execution_id": context.execution_id,
                    "nexus.run_id": context.run_id,
                    "nexus.session_id": context.session_id,
                    "nexus.is_resume": context.is_resume,
                    "task_character_count": start.task_character_count,
                    "model_provider": start.model_provider,
                    "model_name": start.model_name,
                }
            },
            tags=["nexus", "schema:1.0"],
        )

    async def record(self, event: TelemetryEvent) -> None:
        if event.event_type in _SPAN_START_TYPES:
            await self._create_span(event)
            return
        if event.event_type in _SPAN_FINISH_TYPES:
            await self._finish_span(event)
            return
        root_events = self._root_events.setdefault(event.execution_id, [])
        root_events.append(
            {
                "name": event.event_type.value,
                "time": event.timestamp.isoformat(),
                "kwargs": telemetry_payload_dict(event.payload),
            }
        )
        await asyncio.to_thread(
            self._client.update_run,
            UUID(event.execution_id),
            events=list(root_events),
            **self._run_reference(event.execution_id, event.execution_id),
        )

    async def finish(self, finish: TraceRunFinish) -> None:
        context = finish.context
        output = {
            "execution_outcome": finish.execution_outcome.value,
            "runtime_status": finish.runtime_status.value,
            "terminal_status": (
                None if finish.terminal_status is None else finish.terminal_status.value
            ),
            "duration_ms": finish.duration_ms,
            "step_count": finish.step_count,
            "llm_call_count": finish.llm_call_count,
            "tool_call_count": finish.tool_call_count,
            "replan_count": finish.replan_count,
            "repair_count": finish.repair_count,
            "token_usage": {
                "input_tokens": finish.token_usage.input_tokens,
                "output_tokens": finish.token_usage.output_tokens,
                "total_tokens": finish.token_usage.total_tokens,
                "availability": finish.token_usage.availability.value,
            },
            "changed_file_count": finish.changed_file_count,
            "validation_status": finish.validation_status.value,
            "error_code": finish.error_code,
        }
        await asyncio.to_thread(
            self._client.update_run,
            UUID(context.execution_id),
            end_time=finish.finished_at,
            outputs=output,
            error=finish.error_code,
            **self._run_reference(context.execution_id, context.execution_id),
        )
        await asyncio.to_thread(self._client.flush, self._flush_timeout_seconds)
        self._root_events.pop(context.execution_id, None)
        self._dotted_orders.pop(context.execution_id, None)
        self._parent_ids.pop(context.execution_id, None)

    async def close(self) -> None:
        await asyncio.to_thread(self._client.flush, self._flush_timeout_seconds)
        await asyncio.to_thread(self._client.close, self._flush_timeout_seconds)

    async def _create_span(self, event: TelemetryEvent) -> None:
        if event.span_id is None:
            return
        run_type, name = _span_identity(event)
        span_id = UUID(event.span_id)
        parent_id = event.parent_span_id or event.execution_id
        orders = self._dotted_orders.setdefault(event.execution_id, {})
        parent_dotted_order = orders.get(parent_id)
        if parent_dotted_order is None:
            return
        dotted_order = f"{parent_dotted_order}.{_dotted_segment(event.timestamp, span_id)}"
        orders[event.span_id] = dotted_order
        self._parent_ids.setdefault(event.execution_id, {})[event.span_id] = parent_id
        await asyncio.to_thread(
            self._client.create_run,
            name,
            {},
            run_type,
            id=span_id,
            trace_id=UUID(event.execution_id),
            # Use the SDK's documented batched wire form.
            parent_run_id=str(parent_id),
            dotted_order=dotted_order,
            project_name=self._project,
            start_time=event.timestamp,
            outputs={},
            extra={"metadata": telemetry_payload_dict(event.payload)},
            tags=[f"phase:{event.phase.value}", "schema:1.0"],
        )

    async def _finish_span(self, event: TelemetryEvent) -> None:
        if event.span_id is None:
            return
        payload = telemetry_payload_dict(event.payload)
        error_code = payload.get("error_code")
        await asyncio.to_thread(
            self._client.update_run,
            UUID(event.span_id),
            end_time=event.timestamp,
            outputs=payload,
            error=error_code if isinstance(error_code, str) else None,
            **self._run_reference(event.execution_id, event.span_id),
        )

    def _run_reference(self, execution_id: str, span_id: str) -> dict[str, object]:
        dotted_order = self._dotted_orders.get(execution_id, {}).get(span_id)
        if dotted_order is None:
            return {}
        reference: dict[str, object] = {
            "trace_id": UUID(execution_id),
            "dotted_order": dotted_order,
        }
        parent_id = self._parent_ids.get(execution_id, {}).get(span_id)
        if parent_id is not None:
            # A separate batched update is validated independently from create,
            # so it must retain the child relationship as well.
            reference["parent_run_id"] = parent_id
        return reference


_SPAN_START_TYPES: frozenset[TelemetryEventType] = frozenset(
    {
        TelemetryEventType.MODEL_CALL_STARTED,
        TelemetryEventType.TOOL_STARTED,
        TelemetryEventType.VALIDATION_STARTED,
    }
)
_SPAN_FINISH_TYPES: frozenset[TelemetryEventType] = frozenset(
    {
        TelemetryEventType.MODEL_CALL_FINISHED,
        TelemetryEventType.TOOL_FINISHED,
        TelemetryEventType.VALIDATION_FINISHED,
    }
)


def _span_identity(event: TelemetryEvent) -> tuple[str, str]:
    if event.event_type is TelemetryEventType.MODEL_CALL_STARTED:
        return "llm", "nexus.model"
    if event.event_type is TelemetryEventType.TOOL_STARTED:
        tool_name = event.payload.get("tool_name")
        safe_name = tool_name if isinstance(tool_name, str) else "unknown"
        return "tool", f"nexus.tool.{safe_name}"
    return "chain", "nexus.validation"


def _dotted_segment(timestamp: datetime, run_id: UUID) -> str:
    """Build the provider hierarchy segment without importing LangSmith internals."""

    return timestamp.strftime("%Y%m%dT%H%M%S%fZ") + str(run_id)
