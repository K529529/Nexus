"""Typed and safely serializable Day 1 runtime events."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from nexus.domain.agent_decision import AgentDecisionKind
from nexus.domain.model import ModelCallPhase, TokenUsage
from nexus.domain.planning import ChangedFile, ChangeKind, PlanKind, TerminalStatus
from nexus.domain.tooling import ApprovalDecision, PolicyDecision, RiskLevel
from nexus.domain.validation import (
    ValidationCheckKind,
    ValidationConfidence,
    ValidationResult,
    ValidationStatus,
)
from nexus.errors import SAFE_FAILURE_CATEGORIES


class RuntimeStatus(StrEnum):
    """Terminal and non-terminal statuses available through Day 3."""

    STARTED = "STARTED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    INTERRUPTED = "INTERRUPTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class ApprovalSubject(StrEnum):
    TOOL = "TOOL"
    PLAN = "PLAN"


class ExecutionPhase(StrEnum):
    REPOSITORY = "REPOSITORY"
    CONTEXT = "CONTEXT"
    PLANNING = "PLANNING"
    AGENT = "AGENT"
    VALIDATION = "VALIDATION"
    REPLAN = "REPLAN"
    REPAIR = "REPAIR"


class ApprovalActorCategory(StrEnum):
    USER = "USER"
    POLICY = "POLICY"


class TraceSink(StrEnum):
    CONSOLE = "CONSOLE"
    LANGSMITH = "LANGSMITH"
    LOGGER = "LOGGER"
    TELEMETRY = "TELEMETRY"


class TraceOperation(StrEnum):
    START = "START"
    RECORD = "RECORD"
    FINISH = "FINISH"
    FLUSH = "FLUSH"
    ENRICH = "ENRICH"
    REDACT = "REDACT"


class TraceFallback(StrEnum):
    CONSOLE = "CONSOLE"
    LANGSMITH = "LANGSMITH"
    NOOP = "NOOP"
    NONE = "NONE"


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
class PhaseStarted(RuntimeEvent):
    """A graph node entered a user-relevant phase."""

    status: RuntimeStatus = field(default=RuntimeStatus.STARTED, init=False)
    phase: ExecutionPhase


@dataclass(frozen=True, slots=True, kw_only=True)
class PhaseFinished(RuntimeEvent):
    """Elapsed node time, including unsuccessful attempts."""

    status: RuntimeStatus = field(default=RuntimeStatus.STARTED, init=False)
    phase: ExecutionPhase
    duration_ms: int
    success: bool

    def __post_init__(self) -> None:
        RuntimeEvent.__post_init__(self)
        if self.duration_ms < 0:
            raise ValueError("Phase duration must not be negative.")


@dataclass(frozen=True, slots=True, kw_only=True)
class FinalResult(RuntimeEvent):
    """A truthful successful or unsuccessful terminal task result."""

    status: RuntimeStatus = field(default=RuntimeStatus.COMPLETED, init=False)
    content: str
    terminal_status: TerminalStatus = TerminalStatus.SUCCEEDED
    changed_files: tuple[ChangedFile, ...] = ()
    diff: str | None = ""
    validation_result: ValidationResult | None = None
    includes_preexisting_changes: bool = False

    def __post_init__(self) -> None:
        RuntimeEvent.__post_init__(self)
        object.__setattr__(self, "changed_files", tuple(self.changed_files))
        object.__setattr__(
            self,
            "status",
            RuntimeStatus.COMPLETED
            if self.terminal_status is TerminalStatus.SUCCEEDED
            else RuntimeStatus.FAILED,
        )
        if (
            self.terminal_status is TerminalStatus.SUCCEEDED
            and self.validation_result is not None
            and self.validation_result.status is not ValidationStatus.PASS
        ):
            raise ValueError("Validation failure or UNKNOWN cannot produce success.")
        if self.terminal_status is TerminalStatus.SUCCEEDED and self.changed_files:
            if (
                self.validation_result is None
                or self.validation_result.status is not ValidationStatus.PASS
                or self.diff is None
            ):
                raise ValueError("A changed-code success requires PASS validation and exact diff.")


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
    failure_category: str | None = None

    def __post_init__(self) -> None:
        RuntimeEvent.__post_init__(self)
        if (
            self.failure_category is not None
            and self.failure_category not in SAFE_FAILURE_CATEGORIES
        ):
            raise ValueError("Unknown safe failure category.")


@dataclass(frozen=True, slots=True, kw_only=True)
class ApprovalRequested(RuntimeEvent):
    """A Tool or Plan is waiting for its selected approval policy."""

    status: RuntimeStatus = field(default=RuntimeStatus.AWAITING_APPROVAL, init=False)
    approval_id: str
    invocation_id: str | None
    operation: str
    risk_level: RiskLevel
    resource_or_command_summary: str
    subject: ApprovalSubject = ApprovalSubject.TOOL
    plan_id: str | None = None
    plan_version: int | None = None

    def __post_init__(self) -> None:
        RuntimeEvent.__post_init__(self)
        if self.subject is ApprovalSubject.TOOL:
            if (
                self.invocation_id is None
                or self.plan_id is not None
                or self.plan_version is not None
            ):
                raise ValueError("A Tool approval has invalid correlation fields.")
        elif (
            self.invocation_id is not None
            or self.plan_id is None
            or self.plan_version is None
            or self.operation != "approve_plan"
        ):
            raise ValueError("A Plan approval has invalid correlation fields.")


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


@dataclass(frozen=True, slots=True, kw_only=True)
class RepositoryExplored(RuntimeEvent):
    status: RuntimeStatus = field(default=RuntimeStatus.STARTED, init=False)
    instruction_paths: tuple[str, ...]
    manifest_paths: tuple[str, ...]
    relevant_paths: tuple[str, ...]
    exploration_tool_calls: int
    truncated: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class ContextBuilt(RuntimeEvent):
    status: RuntimeStatus = field(default=RuntimeStatus.STARTED, init=False)
    selected_paths: tuple[str, ...]
    retained_characters: int
    truncated: bool
    selected_chunk_count: int = 0
    semantic_retrieval_used: bool = False
    semantic_retrieval_status: str | None = None
    selected_skill_ids: tuple[str, ...] = ()
    skill_selection_reason_summary: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class PlanCreated(RuntimeEvent):
    status: RuntimeStatus = field(default=RuntimeStatus.STARTED, init=False)
    plan_id: str
    plan_version: int
    plan_kind: PlanKind
    step_summaries: tuple[str, ...]
    replan_reason: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class ReplanOccurred(RuntimeEvent):
    status: RuntimeStatus = field(default=RuntimeStatus.STARTED, init=False)
    plan_id: str
    previous_version: int
    new_version: int
    reason: str
    replan_count: int


@dataclass(frozen=True, slots=True, kw_only=True)
class ValidationStarted(RuntimeEvent):
    status: RuntimeStatus = field(default=RuntimeStatus.STARTED, init=False)
    check_ids: tuple[str, ...]
    check_kinds: tuple[ValidationCheckKind, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class ValidationFinished(RuntimeEvent):
    status: RuntimeStatus = field(default=RuntimeStatus.STARTED, init=False)
    validation_status: ValidationStatus
    confidence: ValidationConfidence
    executed_check_count: int
    repair_count: int
    duration_ms: int | None = None

    def __post_init__(self) -> None:
        RuntimeEvent.__post_init__(self)
        if self.duration_ms is not None and self.duration_ms < 0:
            raise ValueError("ValidationFinished duration_ms must not be negative.")


@dataclass(frozen=True, slots=True, kw_only=True)
class RepairStarted(RuntimeEvent):
    status: RuntimeStatus = field(default=RuntimeStatus.STARTED, init=False)
    plan_id: str
    plan_version: int
    repair_count: int
    max_repair_attempts: int
    failure_summary: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelCallStarted(RuntimeEvent):
    status: RuntimeStatus = field(default=RuntimeStatus.STARTED, init=False)
    model_call_id: str
    phase: ModelCallPhase
    provider: str | None
    model: str | None

    def __post_init__(self) -> None:
        RuntimeEvent.__post_init__(self)
        _validate_uuid(self.model_call_id, "model_call_id")


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelCallFinished(RuntimeEvent):
    status: RuntimeStatus = field(default=RuntimeStatus.STARTED, init=False)
    model_call_id: str
    phase: ModelCallPhase
    success: bool
    duration_ms: int
    usage: TokenUsage
    error_code: str | None

    def __post_init__(self) -> None:
        RuntimeEvent.__post_init__(self)
        _validate_uuid(self.model_call_id, "model_call_id")
        if self.duration_ms < 0:
            raise ValueError("ModelCallFinished duration_ms must not be negative.")
        if self.success and self.error_code is not None:
            raise ValueError("Successful model calls cannot carry an error code.")
        if not self.success and not self.error_code:
            raise ValueError("Failed model calls require a safe error code.")


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentStepCompleted(RuntimeEvent):
    status: RuntimeStatus = field(default=RuntimeStatus.STARTED, init=False)
    step_count: int
    decision_kind: AgentDecisionKind
    model_call_id: str

    def __post_init__(self) -> None:
        RuntimeEvent.__post_init__(self)
        if self.step_count < 1:
            raise ValueError("AgentStepCompleted step_count must be positive.")
        _validate_uuid(self.model_call_id, "model_call_id")


@dataclass(frozen=True, slots=True, kw_only=True)
class ApprovalResolved(RuntimeEvent):
    status: RuntimeStatus = field(default=RuntimeStatus.STARTED, init=False)
    approval_id: str
    subject: ApprovalSubject
    invocation_id: str | None
    plan_id: str | None
    plan_version: int | None
    decision: ApprovalDecision
    actor_category: ApprovalActorCategory

    def __post_init__(self) -> None:
        RuntimeEvent.__post_init__(self)
        _validate_uuid(self.approval_id, "approval_id")
        if self.decision is ApprovalDecision.PENDING:
            raise ValueError("ApprovalResolved requires a terminal decision.")
        if self.subject is ApprovalSubject.TOOL:
            if (
                self.invocation_id is None
                or self.plan_id is not None
                or self.plan_version is not None
            ):
                raise ValueError("Resolved Tool approval correlation is invalid.")
            _validate_uuid(self.invocation_id, "invocation_id")
        else:
            if self.invocation_id is not None or self.plan_id is None or self.plan_version is None:
                raise ValueError("Resolved Plan approval correlation is invalid.")
            _validate_uuid(self.plan_id, "plan_id")
            if self.plan_version < 1:
                raise ValueError("Resolved Plan version must be positive.")


@dataclass(frozen=True, slots=True, kw_only=True)
class ChangedFileRecorded(RuntimeEvent):
    status: RuntimeStatus = field(default=RuntimeStatus.STARTED, init=False)
    invocation_id: str
    relative_path: str
    change_kind: ChangeKind
    changed_file_count: int

    def __post_init__(self) -> None:
        RuntimeEvent.__post_init__(self)
        _validate_uuid(self.invocation_id, "invocation_id")
        parts = self.relative_path.replace("\\", "/").split("/")
        if (
            not self.relative_path
            or self.relative_path.startswith(("/", "\\"))
            or ":" in self.relative_path
            or any(part in {"", ".", ".."} for part in parts)
            or len(self.relative_path) > 512
        ):
            raise ValueError("Changed-file path must be normalized and workspace-relative.")
        if self.changed_file_count < 1:
            raise ValueError("changed_file_count must be positive.")


@dataclass(frozen=True, slots=True, kw_only=True)
class ObservabilityWarning(RuntimeEvent):
    status: RuntimeStatus = field(default=RuntimeStatus.STARTED, init=False)
    code: str
    sink: TraceSink
    operation: TraceOperation
    fallback: TraceFallback

    def __post_init__(self) -> None:
        RuntimeEvent.__post_init__(self)
        if self.code not in {
            "TRACE_SINK_FAILED",
            "TRACE_FLUSH_FAILED",
            "TELEMETRY_EVENT_INVALID",
            "TRACE_REDACTION_FAILED",
            "TRACE_SINK_OVERFLOW",
        }:
            raise ValueError("Observability warning code is not approved.")


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


def _validate_uuid(value: str, field_name: str) -> None:
    from uuid import UUID

    try:
        parsed = UUID(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a UUID string.") from exc
    if str(parsed) != value:
        raise ValueError(f"{field_name} must use canonical UUID text.")
