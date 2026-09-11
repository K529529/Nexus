"""Provider-neutral observability sink boundary."""

from typing import Protocol

from nexus.domain.observability import TelemetryEvent, TraceRunFinish, TraceRunStart


class Tracer(Protocol):
    async def start_run(self, start: TraceRunStart) -> None: ...

    async def record(self, event: TelemetryEvent) -> None: ...

    async def finish(self, finish: TraceRunFinish) -> None: ...
