import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest

from nexus.application.runtime import NexusRuntime
from nexus.domain.model import TokenUsage
from nexus.domain.observability import (
    ExecutionOutcome,
    RunExecutionContext,
    TraceRunFinish,
    TraceValidationStatus,
)
from nexus.domain.planning import TerminalStatus
from nexus.domain.runtime_events import (
    ErrorOccurred,
    FinalResult,
    RuntimeEvent,
    RuntimeStatus,
    TaskStarted,
    ToolFinished,
)
from nexus.domain.tooling import ApprovalDecision, PolicyDecision, RiskLevel
from nexus.evaluation.fixtures import FixtureConstraintError
from nexus.evaluation.loader import EvalSuiteLoader
from nexus.evaluation.metrics import InMemoryEvalTraceCollector
from nexus.evaluation.models import EvalOutcome, SecurityEvidenceSource
from nexus.evaluation.runner import EvalRunner, RuntimeFactory, _security_evidence


class _FakeRuntime:
    def __init__(
        self,
        collector: InMemoryEvalTraceCollector,
        *,
        summary: str | None = None,
        error_code: str | None = None,
    ) -> None:
        self._collector = collector
        self._summary = summary
        self._error_code = error_code

    async def run(self, task: str, session_id: str | None = None) -> AsyncIterator[RuntimeEvent]:
        run_id = str(uuid4())
        resolved_session = str(uuid4())
        context = RunExecutionContext(
            trace_id=run_id,
            execution_id=str(uuid4()),
            run_id=run_id,
            session_id=resolved_session,
            is_resume=False,
            started_at=datetime.now(UTC),
        )
        yield TaskStarted(run_id=run_id, session_id=resolved_session, task=task)
        if self._error_code is None:
            terminal: RuntimeEvent = FinalResult(
                run_id=run_id,
                session_id=resolved_session,
                content=self._summary or "",
            )
            runtime_status = RuntimeStatus.COMPLETED
            terminal_status = TerminalStatus.SUCCEEDED
            outcome = ExecutionOutcome.COMPLETED
        else:
            yield ToolFinished(
                run_id=run_id,
                session_id=resolved_session,
                invocation_id=str(uuid4()),
                tool_name="run_command",
                success=False,
                risk_level=RiskLevel.DANGEROUS,
                policy_decision=PolicyDecision.DENIED,
                approval_decision=None,
                duration_ms=1,
                error_code=self._error_code,
            )
            terminal = ErrorOccurred(
                run_id=run_id,
                session_id=resolved_session,
                code=self._error_code,
                message="denied",
                retryable=False,
            )
            runtime_status = RuntimeStatus.FAILED
            terminal_status = TerminalStatus.FAILED
            outcome = ExecutionOutcome.FAILED
        yield terminal
        self._collector.record_finish(
            TraceRunFinish(
                context=context,
                finished_at=datetime.now(UTC),
                execution_outcome=outcome,
                runtime_status=runtime_status,
                terminal_status=terminal_status,
                duration_ms=9,
                step_count=2,
                llm_call_count=1,
                tool_call_count=1 if self._error_code else 0,
                replan_count=0,
                repair_count=0,
                token_usage=TokenUsage.unavailable(),
                changed_file_count=0,
                validation_status=TraceValidationStatus.NOT_RUN,
                error_code=self._error_code,
            )
        )


def _factory(*, summary: str | None = None, error_code: str | None = None) -> RuntimeFactory:
    @asynccontextmanager
    async def factory(
        workspace: Path, collector: InMemoryEvalTraceCollector
    ) -> AsyncIterator[NexusRuntime]:
        del workspace
        yield cast(
            NexusRuntime,
            _FakeRuntime(collector, summary=summary, error_code=error_code),
        )

    return factory


async def test_runner_preserves_no_change_facts_and_authoritative_metrics() -> None:
    cases_root = Path("evals/cases")
    case = EvalSuiteLoader().load_case(cases_root / "EVAL-005" / "case.toml")
    runner = EvalRunner(
        cases_root,
        _factory(summary="Blank lines return None. A malformed record raises ValueError."),
    )

    report = await runner.run_case(case)

    assert report.outcome is EvalOutcome.PASS
    assert report.repository.changed_files == ()
    assert report.repository.created_files == ()
    assert report.metrics is not None
    assert report.metrics.agent_steps == 2
    assert report.metrics.llm_calls == 1


@pytest.mark.parametrize("error_code", ["PLAN_SCOPE_DENIED", "COMMAND_DENIED"])
async def test_eval006_formal_denial_can_pass_failed_runtime(error_code: str) -> None:
    cases_root = Path("evals/cases")
    case = EvalSuiteLoader().load_case(cases_root / "EVAL-006" / "case.toml")
    runner = EvalRunner(cases_root, _factory(error_code=error_code))

    report = await runner.run_case(case)

    assert report.outcome is EvalOutcome.PASS
    assert report.runtime_status is RuntimeStatus.FAILED
    assert report.error_code == error_code
    assert report.security_evidence[0].error_code == error_code


async def test_fixture_symlink_escape_is_evaluator_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cases_root = Path("evals/cases")
    case = EvalSuiteLoader().load_case(cases_root / "EVAL-005" / "case.toml")

    def reject_fixture(source: Path) -> Path:
        del source
        raise FixtureConstraintError("Fixture contains a workspace-escaping symlink.")

    monkeypatch.setattr("nexus.evaluation.runner.validate_fixture_source", reject_fixture)
    runner = EvalRunner(cases_root, _factory(summary="unused"))

    report = await runner.run_case(case)

    assert report.outcome is EvalOutcome.EVALUATOR_ERROR
    assert report.metrics is None
    assert report.deterministic_result.failure_reason is not None


async def test_bootstrap_exception_is_infrastructure_error() -> None:
    cases_root = Path("evals/cases")
    case = EvalSuiteLoader().load_case(cases_root / "EVAL-005" / "case.toml")

    @asynccontextmanager
    async def broken_factory(
        workspace: Path, collector: InMemoryEvalTraceCollector
    ) -> AsyncIterator[NexusRuntime]:
        del workspace, collector
        raise RuntimeError("bootstrap unavailable")
        yield cast(NexusRuntime, object())  # pragma: no cover

    report = await EvalRunner(cases_root, broken_factory).run_case(case)

    assert report.outcome is EvalOutcome.INFRASTRUCTURE_ERROR


async def test_missing_authoritative_finish_is_infrastructure_error() -> None:
    cases_root = Path("evals/cases")
    case = EvalSuiteLoader().load_case(cases_root / "EVAL-005" / "case.toml")

    @asynccontextmanager
    async def unobserved_factory(
        workspace: Path, collector: InMemoryEvalTraceCollector
    ) -> AsyncIterator[NexusRuntime]:
        del workspace
        unrelated_collector = InMemoryEvalTraceCollector()
        yield cast(NexusRuntime, _FakeRuntime(unrelated_collector, summary="complete"))

    report = await EvalRunner(cases_root, unobserved_factory).run_case(case)

    assert report.outcome is EvalOutcome.INFRASTRUCTURE_ERROR
    assert report.metrics is None


async def test_invalid_absolute_executable_stops_before_runtime_factory(
    tmp_path: Path,
) -> None:
    cases_root = tmp_path / "cases"
    shutil.copytree(Path("evals/cases"), cases_root)
    case_file = cases_root / "EVAL-001" / "case.toml"
    untrusted = (tmp_path / "untrusted" / "pytest").resolve().as_posix()
    case_file.write_text(
        case_file.read_text(encoding="utf-8").replace('"pytest"', f'"{untrusted}"'),
        encoding="utf-8",
    )
    loaded = EvalSuiteLoader().load_suite(cases_root)
    runtime_factory_called = False

    @asynccontextmanager
    async def forbidden_factory(
        workspace: Path, collector: InMemoryEvalTraceCollector
    ) -> AsyncIterator[NexusRuntime]:
        nonlocal runtime_factory_called
        del workspace, collector
        runtime_factory_called = True
        yield cast(NexusRuntime, object())

    report = await EvalRunner(cases_root, forbidden_factory).run_suite(loaded)

    assert loaded.errors
    assert report.cases == ()
    assert not runtime_factory_called


@pytest.mark.parametrize(
    (
        "tool_name",
        "error_code",
        "policy_decision",
        "approval_decision",
        "expected_source",
    ),
    [
        (
            "write_file",
            "PLAN_SCOPE_DENIED",
            PolicyDecision.DENIED,
            ApprovalDecision.APPROVED,
            SecurityEvidenceSource.TOOL_RUNTIME,
        ),
        (
            "apply_patch",
            "PERMISSION_DENIED",
            PolicyDecision.DENIED,
            ApprovalDecision.DENIED,
            SecurityEvidenceSource.RUNTIME_EVENT,
        ),
        (
            "shell",
            "PERMISSION_DENIED",
            PolicyDecision.DENIED,
            None,
            SecurityEvidenceSource.COMMAND_POLICY,
        ),
        (
            "read_file",
            "PERMISSION_DENIED",
            PolicyDecision.ALLOWED,
            None,
            SecurityEvidenceSource.TOOL_RUNTIME,
        ),
        (
            "write_file",
            "COMMAND_DENIED",
            PolicyDecision.DENIED,
            ApprovalDecision.DENIED,
            SecurityEvidenceSource.APPROVAL_POLICY,
        ),
        (
            "mcp_write",
            "MCP_WRITE_NOT_AUTHORIZED",
            PolicyDecision.DENIED,
            ApprovalDecision.DENIED,
            SecurityEvidenceSource.MCP_POLICY,
        ),
    ],
)
def test_security_evidence_uses_only_publicly_provable_source(
    tool_name: str,
    error_code: str,
    policy_decision: PolicyDecision,
    approval_decision: ApprovalDecision | None,
    expected_source: SecurityEvidenceSource,
) -> None:
    event = ToolFinished(
        run_id=str(uuid4()),
        session_id=str(uuid4()),
        invocation_id=str(uuid4()),
        tool_name=tool_name,
        success=False,
        risk_level=RiskLevel.DANGEROUS,
        policy_decision=policy_decision,
        approval_decision=approval_decision,
        duration_ms=1,
        error_code=error_code,
    )

    evidence = _security_evidence(event)

    assert evidence is not None
    assert evidence.source is expected_source
    if tool_name == "apply_patch":
        assert evidence.source is not SecurityEvidenceSource.WORKSPACE_GUARD


async def test_harness_commands_are_not_counted_as_runtime_tools() -> None:
    cases_root = Path("evals/cases")
    case = EvalSuiteLoader().load_case(cases_root / "EVAL-003" / "case.toml")
    report = await EvalRunner(cases_root, _factory(summary="unchanged")).run_case(case)

    assert report.metrics is not None
    assert report.metrics.tool_calls == 0
