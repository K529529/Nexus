"""Typed and safely serializable Day 1 runtime events."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from nexus.domain.tooling import ApprovalDecision, PolicyDecision, RiskLevel


class RuntimeStatus(StrEnum):
    """Terminal and non-terminal statuses available through Day 3."""

    STARTED = "STARTED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    INTERRUPTED = "INTERRUPTED"
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
            key: _safe_value(value)
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
class RunInterrupted(RuntimeEvent):
    """A run reached a durable checkpoint and may be resumed later."""

    status: RuntimeStatus = field(default=RuntimeStatus.INTERRUPTED, init=False)
    message: str = "Run interrupted; resume it with the session command."


@dataclass(frozen=True, slots=True, kw_only=True)
class ErrorOccurred(RuntimeEvent):
    """A safe terminal failure event."""

    status: RuntimeStatus = field(default=RuntimeStatus.FAILED, init=False)
    code: str
    message: str
    retryable: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class ApprovalRequested(RuntimeEvent):
    """A WRITE operation is waiting for its selected approval policy."""

    status: RuntimeStatus = field(default=RuntimeStatus.AWAITING_APPROVAL, init=False)
    approval_id: str
    invocation_id: str
    operation: str
    risk_level: RiskLevel
    resource_or_command_summary: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolStarted(RuntimeEvent):
    """A governed invocation entered Tool Runtime with its proposed risk."""

    status: RuntimeStatus = field(default=RuntimeStatus.STARTED, init=False)
    invocation_id: str
    tool_name: str
    risk_level: RiskLevel


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolFinished(RuntimeEvent):
    """A Tool-local outcome that does not terminalize the active Run."""

    status: RuntimeStatus = field(default=RuntimeStatus.STARTED, init=False)
    invocation_id: str
    tool_name: str
    success: bool
    risk_level: RiskLevel
    policy_decision: PolicyDecision
    approval_decision: ApprovalDecision | None
    duration_ms: int
    error_code: str | None

    def __post_init__(self) -> None:
        RuntimeEvent.__post_init__(self)
        if self.duration_ms < 0:
            raise ValueError("ToolFinished duration_ms must not be negative.")
        if self.success and self.error_code is not None:
            raise ValueError("A successful ToolFinished cannot have an error code.")
        if not self.success and self.error_code is None:
            raise ValueError("A failed ToolFinished must have an error code.")


def _safe_value(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _safe_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_value(item) for item in value]
    return value
