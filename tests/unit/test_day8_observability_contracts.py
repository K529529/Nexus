from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from nexus.domain.model import (
    ModelCallPhase,
    TokenUsage,
    TokenUsageAggregate,
    UsageAvailability,
)
from nexus.domain.observability import (
    EVENT_PHASES,
    ExecutionOutcome,
    RedactionMetadata,
    RunExecutionContext,
    RuntimePhase,
    TelemetryEvent,
    TelemetryEventType,
    TelemetrySeverity,
    TraceRunFinish,
    TraceValidationStatus,
    telemetry_severity,
)
from nexus.domain.planning import TerminalStatus
from nexus.domain.runtime_events import RuntimeStatus


def _context() -> RunExecutionContext:
    run_id = str(uuid4())
    return RunExecutionContext(
        trace_id=run_id,
        execution_id=str(uuid4()),
        run_id=run_id,
        session_id=str(uuid4()),
        is_resume=False,
        started_at=datetime.now(UTC),
    )


def test_runtime_phase_and_model_call_phase_are_separate_frozen_dimensions() -> None:
    assert [item.value for item in RuntimePhase] == [
        "RUN",
        "REPOSITORY",
        "CONTEXT",
        "PLAN",
        "APPROVAL",
        "AGENT",
        "MODEL",
        "TOOL",
        "REPLAN",
        "VALIDATION",
        "REPAIR",
        "CHANGE",
        "ERROR",
        "OBSERVABILITY",
    ]
    assert [item.value for item in ModelCallPhase] == [
        "DIRECT_RESPONSE",
        "PLAN",
        "REPLAN",
        "REPAIR",
        "AGENT_STEP",
        "SKILL_SELECTION",
    ]
    assert EVENT_PHASES[TelemetryEventType.MODEL_CALL_FINISHED] is RuntimePhase.MODEL


def test_token_usage_rejects_inconsistent_or_estimated_values() -> None:
    reported = TokenUsage(3, 5, 8, UsageAvailability.REPORTED)
    assert TokenUsageAggregate().add(reported).to_usage() == reported

    unavailable = TokenUsage.unavailable()
    aggregate = TokenUsageAggregate().add(reported).add(unavailable)
    assert aggregate.usage_complete is False
    assert aggregate.to_usage() == TokenUsage(3, 5, None, UsageAvailability.PARTIAL)

    with pytest.raises(ValueError, match="complete usage triple"):
        TokenUsage(3, 5, None, UsageAvailability.REPORTED)
    with pytest.raises(ValueError, match="must equal"):
        TokenUsage(3, 5, 9, UsageAvailability.REPORTED)
    with pytest.raises(ValueError, match="negative"):
        TokenUsage(-1, 5, 4, UsageAvailability.REPORTED)


def test_execution_context_requires_canonical_identity_and_is_immutable() -> None:
    context = _context()
    with pytest.raises(FrozenInstanceError):
        context.execution_id = str(uuid4())  # type: ignore[misc]
    with pytest.raises(ValueError, match="trace_id must equal run_id"):
        RunExecutionContext(
            trace_id=str(uuid4()),
            execution_id=str(uuid4()),
            run_id=str(uuid4()),
            session_id=None,
            is_resume=False,
            started_at=datetime.now(UTC),
        )


def test_telemetry_event_enforces_phase_severity_json_and_payload_immutability() -> None:
    context = _context()
    payload: dict[str, object] = {"success": False, "nested": {"safe": True}}
    event = TelemetryEvent(
        event_id=str(uuid4()),
        event_type=TelemetryEventType.MODEL_CALL_FINISHED,
        timestamp=datetime.now(UTC),
        sequence=1,
        trace_id=context.trace_id,
        execution_id=context.execution_id,
        run_id=context.run_id,
        session_id=context.session_id,
        span_id=str(uuid4()),
        parent_span_id=None,
        phase=RuntimePhase.MODEL,
        severity=TelemetrySeverity.ERROR,
        payload=payload,
        redaction=RedactionMetadata(),
    )
    payload["success"] = True
    assert event.payload["success"] is False

    with pytest.raises(TypeError):
        event.payload["success"] = True  # type: ignore[index]
    with pytest.raises(ValueError, match="RuntimePhase"):
        TelemetryEvent(
            event_id=str(uuid4()),
            event_type=TelemetryEventType.MODEL_CALL_FINISHED,
            timestamp=datetime.now(UTC),
            sequence=1,
            trace_id=context.trace_id,
            execution_id=context.execution_id,
            run_id=context.run_id,
            session_id=context.session_id,
            span_id=None,
            parent_span_id=None,
            phase=RuntimePhase.PLAN,
            severity=TelemetrySeverity.ERROR,
            payload={"success": False},
            redaction=RedactionMetadata(),
        )
    with pytest.raises(ValueError, match="severity"):
        TelemetryEvent(
            event_id=str(uuid4()),
            event_type=TelemetryEventType.TOOL_FINISHED,
            timestamp=datetime.now(UTC),
            sequence=1,
            trace_id=context.trace_id,
            execution_id=context.execution_id,
            run_id=context.run_id,
            session_id=context.session_id,
            span_id=None,
            parent_span_id=None,
            phase=RuntimePhase.TOOL,
            severity=TelemetrySeverity.ERROR,
            payload={"success": False},
            redaction=RedactionMetadata(),
        )


def test_severity_mapping_preserves_tool_failure_and_interruption_as_info() -> None:
    assert telemetry_severity(TelemetryEventType.TOOL_FINISHED) is TelemetrySeverity.INFO
    assert telemetry_severity(TelemetryEventType.RUN_INTERRUPTED) is TelemetrySeverity.INFO
    assert (
        telemetry_severity(
            TelemetryEventType.MODEL_CALL_FINISHED, model_call_success=False
        )
        is TelemetrySeverity.ERROR
    )


def test_trace_finish_preserves_all_three_outcome_layers() -> None:
    finish = TraceRunFinish(
        context=_context(),
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
    assert finish.execution_outcome is ExecutionOutcome.COMPLETED

    with pytest.raises(ValueError, match="SUCCEEDED"):
        TraceRunFinish(
            context=_context(),
            finished_at=datetime.now(UTC),
            execution_outcome=ExecutionOutcome.FAILED,
            runtime_status=RuntimeStatus.FAILED,
            terminal_status=TerminalStatus.SUCCEEDED,
            duration_ms=1,
            step_count=0,
            llm_call_count=0,
            tool_call_count=0,
            replan_count=0,
            repair_count=0,
            token_usage=TokenUsage.unavailable(),
            changed_file_count=0,
            validation_status=TraceValidationStatus.NOT_RUN,
            error_code="SAFE_FAILURE",
        )
