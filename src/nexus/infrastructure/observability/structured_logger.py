"""Sink-only structured JSON Lines logger for already-redacted telemetry."""

from __future__ import annotations

import asyncio
import json
import sys
from typing import TextIO

from nexus.domain.observability import (
    TelemetryEvent,
    TelemetrySeverity,
    telemetry_payload_dict,
)

_LEVELS = {
    TelemetrySeverity.DEBUG: 10,
    TelemetrySeverity.INFO: 20,
    TelemetrySeverity.WARNING: 30,
    TelemetrySeverity.ERROR: 40,
}


class StructuredLogger:
    """Serialize only TelemetryEvent values and write one complete UTF-8 JSON line."""

    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream or sys.stderr
        self._lock = asyncio.Lock()

    async def record(self, event: TelemetryEvent) -> None:
        level = _LEVELS[event.severity]
        line = json.dumps(
            _event_dict(event), ensure_ascii=False, separators=(",", ":")
        )
        async with self._lock:
            await asyncio.to_thread(self._write, level, line)

    async def flush(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._stream.flush)

    def _write(self, level: int, line: str) -> None:
        del level  # Level selection is deliberately derived only from TelemetrySeverity.
        self._stream.write(f"{line}\n")


def _event_dict(event: TelemetryEvent) -> dict[str, object]:
    return {
        "schema_version": event.schema_version,
        "event_id": event.event_id,
        "event_type": event.event_type.value,
        "timestamp": event.timestamp.isoformat(),
        "sequence": event.sequence,
        "trace_id": event.trace_id,
        "execution_id": event.execution_id,
        "run_id": event.run_id,
        "session_id": event.session_id,
        "span_id": event.span_id,
        "parent_span_id": event.parent_span_id,
        "phase": event.phase.value,
        "severity": event.severity.value,
        "payload": telemetry_payload_dict(event.payload),
        "redaction": {
            "policy_version": event.redaction.policy_version,
            "redacted": event.redaction.redacted,
            "omitted_fields": list(event.redaction.omitted_fields),
            "truncated_fields": list(event.redaction.truncated_fields),
        },
    }
