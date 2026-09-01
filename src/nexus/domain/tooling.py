"""Nexus-owned Day 3 tool, policy, and sandbox values."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

JsonObject = dict[str, object]


class RiskLevel(StrEnum):
    SAFE = "SAFE"
    WRITE = "WRITE"
    DANGEROUS = "DANGEROUS"


class ApprovalDecision(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    DENIED = "DENIED"


class PolicyDecision(StrEnum):
    ALLOWED = "ALLOWED"
    DENIED = "DENIED"


@dataclass(frozen=True, slots=True)
class ToolInvocation:
    invocation_id: str
    tool_name: str
    arguments: JsonObject
    run_id: str
    session_id: str

    def __post_init__(self) -> None:
        _validate_uuid(self.invocation_id, "invocation_id")
        _validate_uuid(self.run_id, "run_id")
        _validate_uuid(self.session_id, "session_id")
        if not self.tool_name:
            raise ValueError("tool_name must not be empty.")
        if any(not isinstance(key, str) for key in self.arguments):
            raise ValueError("Tool argument keys must be strings.")
        if not _is_json_value(self.arguments):
            raise ValueError("Tool arguments must contain only JSON-compatible values.")
        object.__setattr__(self, "arguments", dict(self.arguments))


@dataclass(frozen=True, slots=True)
class ToolError:
    code: str
    message: str
    retryable: bool

    def __post_init__(self) -> None:
        if not self.code or not self.message:
            raise ValueError("ToolError code and message must not be empty.")


@dataclass(frozen=True, slots=True)
class ToolResult:
    invocation_id: str
    tool_name: str
    success: bool
    output: JsonObject | None
    error: ToolError | None
    risk_level: RiskLevel
    policy_decision: PolicyDecision
    approval_decision: ApprovalDecision | None
    duration_ms: int

    def __post_init__(self) -> None:
        _validate_uuid(self.invocation_id, "invocation_id")
        if not self.tool_name:
            raise ValueError("tool_name must not be empty.")
        if self.duration_ms < 0:
            raise ValueError("duration_ms must not be negative.")
        if self.success and self.error is not None:
            raise ValueError("A successful ToolResult cannot contain an error.")
        if self.success and self.policy_decision is not PolicyDecision.ALLOWED:
            raise ValueError("A successful ToolResult must be allowed by policy.")
        if not self.success and self.error is None:
            raise ValueError("A failed ToolResult must contain a ToolError.")
        if self.output is not None and not _is_json_value(self.output):
            raise ValueError("Tool output must contain only JSON-compatible values.")
        if self.output is not None:
            object.__setattr__(self, "output", dict(self.output))


@dataclass(frozen=True, slots=True)
class SandboxRequest:
    operation: str
    argv: list[str]
    cwd: str
    timeout_seconds: float

    def __post_init__(self) -> None:
        if not self.operation:
            raise ValueError("Sandbox operation must not be empty.")
        if not self.argv or any(
            not isinstance(item, str) or not item for item in self.argv
        ):
            raise ValueError("Sandbox argv must contain non-empty strings.")
        if not self.cwd:
            raise ValueError("Sandbox cwd must not be empty.")
        if not 0 < self.timeout_seconds <= 300.0:
            raise ValueError("Sandbox timeout must be in (0, 300].")
        object.__setattr__(self, "argv", list(self.argv))


@dataclass(frozen=True, slots=True)
class SandboxResult:
    argv: list[str]
    cwd: str
    risk_level: RiskLevel
    policy_decision: PolicyDecision
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool
    output_truncated: bool
    error: ToolError | None

    def __post_init__(self) -> None:
        if self.duration_ms < 0:
            raise ValueError("duration_ms must not be negative.")
        if self.policy_decision is PolicyDecision.DENIED and self.exit_code is not None:
            raise ValueError("A denied SandboxResult cannot have an exit code.")
        if self.timed_out and self.exit_code is not None:
            raise ValueError("A timed-out SandboxResult cannot have an exit code.")
        object.__setattr__(self, "argv", list(self.argv))


def risk_rank(risk: RiskLevel) -> int:
    return {
        RiskLevel.SAFE: 0,
        RiskLevel.WRITE: 1,
        RiskLevel.DANGEROUS: 2,
    }[risk]


def _validate_uuid(value: str, field_name: str) -> None:
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a UUID string.") from exc
    if str(parsed) != value:
        raise ValueError(f"{field_name} must use canonical UUID text.")


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
