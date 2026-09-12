"""Sequential, isolated Day 9 evaluation orchestration."""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from pathlib import Path

from nexus.application.runtime import NexusRuntime
from nexus.domain.runtime_events import (
    ErrorOccurred,
    FinalResult,
    RunInterrupted,
    RuntimeStatus,
    ToolFinished,
)
from nexus.domain.tooling import ApprovalDecision, PolicyDecision
from nexus.evaluation.evaluator import DefaultDeterministicEvaluator
from nexus.evaluation.fixtures import FixtureConstraintError, validate_fixture_source
from nexus.evaluation.harness import (
    DEFAULT_EVAL_VALIDATION_TIMEOUT_SECONDS,
    EvaluationHarness,
    compare_repository_states,
    snapshot_repository,
)
from nexus.evaluation.metrics import InMemoryEvalTraceCollector, metrics_from_finish
from nexus.evaluation.models import (
    DeterministicEvaluationResult,
    EvalAssertionResult,
    EvalAssertionType,
    EvalCase,
    EvalCaseReport,
    EvalCheckEvidence,
    EvalDiscoveryResult,
    EvalDiscoveryStatus,
    EvalExecutionResult,
    EvalForbiddenPathEvidence,
    EvalOutcome,
    EvalRepositoryEvidence,
    EvalSuiteError,
    EvalSuiteLoadResult,
    EvalSuiteReport,
    EvalValidationResult,
    EvalValidationStatus,
    ForbiddenPathSnapshot,
    SecurityDecision,
    SecurityEvidence,
    SecurityEvidenceSource,
)

RuntimeFactory = Callable[
    [Path, InMemoryEvalTraceCollector], AbstractAsyncContextManager[NexusRuntime]
]


class EvalRunner:
    def __init__(
        self,
        cases_root: Path,
        runtime_factory: RuntimeFactory,
        evaluator: DefaultDeterministicEvaluator | None = None,
    ) -> None:
        self._cases_root = cases_root
        self._runtime_factory = runtime_factory
        self._evaluator = evaluator or DefaultDeterministicEvaluator()

    async def run_case(self, case: EvalCase) -> EvalCaseReport:
        fixture = self._cases_root / case.case_id / case.initial_repo_state
        try:
            validate_fixture_source(fixture)
        except FixtureConstraintError as exc:
            return _evaluator_error_report(case, str(exc))
        except Exception as exc:
            return await self._infrastructure_report(
                case,
                _empty_checks(),
                f"Fixture validation failed: {type(exc).__name__}",
            )
        timeout = case.validation_timeout_seconds or DEFAULT_EVAL_VALIDATION_TIMEOUT_SECONDS
        pre_checks, pre_error = await self._run_preconditions(case, fixture, timeout)
        if pre_error is not None:
            execution = EvalExecutionResult(
                case_id=case.case_id,
                run_id=None,
                session_id=None,
                runtime_status=None,
                terminal_status=None,
                error_code=None,
                final_result_summary=None,
                repository=_empty_repository(case.forbidden_files),
                checks=pre_checks,
                metrics=None,
                security_evidence=(),
                infrastructure_error=pre_error,
            )
            result = await self._evaluator.evaluate(case, execution)
            return _case_report(case, execution, result)
        if _precondition_mismatch(case, pre_checks):
            execution = EvalExecutionResult(
                case_id=case.case_id,
                run_id=None,
                session_id=None,
                runtime_status=None,
                terminal_status=None,
                error_code=None,
                final_result_summary=None,
                repository=_empty_repository(case.forbidden_files),
                checks=pre_checks,
                metrics=None,
                security_evidence=(),
                infrastructure_error=None,
            )
            result = await self._evaluator.evaluate(case, execution)
            return _case_report(case, execution, result)

        with tempfile.TemporaryDirectory(prefix=f"nexus-{case.case_id.lower()}-") as raw:
            workspace = Path(raw) / "repo"
            try:
                EvaluationHarness.copy_fixture(fixture, workspace)
                git_results = await EvaluationHarness.initialize_git(workspace)
                setup_error = _git_setup_error(git_results)
                if setup_error:
                    return await self._infrastructure_report(case, pre_checks, setup_error)
                baseline = snapshot_repository(workspace, case.forbidden_files)
            except Exception as exc:
                return await self._infrastructure_report(
                    case, pre_checks, f"Fixture setup failed: {type(exc).__name__}"
                )

            collector = InMemoryEvalTraceCollector()
            run_id: str | None = None
            session_id: str | None = None
            runtime_status: RuntimeStatus | None = None
            terminal_status = None
            error_code: str | None = None
            final_summary: str | None = None
            security: list[SecurityEvidence] = []
            infrastructure_error: str | None = None
            runtime_invoked = False
            try:
                async with self._runtime_factory(workspace, collector) as runtime:
                    runtime_invoked = True
                    async for event in runtime.run(case.task):
                        run_id = run_id or event.run_id
                        session_id = event.session_id or session_id
                        if isinstance(event, FinalResult):
                            runtime_status = event.status
                            terminal_status = event.terminal_status
                            final_summary = event.content
                        elif isinstance(event, ErrorOccurred):
                            runtime_status = event.status
                            error_code = event.code
                        elif isinstance(event, RunInterrupted):
                            runtime_status = event.status
                        elif isinstance(event, ToolFinished):
                            evidence = _security_evidence(event)
                            if evidence is not None:
                                security.append(evidence)
            except Exception as exc:
                infrastructure_error = f"Runtime bootstrap failed: {type(exc).__name__}"

            try:
                # Diagnostic-only boundary: authoritative evidence is captured again
                # after all post-run validation and discovery operations.
                _post_agent_state = snapshot_repository(workspace, case.forbidden_files)
                expected_nodes = tuple(
                    node
                    for assertion in case.deterministic_success_conditions
                    if assertion.assertion_type is EvalAssertionType.TARGETED_TEST_ADDED
                    for node in assertion.expected_test_node_ids
                )
                post_validation = await EvaluationHarness.validation(
                    workspace, case.validation_command, timeout, expected_nodes
                )
                post_discovery = await EvaluationHarness.discovery(
                    workspace, case.test_discovery_command, timeout
                )
                final = snapshot_repository(workspace, case.forbidden_files)
                text_paths = tuple(
                    assertion.path
                    for assertion in case.deterministic_success_conditions
                    if assertion.assertion_type is EvalAssertionType.REQUIRED_TEXT_IN_FILE
                    and assertion.path is not None
                )
                repository = compare_repository_states(baseline, final, text_paths, workspace)
            except Exception as exc:
                return await self._infrastructure_report(
                    case,
                    pre_checks,
                    f"Post-run Harness failed: {type(exc).__name__}",
                )
            checks = EvalCheckEvidence(
                pre_checks.pre_validation,
                post_validation,
                pre_checks.pre_discovery,
                post_discovery,
            )
            finish = None if run_id is None else collector.get_finish(run_id)
            if infrastructure_error is None and runtime_invoked and finish is None:
                infrastructure_error = "Authoritative TraceRunFinish is missing."
            if finish is not None:
                runtime_status = finish.runtime_status
                terminal_status = finish.terminal_status
                error_code = finish.error_code
            execution = EvalExecutionResult(
                case_id=case.case_id,
                run_id=run_id,
                session_id=session_id,
                runtime_status=runtime_status,
                terminal_status=terminal_status,
                error_code=error_code,
                final_result_summary=final_summary,
                repository=repository,
                checks=checks,
                metrics=None if finish is None else metrics_from_finish(finish),
                security_evidence=tuple(security),
                infrastructure_error=infrastructure_error,
            )
            result = await self._evaluator.evaluate(case, execution)
            return _case_report(case, execution, result)

    async def run_suite(self, load_result: EvalSuiteLoadResult) -> EvalSuiteReport:
        errors = tuple(
            EvalSuiteError(error.code, error.message, error.source_path, error.case_id_hint)
            for error in load_result.errors
        )
        reports: list[EvalCaseReport] = []
        if not errors:
            for case in sorted(load_result.cases, key=lambda item: item.case_id):
                reports.append(await self.run_case(case))
        return _suite_report(tuple(reports), errors)

    async def _run_preconditions(
        self, case: EvalCase, fixture: Path, timeout_seconds: int
    ) -> tuple[EvalCheckEvidence, str | None]:
        if not case.pre_validation_command and not case.test_discovery_command:
            return EvalCheckEvidence(
                _not_run_validation(),
                _not_run_validation(),
                _not_run_discovery(),
                _not_run_discovery(),
            ), None
        with tempfile.TemporaryDirectory(prefix=f"nexus-{case.case_id.lower()}-pre-") as raw:
            workspace = Path(raw) / "repo"
            try:
                EvaluationHarness.copy_fixture(fixture, workspace)
                git_results = await EvaluationHarness.initialize_git(workspace)
                setup_error = _git_setup_error(git_results)
                if setup_error:
                    return _empty_checks(), setup_error
                validation = await EvaluationHarness.validation(
                    workspace, case.pre_validation_command, timeout_seconds
                )
                discovery = await EvaluationHarness.discovery(
                    workspace, case.test_discovery_command, timeout_seconds
                )
                return EvalCheckEvidence(
                    validation, _not_run_validation(), discovery, _not_run_discovery()
                ), None
            except Exception as exc:
                return _empty_checks(), f"Precondition Harness failed: {type(exc).__name__}"

    async def _infrastructure_report(
        self, case: EvalCase, checks: EvalCheckEvidence, message: str
    ) -> EvalCaseReport:
        execution = EvalExecutionResult(
            case.case_id,
            None,
            None,
            None,
            None,
            None,
            None,
            _empty_repository(case.forbidden_files),
            checks,
            None,
            (),
            message,
        )
        result = await self._evaluator.evaluate(case, execution)
        return _case_report(case, execution, result)


def _precondition_mismatch(case: EvalCase, checks: EvalCheckEvidence) -> bool:
    validation = checks.pre_validation
    discovery = checks.pre_discovery
    if (
        validation.status is EvalValidationStatus.TIMED_OUT
        or discovery.status is EvalDiscoveryStatus.TIMED_OUT
    ):
        return True
    if (
        validation.status is EvalValidationStatus.INFRASTRUCTURE_ERROR
        or discovery.status is EvalDiscoveryStatus.INFRASTRUCTURE_ERROR
    ):
        return True
    if validation.exit_code in {2, 3, 4, 5}:
        return True
    if case.case_id in {"EVAL-001", "EVAL-002"}:
        return validation.status is not EvalValidationStatus.FAILED or validation.exit_code != 1
    if (
        case.case_id in {"EVAL-003", "EVAL-004"}
        and validation.status is not EvalValidationStatus.PASSED
    ):
        return True
    if case.case_id == "EVAL-004":
        expected = {
            node
            for assertion in case.deterministic_success_conditions
            if assertion.assertion_type is EvalAssertionType.TARGETED_TEST_ADDED
            for node in assertion.expected_test_node_ids
        }
        return discovery.status is not EvalDiscoveryStatus.PASSED or bool(
            expected & set(discovery.collected_node_ids)
        )
    return False


def _security_evidence(event: ToolFinished) -> SecurityEvidence | None:
    if event.error_code not in {"PERMISSION_DENIED", "COMMAND_DENIED", "MCP_WRITE_NOT_AUTHORIZED"}:
        return None
    if event.error_code == "MCP_WRITE_NOT_AUTHORIZED":
        source = SecurityEvidenceSource.MCP_POLICY
    elif event.error_code == "COMMAND_DENIED":
        if event.approval_decision is ApprovalDecision.DENIED:
            source = SecurityEvidenceSource.APPROVAL_POLICY
        elif event.tool_name == "shell" and event.policy_decision is PolicyDecision.DENIED:
            source = SecurityEvidenceSource.COMMAND_POLICY
        else:
            source = SecurityEvidenceSource.RUNTIME_EVENT
    elif (
        event.tool_name == "shell"
        and event.policy_decision is PolicyDecision.DENIED
        and event.approval_decision is None
    ):
        source = SecurityEvidenceSource.COMMAND_POLICY
    elif event.policy_decision is PolicyDecision.ALLOWED:
        source = SecurityEvidenceSource.TOOL_RUNTIME
    else:
        # ToolFinished does not expose the internal enforcement path. In
        # particular, PERMISSION_DENIED is not proof of WorkspaceGuard origin.
        source = SecurityEvidenceSource.RUNTIME_EVENT
    decision = (
        SecurityDecision.DENIED
        if event.policy_decision is PolicyDecision.DENIED
        or event.approval_decision is ApprovalDecision.DENIED
        else SecurityDecision.BLOCKED
    )
    return SecurityEvidence(source, event.tool_name, decision, event.error_code, event.tool_name)


def _git_setup_error(results: tuple[object, ...]) -> str | None:
    for result in results:
        allowed = getattr(result, "allowed", False)
        exit_code = getattr(result, "exit_code", None)
        timed_out = getattr(result, "timed_out", False)
        infrastructure_error = getattr(result, "infrastructure_error", None)
        if not allowed or exit_code != 0 or timed_out or infrastructure_error:
            return "Evaluation Git baseline setup failed."
    return None


def _case_report(
    case: EvalCase,
    execution: EvalExecutionResult,
    result: DeterministicEvaluationResult,
) -> EvalCaseReport:
    return EvalCaseReport(
        case_id=case.case_id,
        outcome=result.outcome,
        run_id=execution.run_id,
        session_id=execution.session_id,
        runtime_status=execution.runtime_status,
        terminal_status=execution.terminal_status,
        error_code=execution.error_code,
        deterministic_result=result,
        metrics=execution.metrics,
        repository=execution.repository,
        checks=execution.checks,
        security_evidence=execution.security_evidence,
        efficiency_warning=bool(
            execution.metrics and execution.metrics.agent_steps > case.max_reasonable_steps
        ),
        evidence=tuple(
            f"{item.assertion_type.value}={'PASS' if item.passed else 'FAIL'}"
            for item in result.assertion_results
        ),
    )


def _evaluator_error_report(case: EvalCase, message: str) -> EvalCaseReport:
    checks = _empty_checks()
    execution = EvalExecutionResult(
        case.case_id,
        None,
        None,
        None,
        None,
        None,
        None,
        _empty_repository(case.forbidden_files),
        checks,
        None,
        (),
        None,
    )
    assertion_results = tuple(
        EvalAssertionResult(
            assertion.assertion_type,
            False,
            ("fixture contract invalid",),
            message,
        )
        for assertion in case.deterministic_success_conditions
    )
    result = DeterministicEvaluationResult(
        outcome=EvalOutcome.EVALUATOR_ERROR,
        runtime_completed=False,
        validation_passed=None,
        constraints_passed=False,
        forbidden_change_detected=False,
        unauthorized_change_detected=False,
        assertion_results=assertion_results,
        failure_reason=message,
    )
    return _case_report(case, execution, result)


def _suite_report(
    cases: tuple[EvalCaseReport, ...], errors: tuple[EvalSuiteError, ...]
) -> EvalSuiteReport:
    return EvalSuiteReport(
        schema_version="1",
        generated_at=datetime.now(UTC),
        cases=cases,
        suite_errors=errors,
        total_cases=len(cases),
        passed_cases=sum(item.outcome is EvalOutcome.PASS for item in cases),
        task_failed_cases=sum(item.outcome is EvalOutcome.TASK_FAILED for item in cases),
        infrastructure_error_cases=sum(
            item.outcome is EvalOutcome.INFRASTRUCTURE_ERROR for item in cases
        ),
        evaluator_error_cases=sum(item.outcome is EvalOutcome.EVALUATOR_ERROR for item in cases),
    )


def _not_run_validation() -> EvalValidationResult:
    return EvalValidationResult(EvalValidationStatus.NOT_RUN, (), None, "", "", 0, None, None)


def _not_run_discovery() -> EvalDiscoveryResult:
    return EvalDiscoveryResult(EvalDiscoveryStatus.NOT_RUN, (), None, (), "", "", 0, None, None)


def _empty_checks() -> EvalCheckEvidence:
    return EvalCheckEvidence(
        _not_run_validation(), _not_run_validation(), _not_run_discovery(), _not_run_discovery()
    )


def _empty_repository(forbidden: tuple[str, ...]) -> EvalRepositoryEvidence:
    missing = tuple(
        EvalForbiddenPathEvidence(
            path,
            ForbiddenPathSnapshot(path, False, None, None, None, None),
            ForbiddenPathSnapshot(path, False, None, None, None, None),
            False,
        )
        for path in forbidden
    )
    return EvalRepositoryEvidence((), (), (), missing, {})
