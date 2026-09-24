"""Nexus-owned Day 8 observability values, independent of concrete tracers."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from uuid import UUID

from nexus.domain.model import TokenUsage
from nexus.domain.planning import TerminalStatus
from nexus.domain.runtime_events import RuntimeStatus


class ExecutionOutcome(StrEnum):
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"


class TraceValidationStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"
    NOT_RUN = "NOT_RUN"


class TelemetrySeverity(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class RuntimePhase(StrEnum):
    RUN = "RUN"
    REPOSITORY = "REPOSITORY"
    CONTEXT = "CONTEXT"
    PLAN = "PLAN"
    APPROVAL = "APPROVAL"
    AGENT = "AGENT"
    MODEL = "MODEL"
    TOOL = "TOOL"
    REPLAN = "REPLAN"
    VALIDATION = "VALIDATION"
    REPAIR = "REPAIR"
    CHANGE = "CHANGE"
    ERROR = "ERROR"
    OBSERVABILITY = "OBSERVABILITY"


class TelemetryEventType(StrEnum):
    RUN_STARTED = "run.started"
    PHASE_STARTED = "phase.started"
    PHASE_FINISHED = "phase.finished"
    REPOSITORY_EXPLORED = "repository.explored"
    CONTEXT_BUILT = "context.built"
    PLAN_CREATED = "plan.created"
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_RESOLVED = "approval.resolved"
    AGENT_STEP_COMPLETED = "agent_step.completed"
    MODEL_CALL_STARTED = "model_call.started"
    MODEL_CALL_FINISHED = "model_call.finished"
    TOOL_STARTED = "tool.started"
    TOOL_FINISHED = "tool.finished"
    REPLAN_OCCURRED = "replan.occurred"
    VALIDATION_STARTED = "validation.started"
    VALIDATION_FINISHED = "validation.finished"
    REPAIR_STARTED = "repair.started"
    CHANGED_FILE_RECORDED = "changed_file.recorded"
    RUN_INTERRUPTED = "run.interrupted"
    RUN_FINISHED = "run.finished"
    ERROR_OCCURRED = "error.occurred"
    OBSERVABILITY_WARNING = "observability.warning"


EVENT_PHASES: Mapping[TelemetryEventType, RuntimePhase] = MappingProxyType(
    {
        TelemetryEventType.RUN_STARTED: RuntimePhase.RUN,
        TelemetryEventType.PHASE_STARTED: RuntimePhase.RUN,
        TelemetryEventType.PHASE_FINISHED: RuntimePhase.RUN,
        TelemetryEventType.RUN_INTERRUPTED: RuntimePhase.RUN,
        TelemetryEventType.RUN_FINISHED: RuntimePhase.RUN,
        TelemetryEventType.REPOSITORY_EXPLORED: RuntimePhase.REPOSITORY,
        TelemetryEventType.CONTEXT_BUILT: RuntimePhase.CONTEXT,
        TelemetryEventType.PLAN_CREATED: RuntimePhase.PLAN,
        TelemetryEventType.APPROVAL_REQUESTED: RuntimePhase.APPROVAL,
        TelemetryEventType.APPROVAL_RESOLVED: RuntimePhase.APPROVAL,
        TelemetryEventType.AGENT_STEP_COMPLETED: RuntimePhase.AGENT,
        TelemetryEventType.MODEL_CALL_STARTED: RuntimePhase.MODEL,
        TelemetryEventType.MODEL_CALL_FINISHED: RuntimePhase.MODEL,
        TelemetryEventType.TOOL_STARTED: RuntimePhase.TOOL,
        TelemetryEventType.TOOL_FINISHED: RuntimePhase.TOOL,
        TelemetryEventType.REPLAN_OCCURRED: RuntimePhase.REPLAN,
        TelemetryEventType.VALIDATION_STARTED: RuntimePhase.VALIDATION,
        TelemetryEventType.VALIDATION_FINISHED: RuntimePhase.VALIDATION,
        TelemetryEventType.REPAIR_STARTED: RuntimePhase.REPAIR,
        TelemetryEventType.CHANGED_FILE_RECORDED: RuntimePhase.CHANGE,
        TelemetryEventType.ERROR_OCCURRED: RuntimePhase.ERROR,
        TelemetryEventType.OBSERVABILITY_WARNING: RuntimePhase.OBSERVABILITY,
    }
)


@dataclass(frozen=True, slots=True, kw_only=True)
class RunExecutionContext:
    trace_id: str
    execution_id: str
    run_id: str
    session_id: str | None
    is_resume: bool
    started_at: datetime

    def __post_init__(self) -> None:
        _validate_uuid(self.trace_id, "trace_id")
        _validate_uuid(self.execution_id, "execution_id")
        _validate_uuid(self.run_id, "run_id")
        if self.session_id is not None:
            _validate_uuid(self.session_id, "session_id")
        if self.trace_id != self.run_id:
            raise ValueError("trace_id must equal run_id for Nexus V1.")
        _validate_utc(self.started_at, "started_at")


@dataclass(frozen=True, slots=True, kw_only=True)
class RedactionMetadata:
    policy_version: str = "1.0"
    redacted: bool = False
    omitted_fields: tuple[str, ...] = ()
    truncated_fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.policy_version != "1.0":
            raise ValueError("Unsupported redaction policy version.")
        omitted = tuple(self.omitted_fields)
        truncated = tuple(self.truncated_fields)
        if any(not value or len(value) > 256 for value in (*omitted, *truncated)):
            raise ValueError("Redaction field categories are invalid.")
        object.__setattr__(self, "omitted_fields", omitted)
        object.__setattr__(self, "truncated_fields", truncated)
        if self.redacted != bool(omitted or truncated):
            raise ValueError("Redaction metadata flag is inconsistent.")


@dataclass(frozen=True, slots=True, kw_only=True)
class TelemetryEvent:
    event_id: str
    event_type: TelemetryEventType
    timestamp: datetime
    sequence: int
    trace_id: str
    execution_id: str
    run_id: str
    session_id: str | None
    span_id: str | None
    parent_span_id: str | None
    phase: RuntimePhase
    severity: TelemetrySeverity
    payload: Mapping[str, object]
    redaction: RedactionMetadata
    schema_version: str = field(default="1.0", init=False)

    def __post_init__(self) -> None:
        if self.schema_version != "1.0":
            raise ValueError("Unsupported telemetry schema version.")
        for name, value in (
            ("event_id", self.event_id),
            ("trace_id", self.trace_id),
            ("execution_id", self.execution_id),
            ("run_id", self.run_id),
        ):
            _validate_uuid(value, name)
        if self.session_id is not None:
            _validate_uuid(self.session_id, "session_id")
        if self.span_id is not None:
            _validate_uuid(self.span_id, "span_id")
        if self.parent_span_id is not None:
            _validate_uuid(self.parent_span_id, "parent_span_id")
        if self.trace_id != self.run_id:
            raise ValueError("Telemetry trace_id must equal run_id.")
        if self.sequence < 1:
            raise ValueError("Telemetry sequence must be positive.")
        _validate_utc(self.timestamp, "timestamp")
        if EVENT_PHASES[self.event_type] is not self.phase:
            raise ValueError("Telemetry RuntimePhase does not match event type.")
        frozen = _freeze_json(dict(self.payload), depth=0)
        success_value = self.payload.get("success")
        model_call_success = success_value if isinstance(success_value, bool) else None
        expected_severity = telemetry_severity(
            self.event_type,
            model_call_success=model_call_success,
        )
        if self.severity is not expected_severity:
            raise ValueError("Telemetry severity does not match the Day 8 mapping.")
        serialized = json.dumps(_thaw_json(frozen), separators=(",", ":"), ensure_ascii=False)
        if len(serialized.encode("utf-8")) > 32 * 1024:
            raise ValueError("Telemetry payload exceeds the 32 KiB limit.")
        object.__setattr__(self, "payload", frozen)


@dataclass(frozen=True, slots=True, kw_only=True)
class TraceRunStart:
    context: RunExecutionContext
    task_character_count: int
    model_provider: str | None
    model_name: str | None

    def __post_init__(self) -> None:
        if self.task_character_count < 0:
            raise ValueError("task_character_count must not be negative.")
        _validate_safe_identifier(self.model_provider, "model_provider")
        _validate_safe_identifier(self.model_name, "model_name")


@dataclass(frozen=True, slots=True, kw_only=True)
class TraceRunFinish:
    context: RunExecutionContext
    finished_at: datetime
    execution_outcome: ExecutionOutcome
    runtime_status: RuntimeStatus
    terminal_status: TerminalStatus | None
    duration_ms: int
    step_count: int
    llm_call_count: int
    tool_call_count: int
    replan_count: int
    repair_count: int
    token_usage: TokenUsage
    changed_file_count: int
    validation_status: TraceValidationStatus
    error_code: str | None

    def __post_init__(self) -> None:
        _validate_utc(self.finished_at, "finished_at")
        counters = (
            self.duration_ms,
            self.step_count,
            self.llm_call_count,
            self.tool_call_count,
            self.replan_count,
            self.repair_count,
            self.changed_file_count,
        )
        if any(value < 0 for value in counters):
            raise ValueError("Trace finish counters and duration must not be negative.")
        if self.error_code is not None:
            _validate_safe_identifier(self.error_code, "error_code")
        if self.execution_outcome.value != self.runtime_status.value:
            raise ValueError("Execution outcome must preserve the terminal RuntimeStatus.")
        if self.runtime_status is RuntimeStatus.COMPLETED:
            if self.terminal_status is not TerminalStatus.SUCCEEDED:
                raise ValueError("Completed trace requires SUCCEEDED terminal status.")
        elif self.runtime_status is RuntimeStatus.FAILED:
            if self.terminal_status is TerminalStatus.SUCCEEDED:
                raise ValueError("Failed trace cannot carry SUCCEEDED terminal status.")
        elif self.runtime_status is RuntimeStatus.INTERRUPTED:
            if self.terminal_status is not None:
                raise ValueError("Interrupted trace cannot carry a terminal status.")
        else:
            raise ValueError("Trace finish requires a terminal RuntimeStatus.")


def telemetry_severity(
    event_type: TelemetryEventType, *, model_call_success: bool | None = None
) -> TelemetrySeverity:
    if event_type is TelemetryEventType.OBSERVABILITY_WARNING:
        return TelemetrySeverity.WARNING
    if event_type is TelemetryEventType.ERROR_OCCURRED:
        return TelemetrySeverity.ERROR
    if (
        event_type is TelemetryEventType.MODEL_CALL_FINISHED
        and model_call_success is False
    ):
        return TelemetrySeverity.ERROR
    return TelemetrySeverity.INFO


def telemetry_payload_dict(value: Mapping[str, object]) -> dict[str, object]:
    """Return a JSON-compatible mutable copy for sink serialization."""

    thawed = _thaw_json(value)
    if not isinstance(thawed, dict):
        raise ValueError("Telemetry payload root must be an object.")
    return thawed


def _validate_uuid(value: str, field_name: str) -> None:
    try:
        parsed = UUID(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a UUID string.") from exc
    if str(parsed) != value:
        raise ValueError(f"{field_name} must use canonical UUID text.")


def _validate_utc(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be timezone-aware UTC.")


def _validate_safe_identifier(value: str | None, field_name: str) -> None:
    if value is not None and (not value.strip() or len(value) > 256):
        raise ValueError(f"{field_name} must be a bounded non-empty identifier.")


def _freeze_json(value: object, *, depth: int) -> object:
    if depth > 4:
        raise ValueError("Telemetry payload exceeds maximum depth.")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("Telemetry payload keys must be strings.")
        return MappingProxyType(
            {key: _freeze_json(item, depth=depth + 1) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item, depth=depth + 1) for item in value)
    raise ValueError("Telemetry payload must contain only JSON-compatible values.")


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value
