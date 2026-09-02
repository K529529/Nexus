"""Day 4 validation values and deterministic repairability rules."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from nexus.domain.tooling import JsonObject, ToolResult


class ValidationStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


class ValidationConfidence(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class ValidationCheckKind(StrEnum):
    TEST = "TEST"
    BUILD = "BUILD"
    LINT = "LINT"
    TYPE_CHECK = "TYPE_CHECK"
    REPOSITORY_COMMAND = "REPOSITORY_COMMAND"
    GENERATED_TARGETED_TEST = "GENERATED_TARGETED_TEST"
    BASIC_EXECUTION = "BASIC_EXECUTION"
    DIFF_INSPECTION = "DIFF_INSPECTION"


@dataclass(frozen=True, slots=True)
class ValidationCheck:
    check_id: str
    sequence: int
    kind: ValidationCheckKind
    tool_name: str
    arguments: JsonObject
    selection_reason: str
    required: bool

    def __post_init__(self) -> None:
        _validate_uuid(self.check_id, "check_id")
        if self.sequence < 1 or not self.tool_name or not self.selection_reason.strip():
            raise ValueError("ValidationCheck identity and description are invalid.")
        if not _is_json_value(self.arguments):
            raise ValueError("ValidationCheck arguments must be JSON-compatible.")
        object.__setattr__(self, "arguments", dict(self.arguments))


@dataclass(frozen=True, slots=True)
class ValidationCheckResult:
    check: ValidationCheck
    status: ValidationStatus
    tool_result: ToolResult | None
    evidence_summary: str

    def __post_init__(self) -> None:
        if not self.evidence_summary.strip():
            raise ValueError("Validation evidence_summary must not be empty.")
        if self.tool_result is not None and (
            self.tool_result.invocation_id != self.check.check_id
            or self.tool_result.tool_name != self.check.tool_name
        ):
            raise ValueError("Validation Tool evidence has mismatched identity.")


@dataclass(frozen=True, slots=True)
class ValidationPlan:
    checks: tuple[ValidationCheck, ...]

    def __post_init__(self) -> None:
        checks = tuple(self.checks)
        if [check.sequence for check in checks] != list(range(1, len(checks) + 1)):
            raise ValueError("ValidationCheck sequence must be contiguous from one.")
        if len({check.check_id for check in checks}) != len(checks):
            raise ValueError("ValidationCheck IDs must be unique.")
        object.__setattr__(self, "checks", checks)


@dataclass(frozen=True, slots=True)
class ValidationResult:
    selected_checks: tuple[ValidationCheck, ...]
    executed_checks: tuple[ValidationCheckResult, ...]
    status: ValidationStatus
    confidence: ValidationConfidence
    repairable: bool
    repair_count: int
    summary: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "selected_checks", tuple(self.selected_checks))
        object.__setattr__(self, "executed_checks", tuple(self.executed_checks))
        if self.repair_count < 0 or not self.summary.strip():
            raise ValueError("ValidationResult count and summary are invalid.")
        if self.status is not ValidationStatus.FAIL and self.repairable:
            raise ValueError("Only a failed ValidationResult may be repairable.")
        if self.repairable != aggregate_repairable(self.status, self.executed_checks):
            raise ValueError("ValidationResult repairable does not match frozen aggregation.")


_REPAIRABLE_KINDS = {
    ValidationCheckKind.TEST,
    ValidationCheckKind.BUILD,
    ValidationCheckKind.LINT,
    ValidationCheckKind.TYPE_CHECK,
    ValidationCheckKind.GENERATED_TARGETED_TEST,
    ValidationCheckKind.BASIC_EXECUTION,
}


def is_mechanically_repairable(result: ValidationCheckResult) -> bool:
    tool_result = result.tool_result
    return (
        result.status is ValidationStatus.FAIL
        and result.check.kind in _REPAIRABLE_KINDS
        and tool_result is not None
        and tool_result.error is not None
        and tool_result.error.code == "COMMAND_EXIT_NONZERO"
    )


def aggregate_repairable(
    status: ValidationStatus,
    results: tuple[ValidationCheckResult, ...],
) -> bool:
    if status is not ValidationStatus.FAIL:
        return False
    required_failures = tuple(
        result
        for result in results
        if result.check.required and result.status is ValidationStatus.FAIL
    )
    return bool(required_failures) and all(
        is_mechanically_repairable(result) for result in required_failures
    )


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
