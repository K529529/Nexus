import tempfile
from pathlib import Path

from nexus.evaluation.harness import (
    EvaluationHarness,
    compare_repository_states,
    snapshot_repository,
)
from nexus.evaluation.loader import EvalSuiteLoader
from nexus.evaluation.models import (
    EvalAssertionType,
    EvalDiscoveryStatus,
    EvalValidationStatus,
)


async def test_mandatory_preconditions_run_in_disposable_copies() -> None:
    cases_root = Path("evals/cases")
    loaded = EvalSuiteLoader().load_suite(cases_root)
    assert loaded.errors == ()

    for case in loaded.cases[:4]:
        with tempfile.TemporaryDirectory(prefix="nexus-day9-pre-test-") as raw:
            workspace = Path(raw) / "repo"
            source = cases_root / case.case_id / case.initial_repo_state
            EvaluationHarness.copy_fixture(source, workspace)
            setup = await EvaluationHarness.initialize_git(workspace)
            assert len(setup) == 3
            assert all(item.allowed and item.exit_code == 0 for item in setup)
            validation = await EvaluationHarness.validation(
                workspace,
                case.pre_validation_command,
                case.validation_timeout_seconds or 120,
            )
            discovery = await EvaluationHarness.discovery(
                workspace,
                case.test_discovery_command,
                case.validation_timeout_seconds or 120,
            )

            if case.case_id in {"EVAL-001", "EVAL-002"}:
                assert validation.status is EvalValidationStatus.FAILED
                assert validation.exit_code == 1
            else:
                assert validation.status is EvalValidationStatus.PASSED
                assert validation.exit_code == 0
            if case.case_id == "EVAL-004":
                assert discovery.status is EvalDiscoveryStatus.PASSED
                expected = {
                    node
                    for assertion in case.deterministic_success_conditions
                    if assertion.assertion_type is EvalAssertionType.TARGETED_TEST_ADDED
                    for node in assertion.expected_test_node_ids
                }
                assert expected.isdisjoint(discovery.collected_node_ids)

    assert not any(
        path.name in {".pytest_cache", "__pycache__"}
        for path in (cases_root / "EVAL-004" / "fixture").rglob("*")
    )


async def test_eval004_separates_green_precheck_from_targeted_postcheck() -> None:
    cases_root = Path("evals/cases")
    case = EvalSuiteLoader().load_case(cases_root / "EVAL-004" / "case.toml")
    target_assertion = next(
        assertion
        for assertion in case.deterministic_success_conditions
        if assertion.assertion_type is EvalAssertionType.TARGETED_TEST_ADDED
    )
    expected_node = target_assertion.expected_test_node_ids[0]
    with tempfile.TemporaryDirectory(prefix="nexus-day9-eval004-") as raw:
        workspace = Path(raw) / "repo"
        EvaluationHarness.copy_fixture(cases_root / "EVAL-004" / case.initial_repo_state, workspace)
        setup = await EvaluationHarness.initialize_git(workspace)
        assert all(item.allowed and item.exit_code == 0 for item in setup)
        baseline = snapshot_repository(workspace, case.forbidden_files)
        test_file = workspace / "tests" / "test_discount.py"
        test_file.write_text(
            test_file.read_text(encoding="utf-8")
            + "\n\ndef test_discount_for_premium_user() -> None:\n"
            + "    assert discount_percent(premium=True, order_total=100) == 15\n",
            encoding="utf-8",
        )

        post_validation = await EvaluationHarness.validation(
            workspace,
            case.validation_command,
            case.validation_timeout_seconds or 120,
            (expected_node,),
        )
        post_discovery = await EvaluationHarness.discovery(
            workspace,
            case.test_discovery_command,
            case.validation_timeout_seconds or 120,
        )
        final = snapshot_repository(workspace, case.forbidden_files)
        repository = compare_repository_states(baseline, final, (), workspace)

    assert post_validation.status is EvalValidationStatus.PASSED
    assert post_validation.pytest_node_results[0].node_id == expected_node
    assert post_validation.pytest_node_results[0].outcome.value == "PASSED"
    assert post_discovery.status is EvalDiscoveryStatus.PASSED
    assert expected_node in post_discovery.collected_node_ids
    assert repository.changed_files == ("tests/test_discount.py",)
