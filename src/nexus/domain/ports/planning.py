"""Day 4 planning port."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from nexus.domain.exploration import WorkingContext
from nexus.domain.planning import Plan, PlanKind, RepairGuidance
from nexus.domain.validation import ValidationResult


@dataclass(frozen=True, slots=True)
class PlanningRequest:
    task: str
    context: WorkingContext
    kind: PlanKind
    previous_plan: Plan | None
    reason: str | None
    run_id: str
    session_id: str

    def __post_init__(self) -> None:
        _validate_canonical_uuid(self.run_id, "run_id")
        _validate_canonical_uuid(self.session_id, "session_id")
        if self.kind is PlanKind.INITIAL:
            if self.previous_plan is not None:
                raise ValueError("INITIAL PlanningRequest cannot contain a previous Plan.")
        else:
            previous = self.previous_plan
            if previous is None:
                raise ValueError("REPLAN PlanningRequest requires a previous Plan.")
            if self.run_id != previous.run_id or self.session_id != previous.session_id:
                raise ValueError("REPLAN cannot change Run or Session identity.")


@dataclass(frozen=True, slots=True)
class RepairPlanningRequest:
    task: str
    context: WorkingContext
    plan: Plan
    validation_result: ValidationResult
    repair_attempt: int


class Planner(Protocol):
    async def create_plan(self, request: PlanningRequest) -> Plan: ...

    async def create_repair_guidance(
        self,
        request: RepairPlanningRequest,
    ) -> RepairGuidance: ...


def _validate_canonical_uuid(value: str, field_name: str) -> None:
    try:
        parsed = UUID(value)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a canonical UUID string.") from exc
    if str(parsed) != value:
        raise ValueError(f"{field_name} must be a canonical UUID string.")
