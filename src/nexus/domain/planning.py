"""Nexus-owned Day 4 planning, authorization, change, and terminal values."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import PurePosixPath
from uuid import UUID

from nexus.domain.tooling import ApprovalDecision


class PlanKind(StrEnum):
    INITIAL = "INITIAL"
    REPLAN = "REPLAN"


class PlanStatus(StrEnum):
    CREATED = "CREATED"
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class PlanStepStatus(StrEnum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"


class PlanAuthorizationSource(StrEnum):
    INTERACTIVE = "INTERACTIVE"
    AUTO_MODE = "AUTO_MODE"


class ChangeKind(StrEnum):
    MODIFIED = "MODIFIED"
    ADDED = "ADDED"


class TerminalStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    FAILED_APPROVAL_DENIED = "FAILED_APPROVAL_DENIED"
    FAILED_VALIDATION = "FAILED_VALIDATION"
    FAILED_VALIDATION_UNKNOWN = "FAILED_VALIDATION_UNKNOWN"
    STOPPED_MAX_STEPS = "STOPPED_MAX_STEPS"
    STOPPED_MAX_REPAIRS = "STOPPED_MAX_REPAIRS"
    STOPPED_MAX_REPLANS = "STOPPED_MAX_REPLANS"


@dataclass(frozen=True, slots=True)
class PlanStep:
    step_id: str
    sequence: int
    description: str
    tool_name: str | None
    target_paths: tuple[str, ...]
    command_argv: tuple[str, ...] | None
    command_cwd: str | None
    status: PlanStepStatus

    def __post_init__(self) -> None:
        _validate_uuid(self.step_id, "step_id")
        if self.sequence < 1 or not self.description.strip():
            raise ValueError("PlanStep sequence and description are invalid.")
        paths = tuple(self.target_paths)
        if paths != tuple(sorted(set(paths))):
            raise ValueError("PlanStep target_paths must be sorted and unique.")
        for path in paths:
            _validate_repository_path(path)
        object.__setattr__(self, "target_paths", paths)
        argv = None if self.command_argv is None else tuple(self.command_argv)
        if argv is not None and (not argv or any(not item for item in argv)):
            raise ValueError("PlanStep command_argv must contain non-empty strings.")
        object.__setattr__(self, "command_argv", argv)
        if argv is not None:
            if self.tool_name != "shell" or self.command_cwd is None:
                raise ValueError("A command PlanStep must be a shell step with a cwd.")
            _validate_repository_path(self.command_cwd, allow_root=True)
        elif self.command_cwd is not None:
            raise ValueError("command_cwd requires command_argv.")
        if self.tool_name in {"edit_file", "apply_patch", "write_file"} and len(paths) != 1:
            raise ValueError("An editing PlanStep must target exactly one path.")
        if self.tool_name is None and (paths or argv is not None):
            raise ValueError("A narrative PlanStep cannot carry Tool authority.")
        if self.tool_name not in {None, "edit_file", "apply_patch", "write_file", "shell"} and (
            paths or argv is not None
        ):
            raise ValueError("Only frozen Day 4 Tool actions may carry Plan authority.")


@dataclass(frozen=True, slots=True)
class AuthorizationScope:
    allowed_write_actions: tuple[tuple[str, str], ...]
    allowed_commands: tuple[tuple[tuple[str, ...], str], ...]

    def __post_init__(self) -> None:
        writes = tuple((tool, path) for tool, path in self.allowed_write_actions)
        commands = tuple((tuple(argv), cwd) for argv, cwd in self.allowed_commands)
        if writes != tuple(sorted(set(writes))):
            raise ValueError("allowed_write_actions must be sorted and unique.")
        if commands != tuple(sorted(set(commands))):
            raise ValueError("allowed_commands must be sorted and unique.")
        for tool, path in writes:
            if tool not in {"edit_file", "apply_patch", "write_file"}:
                raise ValueError("AuthorizationScope contains an invalid WRITE Tool.")
            _validate_repository_path(path)
        for argv, cwd in commands:
            if not argv or any(not item for item in argv):
                raise ValueError("AuthorizationScope command argv is invalid.")
            _validate_repository_path(cwd, allow_root=True)
        object.__setattr__(self, "allowed_write_actions", writes)
        object.__setattr__(self, "allowed_commands", commands)


@dataclass(frozen=True, slots=True)
class Plan:
    plan_id: str
    run_id: str
    session_id: str
    version: int
    kind: PlanKind
    status: PlanStatus
    approval_status: ApprovalDecision
    steps: tuple[PlanStep, ...]
    authorization_scope: AuthorizationScope
    rationale_summary: str
    replan_reason: str | None
    approval_id: str | None
    scope_digest: str
    created_at: datetime
    approved_at: datetime | None

    def __post_init__(self) -> None:
        for field_name, value in (
            ("plan_id", self.plan_id),
            ("run_id", self.run_id),
            ("session_id", self.session_id),
        ):
            _validate_uuid(value, field_name)
        _validate_timestamp(self.created_at, "created_at")
        if self.approved_at is not None:
            _validate_timestamp(self.approved_at, "approved_at")
        if self.version < 1 or not self.rationale_summary.strip():
            raise ValueError("Plan version and rationale_summary are invalid.")
        steps = tuple(self.steps)
        if not steps:
            raise ValueError("Plan must contain at least one step.")
        if [step.sequence for step in steps] != list(range(1, len(steps) + 1)):
            raise ValueError("Plan step sequence must be contiguous from one.")
        if len({step.step_id for step in steps}) != len(steps):
            raise ValueError("Plan step IDs must be unique.")
        object.__setattr__(self, "steps", steps)
        if self.authorization_scope != derive_authorization_scope(steps):
            raise ValueError("Plan AuthorizationScope must exactly match Plan.steps.")
        expected_digest = compute_scope_digest(
            self.plan_id,
            self.version,
            self.authorization_scope,
        )
        if self.scope_digest != expected_digest:
            raise ValueError("Plan scope_digest does not match its authorization scope.")
        if self.kind is PlanKind.INITIAL:
            if self.version != 1 or self.replan_reason is not None:
                raise ValueError("An INITIAL Plan must be version one without a reason.")
        elif self.version <= 1 or not (self.replan_reason or "").strip():
            raise ValueError("A REPLAN must increment version and contain a reason.")
        if self.status is PlanStatus.CREATED:
            if (
                self.approval_status is not ApprovalDecision.PENDING
                or self.approval_id is not None
                or self.approved_at is not None
            ):
                raise ValueError("A CREATED Plan must await approval.")
        if self.status is PlanStatus.ACTIVE and (
            self.approval_status is not ApprovalDecision.APPROVED
            or self.approval_id is None
            or self.approved_at is None
        ):
            raise ValueError("An ACTIVE Plan requires approved evidence identity.")
        if (
            self.approval_status is ApprovalDecision.DENIED
            and self.status is not PlanStatus.FAILED
        ):
            raise ValueError("A denied Plan must be FAILED.")


@dataclass(frozen=True, slots=True)
class ApprovedPlanEvidence:
    plan_id: str
    plan_version: int
    run_id: str
    session_id: str
    source: PlanAuthorizationSource
    approval_id: str
    scope_digest: str
    authorization_scope: AuthorizationScope
    approved_at: datetime

    def __post_init__(self) -> None:
        for field_name, value in (
            ("plan_id", self.plan_id),
            ("run_id", self.run_id),
            ("session_id", self.session_id),
            ("approval_id", self.approval_id),
        ):
            _validate_uuid(value, field_name)
        if self.plan_version < 1:
            raise ValueError("Approved Plan version must be positive.")
        _validate_timestamp(self.approved_at, "approved_at")
        if self.scope_digest != compute_scope_digest(
            self.plan_id,
            self.plan_version,
            self.authorization_scope,
        ):
            raise ValueError("Approved Plan evidence has an invalid scope digest.")


@dataclass(frozen=True, slots=True)
class RepairGuidance:
    plan_id: str
    plan_version: int
    repair_attempt: int
    steps: tuple[PlanStep, ...]
    failure_summary: str

    def __post_init__(self) -> None:
        _validate_uuid(self.plan_id, "plan_id")
        if self.plan_version < 1 or self.repair_attempt < 1:
            raise ValueError("Repair identity values must be positive.")
        if not self.failure_summary.strip():
            raise ValueError("Repair failure_summary must not be empty.")
        object.__setattr__(self, "steps", tuple(self.steps))


@dataclass(frozen=True, slots=True)
class ChangedFile:
    path: str
    change_kind: ChangeKind
    first_invocation_id: str
    latest_invocation_id: str

    def __post_init__(self) -> None:
        _validate_repository_path(self.path)
        _validate_uuid(self.first_invocation_id, "first_invocation_id")
        _validate_uuid(self.latest_invocation_id, "latest_invocation_id")


@dataclass(frozen=True, slots=True)
class PlanApprovalResumeInput:
    decision: ApprovalDecision
    reason: str | None

    def __post_init__(self) -> None:
        if self.decision not in {ApprovalDecision.APPROVED, ApprovalDecision.DENIED}:
            raise ValueError("Plan approval resume input must be terminal.")


def derive_authorization_scope(steps: tuple[PlanStep, ...]) -> AuthorizationScope:
    writes = {
        (step.tool_name, step.target_paths[0])
        for step in steps
        if step.tool_name in {"edit_file", "apply_patch", "write_file"}
    }
    commands = {
        (step.command_argv, step.command_cwd)
        for step in steps
        if step.tool_name == "shell"
        and step.command_argv is not None
        and step.command_cwd is not None
    }
    return AuthorizationScope(
        tuple(sorted((str(tool), path) for tool, path in writes)),
        tuple(sorted((tuple(argv), str(cwd)) for argv, cwd in commands)),
    )


def compute_scope_digest(
    plan_id: str,
    plan_version: int,
    scope: AuthorizationScope,
) -> str:
    payload = {
        "authorization_scope": {
            "allowed_commands": [
                [list(argv), cwd] for argv, cwd in scope.allowed_commands
            ],
            "allowed_write_actions": [list(action) for action in scope.allowed_write_actions],
        },
        "plan_id": plan_id,
        "plan_version": plan_version,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def approval_summary(plan_id: str, version: int, digest: str) -> str:
    return f"approve_plan:{plan_id}:v{version}:scope-sha256:{digest}"


def _validate_repository_path(value: str, *, allow_root: bool = False) -> None:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("Repository paths must be non-empty POSIX-style strings.")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or (value == "." and not allow_root):
        raise ValueError("Repository path is outside the allowed relative form.")
    if value != "." and (value.startswith("./") or "/./" in value):
        raise ValueError("Repository paths must be normalized.")


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
