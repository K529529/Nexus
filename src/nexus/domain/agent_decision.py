"""Structured Day 4 Agent decision and deterministic observation values."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from nexus.domain.exploration import WorkingContext
from nexus.domain.planning import Plan, RepairGuidance
from nexus.domain.tooling import JsonObject
from nexus.domain.validation import ValidationResult


class AgentDecisionKind(StrEnum):
    TOOL_ACTION = "TOOL_ACTION"
    CONTINUE = "CONTINUE"
    TASK_READY = "TASK_READY"


class AgentRoute(StrEnum):
    TOOL_REQUIRED = "TOOL_REQUIRED"
    CONTINUE = "CONTINUE"
    TASK_READY = "TASK_READY"


class ObserveRoute(StrEnum):
    REPLAN_REQUIRED = "REPLAN_REQUIRED"
    CONTINUE = "CONTINUE"


class ValidationRoute(StrEnum):
    PASSED = "PASSED"
    REPAIR_AVAILABLE = "REPAIR_AVAILABLE"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class ToolAction:
    tool_name: str
    arguments: JsonObject

    def __post_init__(self) -> None:
        if not self.tool_name:
            raise ValueError("ToolAction tool_name must not be empty.")
        if not _is_json_value(self.arguments):
            raise ValueError("ToolAction arguments must be JSON-compatible.")
        object.__setattr__(self, "arguments", dict(self.arguments))


@dataclass(frozen=True, slots=True)
class AgentDecision:
    kind: AgentDecisionKind
    action: ToolAction | None
    summary: str

    def __post_init__(self) -> None:
        if not self.summary.strip():
            raise ValueError("AgentDecision summary must not be empty.")
        if (self.kind is AgentDecisionKind.TOOL_ACTION) != (self.action is not None):
            raise ValueError("AgentDecision action does not match its kind.")


@dataclass(frozen=True, slots=True)
class Observation:
    invocation_id: str
    tool_name: str
    success: bool
    evidence_summary: str
    error_code: str | None
    replan_reason: str | None

    def __post_init__(self) -> None:
        _validate_uuid(self.invocation_id, "invocation_id")
        if not self.tool_name or not self.evidence_summary.strip():
            raise ValueError("Observation Tool identity and evidence must not be empty.")
        if self.error_code == "PLAN_SCOPE_DENIED":
            if not (self.replan_reason or "").strip():
                raise ValueError("PLAN_SCOPE_DENIED requires a material Replan reason.")
        elif self.replan_reason is not None:
            raise ValueError("Day 4 permits no additional semantic Replan trigger.")


@dataclass(frozen=True, slots=True)
class AgentDecisionRequest:
    task: str
    context: WorkingContext
    plan: Plan
    observations: tuple[Observation, ...]
    validation_result: ValidationResult | None = None
    repair_guidance: RepairGuidance | None = None


def _validate_uuid(value: str, field_name: str) -> None:
    try:
        parsed = UUID(value)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a canonical UUID string.") from exc
    if str(parsed) != value:
        raise ValueError(f"{field_name} must be a canonical UUID string.")


def _is_json_value(value: object) -> bool:
    if value is None or isinstance(value, (str, int, float, bool)):
        return True
    if isinstance(value, list):
        return all(_is_json_value(item) for item in value)
    if isinstance(value, dict):
        return all(
            isinstance(key, str) and _is_json_value(item)
            for key, item in value.items()
        )
    return False
