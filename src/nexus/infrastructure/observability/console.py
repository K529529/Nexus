"""Safe JSON Lines tracer for explicit local observability."""

from __future__ import annotations

from typing import TextIO

from nexus.domain.observability import TelemetryEvent, TraceRunFinish, TraceRunStart
from nexus.infrastructure.observability.structured_logger import StructuredLogger


class ConsoleTracer:
    """Write only already-redacted TelemetryEvent values to stderr."""

    def __init__(
        self,
        logger: StructuredLogger | None = None,
        *,
        stream: TextIO | None = None,
    ) -> None:
        if logger is not None and stream is not None:
            raise ValueError("Provide a StructuredLogger or stream, not both.")
        self._logger = logger or StructuredLogger(stream)

    async def start_run(self, start: TraceRunStart) -> None:
        return None

    async def record(self, event: TelemetryEvent) -> None:
        await self._logger.record(event)

    async def finish(self, finish: TraceRunFinish) -> None:
        await self._logger.flush()
