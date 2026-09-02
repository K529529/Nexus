"""Deterministic Day 4 validation selection and governed execution."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from uuid import uuid4

from nexus.application.tool_runtime import ToolRuntime
from nexus.domain.exploration import ExplorationResult, WorkingContext
from nexus.domain.planning import ApprovedPlanEvidence, ChangedFile, Plan
from nexus.domain.runtime_events import (
    RuntimeEvent,
    ValidationFinished,
    ValidationStarted,
)
from nexus.domain.tooling import ToolInvocation, ToolResult
from nexus.domain.validation import (
    ValidationCheck,
    ValidationCheckKind,
    ValidationCheckResult,
    ValidationConfidence,
    ValidationPlan,
    ValidationResult,
    ValidationStatus,
    aggregate_repairable,
)
from nexus.errors import ValidationError

RuntimeEventEmitter = Callable[[RuntimeEvent], Awaitable[None]]

_PREFIX_KINDS = {
    "TEST": ValidationCheckKind.TEST,
    "BUILD": ValidationCheckKind.BUILD,
    "LINT": ValidationCheckKind.LINT,
    "TYPE_CHECK": ValidationCheckKind.TYPE_CHECK,
    "REPOSITORY_COMMAND": ValidationCheckKind.REPOSITORY_COMMAND,
    "GENERATED_TARGETED_TEST": ValidationCheckKind.GENERATED_TARGETED_TEST,
    "BASIC_EXECUTION": ValidationCheckKind.BASIC_EXECUTION,
}
_PRIORITY = {kind: index for index, kind in enumerate(_PREFIX_KINDS.values())}
_CONCLUSIVE_CODE_KINDS = {
    ValidationCheckKind.TEST,
    ValidationCheckKind.BUILD,
    ValidationCheckKind.GENERATED_TARGETED_TEST,
    ValidationCheckKind.BASIC_EXECUTION,
}


class DeterministicValidationPlanner:
    async def plan(
        self,
        *,
        task: str,
        plan: Plan,
        exploration: ExplorationResult,
        context: WorkingContext,
        changed_files: tuple[ChangedFile, ...],
    ) -> ValidationPlan:
        del task, exploration, context
        candidates: list[tuple[int, int, ValidationCheckKind, tuple[str, ...], str]] = []
        for step in plan.steps:
            if step.command_argv is None or step.command_cwd is None:
                continue
            kind = _kind_from_description(step.description)
            candidates.append(
                (_PRIORITY[kind], step.sequence, kind, step.command_argv, step.command_cwd)
            )
        candidates.sort(key=lambda item: (item[0], item[1]))

        checks: list[ValidationCheck] = []
        for _, _, kind, argv, cwd in candidates:
            checks.append(
                ValidationCheck(
                    str(uuid4()),
                    len(checks) + 1,
                    kind,
                    "shell",
                    {"argv": list(argv), "cwd": cwd},
                    "Explicit validation command in the approved Plan.",
                    True,
                )
            )
        if changed_files:
            checks.append(
                ValidationCheck(
                    str(uuid4()),
                    len(checks) + 1,
                    ValidationCheckKind.DIFF_INSPECTION,
                    "git_diff",
                    {"staged": False},
                    "Every changed-file run requires exact diff inspection.",
                    True,
                )
            )
        return ValidationPlan(tuple(checks))


class ToolValidationRunner:
    def __init__(
        self,
        tool_runtime: ToolRuntime,
        *,
        emit: RuntimeEventEmitter | None = None,
    ) -> None:
        self._tool_runtime = tool_runtime
        self._emit = emit or _ignore_event

    async def run(
        self,
        plan: ValidationPlan,
        *,
        run_id: str,
        session_id: str,
        authorization: ApprovedPlanEvidence,
        repair_count: int,
    ) -> ValidationResult:
        _validate_plan(plan, authorization)
        await self._emit(
            ValidationStarted(
                run_id=run_id,
                session_id=session_id,
                check_ids=tuple(check.check_id for check in plan.checks),
                check_kinds=tuple(check.kind for check in plan.checks),
            )
        )
        results: list[ValidationCheckResult] = []
        for check in plan.checks:
            invocation = ToolInvocation(
                check.check_id,
                check.tool_name,
                check.arguments,
                run_id,
                session_id,
            )
            result = await self._tool_runtime.execute(
                invocation,
                authorization=authorization if check.tool_name == "shell" else None,
            )
            results.append(_check_result(check, result))

        status = _aggregate_status(
            tuple(results),
            changed=any(
                check.kind is ValidationCheckKind.DIFF_INSPECTION
                for check in plan.checks
            ),
        )
        confidence = _confidence(status, tuple(results))
        repairable = aggregate_repairable(status, tuple(results))
        validation = ValidationResult(
            plan.checks,
            tuple(results),
            status,
            confidence,
            repairable,
            repair_count,
            _summary(status, tuple(results)),
        )
        await self._emit(
            ValidationFinished(
                run_id=run_id,
                session_id=session_id,
                validation_status=status,
                confidence=confidence,
                executed_check_count=len(results),
                repair_count=repair_count,
            )
        )
        return validation


def _kind_from_description(description: str) -> ValidationCheckKind:
    prefix, separator, _ = description.partition(":")
    if separator:
        kind = _PREFIX_KINDS.get(prefix.strip().upper())
        if kind is not None:
            return kind
    return ValidationCheckKind.REPOSITORY_COMMAND


def _check_result(check: ValidationCheck, result: ToolResult) -> ValidationCheckResult:
    truncated = bool(
        result.output
        and (
            result.output.get("truncated") is True
            or result.output.get("output_truncated") is True
        )
    )
    if result.success and not truncated:
        status = ValidationStatus.PASS
        summary = f"{check.kind.value} completed successfully."
    elif result.error is not None and result.error.code == "COMMAND_EXIT_NONZERO":
        status = ValidationStatus.FAIL
        summary = f"{check.kind.value} completed with a non-zero exit."
    else:
        status = ValidationStatus.UNKNOWN
        code = "NO_EVIDENCE" if result.error is None else result.error.code
        summary = f"{check.kind.value} is inconclusive ({code})."
    return ValidationCheckResult(check, status, result, summary)


def _aggregate_status(
    results: tuple[ValidationCheckResult, ...],
    *,
    changed: bool,
) -> ValidationStatus:
    required = tuple(result for result in results if result.check.required)
    if any(result.status is ValidationStatus.FAIL for result in required):
        return ValidationStatus.FAIL
    if not required or any(result.status is ValidationStatus.UNKNOWN for result in required):
        return ValidationStatus.UNKNOWN
    has_diff = any(
        result.check.kind is ValidationCheckKind.DIFF_INSPECTION
        and result.status is ValidationStatus.PASS
        for result in required
    )
    has_code_check = any(
        result.check.kind in _CONCLUSIVE_CODE_KINDS
        and result.status is ValidationStatus.PASS
        for result in required
    )
    if changed and (not has_diff or not has_code_check):
        return ValidationStatus.UNKNOWN
    return ValidationStatus.PASS


def _confidence(
    status: ValidationStatus,
    results: tuple[ValidationCheckResult, ...],
) -> ValidationConfidence:
    if status is ValidationStatus.UNKNOWN:
        return ValidationConfidence.LOW
    behavioral_count = sum(
        result.check.kind in _CONCLUSIVE_CODE_KINDS
        and result.status is ValidationStatus.PASS
        for result in results
    )
    if status is ValidationStatus.PASS and behavioral_count > 1:
        return ValidationConfidence.HIGH
    return ValidationConfidence.MEDIUM


def _summary(
    status: ValidationStatus,
    results: tuple[ValidationCheckResult, ...],
) -> str:
    counts = {
        value: sum(result.status is value for result in results)
        for value in ValidationStatus
    }
    return (
        f"Validation {status.value}: {counts[ValidationStatus.PASS]} passed, "
        f"{counts[ValidationStatus.FAIL]} failed, "
        f"{counts[ValidationStatus.UNKNOWN]} unknown."
    )


async def _ignore_event(event: RuntimeEvent) -> None:
    del event


def _validate_plan(
    plan: ValidationPlan,
    authorization: ApprovedPlanEvidence,
) -> None:
    for check in plan.checks:
        if check.kind is ValidationCheckKind.DIFF_INSPECTION:
            valid = check.tool_name == "git_diff" and check.arguments == {
                "staged": False
            }
        else:
            argv = check.arguments.get("argv")
            cwd = check.arguments.get("cwd")
            valid = (
                check.tool_name == "shell"
                and isinstance(argv, list)
                and all(isinstance(item, str) for item in argv)
                and isinstance(cwd, str)
                and (tuple(argv), cwd)
                in authorization.authorization_scope.allowed_commands
            )
        if not valid:
            raise ValidationError(
                "A selected check violates the approved validation contract.",
                code="VALIDATION_PLAN_INVALID",
            )
