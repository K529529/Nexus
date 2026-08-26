"""Typed and safely serializable Day 1 runtime events."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class RuntimeStatus(StrEnum):
    """Terminal and non-terminal statuses available during Day 1."""

    STARTED = "STARTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True, kw_only=True)
class RuntimeEvent:
    """Nexus-owned base event with stable structured serialization."""

    run_id: str
    session_id: str | None
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    status: RuntimeStatus

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ValueError("RuntimeEvent timestamp must be timezone-aware.")

    def to_dict(self) -> dict[str, Any]:
        """Serialize without leaking framework/provider objects."""

        values = asdict(self)
        payload = {
            key: value
            for key, value in values.items()
            if key not in {"run_id", "session_id", "timestamp", "status"}
        }
        return {
            "type": type(self).__name__,
            "run_id": self.run_id,
            "session_id": self.session_id,
            "timestamp": self.timestamp.isoformat(),
            "status": self.status.value,
            "payload": payload,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class TaskStarted(RuntimeEvent):
    """The runtime accepted a task and opened a new run."""

    status: RuntimeStatus = field(default=RuntimeStatus.STARTED, init=False)
    task: str


@dataclass(frozen=True, slots=True, kw_only=True)
class FinalResult(RuntimeEvent):
    """The terminal successful result for a Day 1 task."""

    status: RuntimeStatus = field(default=RuntimeStatus.COMPLETED, init=False)
    content: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ErrorOccurred(RuntimeEvent):
    """A safe terminal failure event."""

    status: RuntimeStatus = field(default=RuntimeStatus.FAILED, init=False)
    code: str
    message: str
    retryable: bool
