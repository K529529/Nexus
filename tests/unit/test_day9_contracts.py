import shutil
from dataclasses import fields, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest

from nexus.application.telemetry import EventEnricher
from nexus.application.telemetry_subscriber import TelemetrySubscriber
from nexus.application.tracing_dispatcher import SafeTracerDispatcher
from nexus.domain.model import TokenUsage, UsageAvailability
from nexus.domain.observability import (
    ExecutionOutcome,
    RunExecutionContext,
    TraceRunFinish,
    TraceValidationStatus,
)
from nexus.domain.planning import TerminalStatus
from nexus.domain.runtime_events import RuntimeStatus
from nexus.evaluation.harness import _resolve_eval_executables
from nexus.evaluation.loader import EvalSuiteLoader
from nexus.evaluation.metrics import InMemoryEvalTraceCollector, metrics_from_finish
from nexus.evaluation.models import EvalCase, EvalCaseReport, EvalExecutionResult


def test_v08_has_one_explicit_pre_validation_and_no_legacy_execution_fields() -> None:
    case_names = [item.name for item in fields(EvalCase)]
    execution_names = [item.name for item in fields(EvalExecutionResult)]
    report_names = [item.name for item in fields(EvalCaseReport)]

    assert case_names.count("pre_validation_command") == 1
    assert "runtime_status" in execution_names
    assert "terminal_status" in execution_names
    assert "error_code" in execution_names
    assert "repository" in execution_names
    assert "runtime_terminal_status" not in execution_names
    assert "runtime_error_codes" not in execution_names
    assert "changed_files" not in execution_names
    assert "repository" in report_names
    assert "changed_files" not in report_names


def test_mandatory_suite_loads_and_freezes_command_relations() -> None:
    result = EvalSuiteLoader().load_suite(Path("evals/cases"))

    assert result.errors == ()
    assert [case.case_id for case in result.cases] == [
        "EVAL-001",
        "EVAL-002",
        "EVAL-003",
        "EVAL-004",
        "EVAL-005",
        "EVAL-006",
    ]
    by_id = {case.case_id: case for case in result.cases}
    for case_id in ("EVAL-001", "EVAL-002", "EVAL-003"):
        assert by_id[case_id].pre_validation_command == by_id[case_id].validation_command
    assert by_id["EVAL-004"].pre_validation_command != by_id["EVAL-004"].validation_command
    assert by_id["EVAL-005"].pre_validation_command == ()
    assert by_id["EVAL-006"].pre_validation_command == ()
    security = next(
        condition
        for condition in by_id["EVAL-006"].deterministic_success_conditions
        if condition.assertion_type.value == "required_security_evidence"
    )
    assert security.security_codes == (
        "PLAN_SCOPE_DENIED",
        "PERMISSION_DENIED",
        "COMMAND_DENIED",
    )


def test_loader_rejects_malformed_pre_post_command_relationship(tmp_path: Path) -> None:
    source = Path("evals/cases/EVAL-001")
    target = tmp_path / "EVAL-001"
    shutil.copytree(source, target)
    case_file = target / "case.toml"
    case_file.write_text(
        case_file.read_text(encoding="utf-8").replace(
            'pre_validation_command = ["pytest", "-q", "tests/test_calculator.py"]',
            'pre_validation_command = ["pytest", "-v", "tests/test_calculator.py"]',
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="equal non-empty"):
        EvalSuiteLoader().load_case(case_file)


def test_loader_accepts_only_configured_trusted_absolute_executable(tmp_path: Path) -> None:
    source = Path("evals/cases/EVAL-001")
    trusted_target = tmp_path / "trusted" / "EVAL-001"
    shutil.copytree(source, trusted_target)
    configured_pytest = _resolve_eval_executables(trusted_target).pytest
    assert configured_pytest is not None
    trusted_pytest = Path(configured_pytest)
    trusted_case = trusted_target / "case.toml"
    trusted_case.write_text(
        trusted_case.read_text(encoding="utf-8").replace(
            '"pytest"', f'"{trusted_pytest.as_posix()}"'
        ),
        encoding="utf-8",
    )

    loaded = EvalSuiteLoader().load_case(trusted_case)

    assert Path(loaded.validation_command[0]).resolve() == trusted_pytest

    untrusted_target = tmp_path / "untrusted" / "EVAL-001"
    shutil.copytree(source, untrusted_target)
    untrusted_pytest = (tmp_path / "untrusted-bin" / trusted_pytest.name).resolve()
    untrusted_case = untrusted_target / "case.toml"
    untrusted_case.write_text(
        untrusted_case.read_text(encoding="utf-8").replace(
            '"pytest"', f'"{untrusted_pytest.as_posix()}"'
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="outside the Day 9 Harness grammar"):
        EvalSuiteLoader().load_case(untrusted_case)


def test_loader_rejects_eval004_validation_missing_expected_node(tmp_path: Path) -> None:
    source = Path("evals/cases/EVAL-004")
    target = tmp_path / "EVAL-004"
    shutil.copytree(source, target)
    case_file = target / "case.toml"
    case_file.write_text(
        case_file.read_text(encoding="utf-8").replace(
            'validation_command = ["pytest", "-vv", '
            '"tests/test_discount.py::test_discount_for_premium_user"]',
            'validation_command = ["pytest", "-vv", "tests/test_discount.py"]',
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="expected nodes must be post-targeted"):
        EvalSuiteLoader().load_case(case_file)


def test_metrics_are_exactly_mapped_from_trace_finish() -> None:
    run_id = str(uuid4())
    context = RunExecutionContext(
        trace_id=run_id,
        execution_id=str(uuid4()),
        run_id=run_id,
        session_id=str(uuid4()),
        is_resume=False,
        started_at=datetime.now(UTC),
    )
    usage = TokenUsage(3, None, None, UsageAvailability.PARTIAL)
    finish = TraceRunFinish(
        context=context,
        finished_at=datetime.now(UTC),
        execution_outcome=ExecutionOutcome.COMPLETED,
        runtime_status=RuntimeStatus.COMPLETED,
        terminal_status=TerminalStatus.SUCCEEDED,
        duration_ms=17,
        step_count=2,
        llm_call_count=3,
        tool_call_count=4,
        replan_count=5,
        repair_count=6,
        token_usage=usage,
        changed_file_count=1,
        validation_status=TraceValidationStatus.PASS,
        error_code=None,
    )
    collector = InMemoryEvalTraceCollector()

    collector.record_finish(finish)
    collected = collector.get_finish(run_id)
    assert collected is not None
    metrics = metrics_from_finish(collected)

    assert metrics.agent_steps == 2
    assert metrics.llm_calls == 3
    assert metrics.tool_calls == 4
    assert metrics.replans == 5
    assert metrics.repairs == 6
    assert metrics.latency_ms == 17
    assert metrics.token_usage is usage

    for preserved in (
        TokenUsage(2, 3, 5, UsageAvailability.REPORTED),
        TokenUsage(2, None, None, UsageAvailability.PARTIAL),
        TokenUsage.unavailable(),
    ):
        assert metrics_from_finish(replace(finish, token_usage=preserved)).token_usage is preserved


def test_telemetry_subscriber_forwards_the_authoritative_finish_to_collector() -> None:
    run_id = str(uuid4())
    context = RunExecutionContext(
        trace_id=run_id,
        execution_id=str(uuid4()),
        run_id=run_id,
        session_id=None,
        is_resume=False,
        started_at=datetime.now(UTC),
    )
    finish = TraceRunFinish(
        context=context,
        finished_at=datetime.now(UTC),
        execution_outcome=ExecutionOutcome.COMPLETED,
        runtime_status=RuntimeStatus.COMPLETED,
        terminal_status=TerminalStatus.SUCCEEDED,
        duration_ms=1,
        step_count=1,
        llm_call_count=1,
        tool_call_count=0,
        replan_count=0,
        repair_count=0,
        token_usage=TokenUsage.unavailable(),
        changed_file_count=0,
        validation_status=TraceValidationStatus.NOT_RUN,
        error_code=None,
    )
    collector = InMemoryEvalTraceCollector()
    subscriber = TelemetrySubscriber(
        cast(EventEnricher, object()),
        cast(SafeTracerDispatcher, object()),
        finish_observer=collector.record_finish,
    )

    subscriber.finish_execution(finish)

    assert collector.get_finish(run_id) is finish
