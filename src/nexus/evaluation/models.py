"""Frozen Day 9 evaluation contracts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Protocol

from nexus.domain.model import TokenUsage
from nexus.domain.observability import TraceRunFinish
from nexus.domain.planning import TerminalStatus
from nexus.domain.runtime_events import RuntimeStatus
from nexus.domain.tooling import JsonObject


@dataclass(frozen=True, slots=True)
class RequiredFact:
    fact_id: str
    description: str
    required_terms: tuple[str, ...]


class EvalAssertionType(StrEnum):
    VALIDATION_PASSES = "validation_passes"
    NO_UNAUTHORIZED_CHANGES = "no_unauthorized_changes"
    NO_FORBIDDEN_CHANGES = "no_forbidden_changes"
    NO_REPOSITORY_CHANGES = "no_repository_changes"
    REQUIRED_PATH_CHANGED = "required_path_changed"
    REQUIRED_PATH_CREATED = "required_path_created"
    REQUIRED_TEXT_IN_FILE = "required_text_in_file"
    REQUIRED_FINAL_FACTS = "required_final_facts"
    REQUIRED_SECURITY_EVIDENCE = "required_security_evidence"
    TARGETED_TEST_ADDED = "targeted_test_added"


@dataclass(frozen=True, slots=True)
class EvalAssertion:
    assertion_type: EvalAssertionType
    path: str | None = None
    expected_text: str | None = None
    required_facts: tuple[RequiredFact, ...] = ()
    security_codes: tuple[str, ...] = ()
    expected_test_node_ids: tuple[str, ...] = ()
    metadata: JsonObject | None = None


@dataclass(frozen=True, slots=True)
class EvalCase:
    case_id: str
    initial_repo_state: str
    task: str
    allowed_files: tuple[str, ...]
    forbidden_files: tuple[str, ...]
    pre_validation_command: tuple[str, ...]
    validation_command: tuple[str, ...]
    validation_timeout_seconds: int | None
    test_discovery_command: tuple[str, ...]
    expected_behavior: str
    deterministic_success_conditions: tuple[EvalAssertion, ...]
    max_reasonable_steps: int
    metadata: JsonObject | None = None


@dataclass(frozen=True, slots=True)
class FileFingerprint:
    path: str
    kind: str
    content_sha256: str | None
    executable: bool
    symlink_target: str | None


@dataclass(frozen=True, slots=True)
class ForbiddenPathSnapshot:
    path: str
    exists: bool
    kind: str | None
    content_sha256: str | None
    executable: bool | None
    symlink_target: str | None


@dataclass(frozen=True, slots=True)
class RepositoryState:
    files: Mapping[str, FileFingerprint]
    forbidden_snapshots: Mapping[str, ForbiddenPathSnapshot]

    def __post_init__(self) -> None:
        object.__setattr__(self, "files", MappingProxyType(dict(self.files)))
        object.__setattr__(
            self,
            "forbidden_snapshots",
            MappingProxyType(dict(self.forbidden_snapshots)),
        )


@dataclass(frozen=True, slots=True)
class EvalForbiddenPathEvidence:
    path: str
    before: ForbiddenPathSnapshot
    after: ForbiddenPathSnapshot
    changed: bool


@dataclass(frozen=True, slots=True)
class EvalRepositoryEvidence:
    changed_files: tuple[str, ...]
    created_files: tuple[str, ...]
    deleted_files: tuple[str, ...]
    forbidden_paths: tuple[EvalForbiddenPathEvidence, ...]
    final_text_files: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "changed_files", tuple(self.changed_files))
        object.__setattr__(self, "created_files", tuple(self.created_files))
        object.__setattr__(self, "deleted_files", tuple(self.deleted_files))
        object.__setattr__(self, "forbidden_paths", tuple(self.forbidden_paths))
        object.__setattr__(self, "final_text_files", MappingProxyType(dict(self.final_text_files)))


class EvalHarnessOperation(StrEnum):
    GIT_INIT = "GIT_INIT"
    GIT_ADD_BASELINE = "GIT_ADD_BASELINE"
    GIT_COMMIT_BASELINE = "GIT_COMMIT_BASELINE"
    GIT_STATUS = "GIT_STATUS"
    GIT_DIFF = "GIT_DIFF"
    GIT_LS_FILES = "GIT_LS_FILES"
    VALIDATION = "VALIDATION"
    TEST_DISCOVERY = "TEST_DISCOVERY"


@dataclass(frozen=True, slots=True)
class EvalHarnessCommand:
    operation: EvalHarnessOperation
    argv: tuple[str, ...]
    cwd: Path
    timeout_seconds: int


@dataclass(frozen=True, slots=True)
class EvalHarnessCommandResult:
    operation: EvalHarnessOperation
    allowed: bool
    denial_code: str | None
    exit_code: int | None
    stdout_summary: str
    stderr_summary: str
    duration_ms: int
    timed_out: bool
    infrastructure_error: str | None


class SecurityEvidenceSource(StrEnum):
    COMMAND_POLICY = "COMMAND_POLICY"
    APPROVAL_POLICY = "APPROVAL_POLICY"
    TOOL_RUNTIME = "TOOL_RUNTIME"
    WORKSPACE_GUARD = "WORKSPACE_GUARD"
    MCP_POLICY = "MCP_POLICY"
    RUNTIME_EVENT = "RUNTIME_EVENT"


class SecurityDecision(StrEnum):
    DENIED = "DENIED"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True, slots=True)
class SecurityEvidence:
    source: SecurityEvidenceSource
    operation: str
    decision: SecurityDecision
    error_code: str
    resource_summary: str | None


class EvalTraceCollector(Protocol):
    def record_finish(self, finish: TraceRunFinish) -> None: ...

    def get_finish(self, run_id: str) -> TraceRunFinish | None: ...


@dataclass(frozen=True, slots=True)
class EvalMetrics:
    agent_steps: int
    llm_calls: int
    tool_calls: int
    replans: int
    repairs: int
    latency_ms: int
    token_usage: TokenUsage


class EvalOutcome(StrEnum):
    PASS = "PASS"
    TASK_FAILED = "TASK_FAILED"
    INFRASTRUCTURE_ERROR = "INFRASTRUCTURE_ERROR"
    EVALUATOR_ERROR = "EVALUATOR_ERROR"


class EvalValidationStatus(StrEnum):
    NOT_RUN = "NOT_RUN"
    PASSED = "PASSED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    INFRASTRUCTURE_ERROR = "INFRASTRUCTURE_ERROR"


class EvalPytestNodeOutcome(StrEnum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    XFAILED = "XFAILED"
    XPASSED = "XPASSED"
    ERROR = "ERROR"
    NOT_RUN = "NOT_RUN"


@dataclass(frozen=True, slots=True)
class EvalPytestNodeResult:
    node_id: str
    outcome: EvalPytestNodeOutcome


@dataclass(frozen=True, slots=True)
class EvalValidationResult:
    status: EvalValidationStatus
    command: tuple[str, ...]
    exit_code: int | None
    stdout_summary: str
    stderr_summary: str
    duration_ms: int
    timeout_seconds: int | None
    infrastructure_error: str | None
    pytest_node_results: tuple[EvalPytestNodeResult, ...] = ()


class EvalDiscoveryStatus(StrEnum):
    NOT_RUN = "NOT_RUN"
    PASSED = "PASSED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    INFRASTRUCTURE_ERROR = "INFRASTRUCTURE_ERROR"


@dataclass(frozen=True, slots=True)
class EvalDiscoveryResult:
    status: EvalDiscoveryStatus
    command: tuple[str, ...]
    exit_code: int | None
    collected_node_ids: tuple[str, ...]
    stdout_summary: str
    stderr_summary: str
    duration_ms: int
    timeout_seconds: int | None
    infrastructure_error: str | None


@dataclass(frozen=True, slots=True)
class EvalCheckEvidence:
    pre_validation: EvalValidationResult
    post_validation: EvalValidationResult
    pre_discovery: EvalDiscoveryResult
    post_discovery: EvalDiscoveryResult


@dataclass(frozen=True, slots=True)
class EvalExecutionResult:
    case_id: str
    run_id: str | None
    session_id: str | None
    runtime_status: RuntimeStatus | None
    terminal_status: TerminalStatus | None
    error_code: str | None
    final_result_summary: str | None
    repository: EvalRepositoryEvidence
    checks: EvalCheckEvidence
    metrics: EvalMetrics | None
    security_evidence: tuple[SecurityEvidence, ...]
    infrastructure_error: str | None
    failure_category: str | None = None


@dataclass(frozen=True, slots=True)
class EvalAssertionResult:
    assertion_type: EvalAssertionType
    passed: bool
    evidence: tuple[str, ...]
    failure_reason: str | None


@dataclass(frozen=True, slots=True)
class DeterministicEvaluationResult:
    outcome: EvalOutcome
    runtime_completed: bool
    validation_passed: bool | None
    constraints_passed: bool
    forbidden_change_detected: bool
    unauthorized_change_detected: bool
    assertion_results: tuple[EvalAssertionResult, ...]
    failure_reason: str | None


class DeterministicEvaluator(Protocol):
    async def evaluate(
        self, case: EvalCase, execution: EvalExecutionResult
    ) -> DeterministicEvaluationResult: ...


@dataclass(frozen=True, slots=True)
class EvalCaseLoadError:
    source_path: str
    case_id_hint: str | None
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class EvalSuiteLoadResult:
    cases: tuple[EvalCase, ...]
    errors: tuple[EvalCaseLoadError, ...]


@dataclass(frozen=True, slots=True)
class EvalCaseReport:
    case_id: str
    outcome: EvalOutcome
    run_id: str | None
    session_id: str | None
    runtime_status: RuntimeStatus | None
    terminal_status: TerminalStatus | None
    error_code: str | None
    deterministic_result: DeterministicEvaluationResult
    metrics: EvalMetrics | None
    repository: EvalRepositoryEvidence
    checks: EvalCheckEvidence
    security_evidence: tuple[SecurityEvidence, ...]
    efficiency_warning: bool
    evidence: tuple[str, ...]
    failure_category: str | None = None


@dataclass(frozen=True, slots=True)
class EvalSuiteError:
    code: str
    message: str
    source_path: str | None
    case_id_hint: str | None


@dataclass(frozen=True, slots=True)
class EvalSuiteReport:
    schema_version: str
    generated_at: datetime
    cases: tuple[EvalCaseReport, ...]
    suite_errors: tuple[EvalSuiteError, ...]
    total_cases: int
    passed_cases: int
    task_failed_cases: int
    infrastructure_error_cases: int
    evaluator_error_cases: int
