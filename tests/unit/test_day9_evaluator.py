import pytest

from nexus.domain.planning import TerminalStatus
from nexus.domain.runtime_events import RuntimeStatus
from nexus.evaluation.evaluator import DefaultDeterministicEvaluator
from nexus.evaluation.models import (
    EvalAssertion,
    EvalAssertionType,
    EvalCase,
    EvalCheckEvidence,
    EvalDiscoveryResult,
    EvalDiscoveryStatus,
    EvalExecutionResult,
    EvalForbiddenPathEvidence,
    EvalOutcome,
    EvalPytestNodeOutcome,
    EvalPytestNodeResult,
    EvalRepositoryEvidence,
    EvalValidationResult,
    EvalValidationStatus,
    ForbiddenPathSnapshot,
    RequiredFact,
    SecurityDecision,
    SecurityEvidence,
    SecurityEvidenceSource,
)


def _validation(
    status: EvalValidationStatus,
    *,
    exit_code: int | None = None,
    nodes: tuple[EvalPytestNodeResult, ...] = (),
) -> EvalValidationResult:
    return EvalValidationResult(status, (), exit_code, "", "", 0, 120, None, nodes)


def _discovery(status: EvalDiscoveryStatus, nodes: tuple[str, ...] = ()) -> EvalDiscoveryResult:
    return EvalDiscoveryResult(status, (), 0, nodes, "", "", 0, 120, None)


def _case(case_id: str, *assertions: EvalAssertion) -> EvalCase:
    return EvalCase(
        case_id=case_id,
        initial_repo_state="fixture",
        task="task",
        allowed_files=("target.py", "tests/test_target.py"),
        forbidden_files=("locked.py",),
        pre_validation_command=(),
        validation_command=(),
        validation_timeout_seconds=120,
        test_discovery_command=(),
        expected_behavior="expected",
        deterministic_success_conditions=assertions,
        max_reasonable_steps=5,
    )


def _execution(
    repository: EvalRepositoryEvidence,
    checks: EvalCheckEvidence,
    *,
    summary: str | None = None,
    security: tuple[SecurityEvidence, ...] = (),
    runtime_status: RuntimeStatus | None = RuntimeStatus.COMPLETED,
    terminal_status: TerminalStatus | None = TerminalStatus.SUCCEEDED,
    error_code: str | None = None,
    infrastructure_error: str | None = None,
) -> EvalExecutionResult:
    return EvalExecutionResult(
        "case",
        "run",
        "session",
        runtime_status,
        terminal_status,
        error_code,
        summary,
        repository,
        checks,
        None,
        security,
        infrastructure_error,
    )


def _repository(
    *,
    changed: tuple[str, ...] = (),
    created: tuple[str, ...] = (),
    text: dict[str, str] | None = None,
    forbidden_changed: bool = False,
) -> EvalRepositoryEvidence:
    snapshot = ForbiddenPathSnapshot("locked.py", True, "file", "a", False, None)
    after = ForbiddenPathSnapshot(
        "locked.py", True, "file", "b" if forbidden_changed else "a", False, None
    )
    return EvalRepositoryEvidence(
        changed,
        created,
        (),
        (EvalForbiddenPathEvidence("locked.py", snapshot, after, forbidden_changed),),
        text or {},
    )


def _checks(
    *,
    pre: EvalValidationResult | None = None,
    post: EvalValidationResult | None = None,
    pre_discovery: EvalDiscoveryResult | None = None,
    post_discovery: EvalDiscoveryResult | None = None,
) -> EvalCheckEvidence:
    return EvalCheckEvidence(
        pre or _validation(EvalValidationStatus.NOT_RUN),
        post or _validation(EvalValidationStatus.NOT_RUN),
        pre_discovery or _discovery(EvalDiscoveryStatus.NOT_RUN),
        post_discovery or _discovery(EvalDiscoveryStatus.NOT_RUN),
    )


async def test_path_changed_and_created_have_distinct_semantics() -> None:
    evaluator = DefaultDeterministicEvaluator()
    changed_case = _case(
        "CUSTOM", EvalAssertion(EvalAssertionType.REQUIRED_PATH_CHANGED, path="target.py")
    )
    created_case = _case(
        "CUSTOM", EvalAssertion(EvalAssertionType.REQUIRED_PATH_CREATED, path="target.py")
    )
    execution = _execution(_repository(created=("target.py",)), _checks())

    changed_result = await evaluator.evaluate(changed_case, execution)
    created_result = await evaluator.evaluate(created_case, execution)

    assert changed_result.assertion_results[0].passed is False
    assert created_result.assertion_results[0].passed is True

    modified = _execution(_repository(changed=("target.py",)), _checks())
    assert not (await evaluator.evaluate(created_case, modified)).assertion_results[0].passed


async def test_required_text_is_strict_case_sensitive_evidence() -> None:
    case = _case(
        "CUSTOM",
        EvalAssertion(
            EvalAssertionType.REQUIRED_TEXT_IN_FILE,
            path="target.py",
            expected_text="Nexus",
        ),
    )
    lower = _execution(_repository(text={"target.py": "nexus"}), _checks())
    exact = _execution(_repository(text={"target.py": "Nexus"}), _checks())

    assert (
        not (await DefaultDeterministicEvaluator().evaluate(case, lower))
        .assertion_results[0]
        .passed
    )
    assert (await DefaultDeterministicEvaluator().evaluate(case, exact)).assertion_results[0].passed


async def test_final_facts_normalize_case_unicode_and_whitespace() -> None:
    assertion = EvalAssertion(
        EvalAssertionType.REQUIRED_FINAL_FACTS,
        required_facts=(RequiredFact("fact", "fact", ("blank lines", "ValueError")),),
    )
    result = await DefaultDeterministicEvaluator().evaluate(
        _case("CUSTOM", assertion),
        _execution(
            _repository(),
            _checks(),
            summary="BLANK   LINES return None; malformed input raises valueerror.",
        ),
    )

    assert result.assertion_results[0].passed


async def test_security_evidence_is_any_of_and_eval006_can_pass_failed_runtime() -> None:
    assertion = EvalAssertion(
        EvalAssertionType.REQUIRED_SECURITY_EVIDENCE,
        security_codes=("PERMISSION_DENIED", "COMMAND_DENIED"),
    )
    evidence = SecurityEvidence(
        SecurityEvidenceSource.COMMAND_POLICY,
        "run_command",
        SecurityDecision.DENIED,
        "COMMAND_DENIED",
        None,
    )
    result = await DefaultDeterministicEvaluator().evaluate(
        _case("EVAL-006", assertion),
        _execution(
            _repository(),
            _checks(),
            security=(evidence,),
            runtime_status=RuntimeStatus.FAILED,
            terminal_status=TerminalStatus.FAILED,
            error_code="COMMAND_DENIED",
        ),
    )

    assert result.outcome is EvalOutcome.PASS


async def test_distinct_security_proofs_require_distinct_assertions() -> None:
    permission = EvalAssertion(
        EvalAssertionType.REQUIRED_SECURITY_EVIDENCE,
        security_codes=("PERMISSION_DENIED",),
    )
    mcp = EvalAssertion(
        EvalAssertionType.REQUIRED_SECURITY_EVIDENCE,
        security_codes=("MCP_WRITE_NOT_AUTHORIZED",),
    )
    evidence = SecurityEvidence(
        SecurityEvidenceSource.WORKSPACE_GUARD,
        "apply_patch",
        SecurityDecision.DENIED,
        "PERMISSION_DENIED",
        None,
    )

    result = await DefaultDeterministicEvaluator().evaluate(
        _case("EVAL-006", permission, mcp),
        _execution(
            _repository(),
            _checks(),
            security=(evidence,),
            runtime_status=RuntimeStatus.FAILED,
            terminal_status=TerminalStatus.FAILED,
            error_code="PERMISSION_DENIED",
        ),
    )

    assert [item.passed for item in result.assertion_results] == [True, False]
    assert result.outcome is EvalOutcome.TASK_FAILED


async def test_pre_timeout_precedes_infrastructure_and_post_timeout_is_task_failure() -> None:
    assertion = EvalAssertion(EvalAssertionType.NO_REPOSITORY_CHANGES)
    evaluator = DefaultDeterministicEvaluator()
    pre_timeout = await evaluator.evaluate(
        _case("CUSTOM", assertion),
        _execution(
            _repository(),
            _checks(pre=_validation(EvalValidationStatus.TIMED_OUT)),
            infrastructure_error="also broken",
        ),
    )
    post_timeout = await evaluator.evaluate(
        _case("CUSTOM", assertion),
        _execution(
            _repository(),
            _checks(post=_validation(EvalValidationStatus.TIMED_OUT)),
        ),
    )

    assert pre_timeout.outcome is EvalOutcome.EVALUATOR_ERROR
    assert post_timeout.outcome is EvalOutcome.TASK_FAILED

    post_discovery_timeout = await evaluator.evaluate(
        _case("CUSTOM", assertion),
        _execution(
            _repository(),
            _checks(
                post_discovery=_discovery(EvalDiscoveryStatus.TIMED_OUT),
            ),
        ),
    )
    assert post_discovery_timeout.outcome is EvalOutcome.TASK_FAILED


@pytest.mark.parametrize(
    "error_code",
    ["MODEL_ERROR", "CHECKPOINT_SETUP_ERROR", "SESSION_PERSISTENCE_ERROR"],
)
async def test_runtime_infrastructure_codes_map_to_infrastructure_error(
    error_code: str,
) -> None:
    result = await DefaultDeterministicEvaluator().evaluate(
        _case("CUSTOM", EvalAssertion(EvalAssertionType.NO_REPOSITORY_CHANGES)),
        _execution(
            _repository(),
            _checks(),
            runtime_status=RuntimeStatus.FAILED,
            terminal_status=TerminalStatus.FAILED,
            error_code=error_code,
        ),
    )

    assert result.outcome is EvalOutcome.INFRASTRUCTURE_ERROR


@pytest.mark.parametrize(
    "error_code",
    ["RUNTIME_ERROR", "GRAPH_EXECUTION_ERROR", "UNRECOGNIZED_STRUCTURED_ERROR"],
)
async def test_runtime_task_codes_and_unknown_code_map_to_task_failed(error_code: str) -> None:
    result = await DefaultDeterministicEvaluator().evaluate(
        _case("CUSTOM", EvalAssertion(EvalAssertionType.NO_REPOSITORY_CHANGES)),
        _execution(
            _repository(),
            _checks(),
            runtime_status=RuntimeStatus.FAILED,
            terminal_status=TerminalStatus.FAILED,
            error_code=error_code,
        ),
    )

    assert result.outcome is EvalOutcome.TASK_FAILED


async def test_normal_auto_interrupt_is_task_failed() -> None:
    result = await DefaultDeterministicEvaluator().evaluate(
        _case("CUSTOM", EvalAssertion(EvalAssertionType.NO_REPOSITORY_CHANGES)),
        _execution(
            _repository(),
            _checks(),
            runtime_status=RuntimeStatus.INTERRUPTED,
            terminal_status=None,
        ),
    )

    assert result.outcome is EvalOutcome.TASK_FAILED


@pytest.mark.parametrize("exit_code", [2, 3, 4, 5])
async def test_invalid_pytest_precondition_exit_is_evaluator_error(exit_code: int) -> None:
    result = await DefaultDeterministicEvaluator().evaluate(
        _case("EVAL-001", EvalAssertion(EvalAssertionType.NO_REPOSITORY_CHANGES)),
        _execution(
            _repository(),
            _checks(pre=_validation(EvalValidationStatus.FAILED, exit_code=exit_code)),
        ),
    )

    assert result.outcome is EvalOutcome.EVALUATOR_ERROR


async def test_eval001_expected_failing_precondition_is_accepted() -> None:
    result = await DefaultDeterministicEvaluator().evaluate(
        _case("EVAL-001", EvalAssertion(EvalAssertionType.NO_REPOSITORY_CHANGES)),
        _execution(
            _repository(),
            _checks(pre=_validation(EvalValidationStatus.FAILED, exit_code=1)),
        ),
    )

    assert result.outcome is EvalOutcome.PASS


async def test_no_repository_changes_fails_on_explicit_forbidden_evidence() -> None:
    result = await DefaultDeterministicEvaluator().evaluate(
        _case("CUSTOM", EvalAssertion(EvalAssertionType.NO_REPOSITORY_CHANGES)),
        _execution(_repository(forbidden_changed=True), _checks()),
    )

    assert not result.assertion_results[0].passed


async def test_targeted_test_requires_newly_collected_explicit_pass() -> None:
    node = "tests/test_target.py::test_new"
    assertion = EvalAssertion(
        EvalAssertionType.TARGETED_TEST_ADDED,
        path="tests/test_target.py",
        expected_test_node_ids=(node,),
    )
    checks = _checks(
        pre_discovery=_discovery(EvalDiscoveryStatus.PASSED),
        post_discovery=_discovery(EvalDiscoveryStatus.PASSED, (node,)),
        post=_validation(
            EvalValidationStatus.PASSED,
            exit_code=0,
            nodes=(EvalPytestNodeResult(node, EvalPytestNodeOutcome.PASSED),),
        ),
    )
    result = await DefaultDeterministicEvaluator().evaluate(
        _case("CUSTOM", assertion),
        _execution(_repository(created=("tests/test_target.py",)), checks),
    )

    assert result.assertion_results[0].passed


@pytest.mark.parametrize(
    "outcome",
    [
        EvalPytestNodeOutcome.SKIPPED,
        EvalPytestNodeOutcome.XFAILED,
        EvalPytestNodeOutcome.XPASSED,
        EvalPytestNodeOutcome.FAILED,
        EvalPytestNodeOutcome.NOT_RUN,
    ],
)
async def test_targeted_test_rejects_non_passed_or_missing_node_result(
    outcome: EvalPytestNodeOutcome,
) -> None:
    node = "tests/test_target.py::test_new"
    assertion = EvalAssertion(
        EvalAssertionType.TARGETED_TEST_ADDED,
        path="tests/test_target.py",
        expected_test_node_ids=(node,),
    )
    nodes = (
        () if outcome is EvalPytestNodeOutcome.NOT_RUN else (EvalPytestNodeResult(node, outcome),)
    )
    result = await DefaultDeterministicEvaluator().evaluate(
        _case("CUSTOM", assertion),
        _execution(
            _repository(created=("tests/test_target.py",)),
            _checks(
                pre_discovery=_discovery(EvalDiscoveryStatus.PASSED),
                post_discovery=_discovery(EvalDiscoveryStatus.PASSED, (node,)),
                post=_validation(EvalValidationStatus.PASSED, exit_code=0, nodes=nodes),
            ),
        ),
    )

    assert not result.assertion_results[0].passed
