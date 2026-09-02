"""Nexus-owned Day 3 approval lifecycle entity."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from nexus.domain.tooling import ApprovalDecision, RiskLevel

_ACTORS = {"runtime", "user", "auto_policy", "security_policy"}


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    approval_id: str
    run_id: str
    session_id: str
    operation: str
    risk_level: RiskLevel
    resource_or_command_summary: str
    decision: ApprovalDecision
    actor: str
    reason: str | None
    created_at: datetime
    decided_at: datetime | None

    def __post_init__(self) -> None:
        _validate_uuid(self.approval_id, "approval_id")
        _validate_uuid(self.run_id, "run_id")
        _validate_uuid(self.session_id, "session_id")
        _validate_timestamp(self.created_at, "created_at")
        if self.decided_at is not None:
            _validate_timestamp(self.decided_at, "decided_at")
        if not self.operation or not self.resource_or_command_summary:
            raise ValueError("Approval operation, summary, and actor must not be empty.")
        if self.actor not in _ACTORS:
            raise ValueError("Approval actor is not a frozen Day 3 actor.")
        if self.decision is ApprovalDecision.PENDING:
            if self.decided_at is not None or self.actor != "runtime":
                raise ValueError("A pending approval must have runtime actor and no decided_at.")
        elif self.decided_at is None:
            raise ValueError("A terminal approval must have decided_at.")
        elif self.decision is ApprovalDecision.APPROVED and not (
            self.actor == "user"
            or self.actor == "auto_policy" and self.operation == "approve_plan"
        ):
            raise ValueError("Only the user or scoped Plan auto policy may approve.")
        elif self.decision is ApprovalDecision.DENIED and self.actor == "runtime":
            raise ValueError("The runtime actor cannot own a terminal denial.")


def _validate_uuid(value: str, field_name: str) -> None:
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a UUID string.") from exc
    if str(parsed) != value:
        raise ValueError(f"{field_name} must use canonical UUID text.")


def _validate_timestamp(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware.")
