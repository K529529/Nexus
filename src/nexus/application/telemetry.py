"""Strict allowlist conversion from business RuntimeEvents to TelemetryEvent v1.0."""

from __future__ import annotations

import json
import re
from pathlib import Path
from uuid import uuid4

from nexus.application.event_publisher import PublishedObservation
from nexus.domain.model import TokenUsage
from nexus.domain.observability import (
    EVENT_PHASES,
    RedactionMetadata,
    TelemetryEvent,
    TelemetryEventType,
    telemetry_payload_dict,
    telemetry_severity,
)
from nexus.domain.runtime_events import (
    AgentStepCompleted,
    ApprovalRequested,
    ApprovalResolved,
    ChangedFileRecorded,
    ContextBuilt,
    ErrorOccurred,
    FinalResult,
    ModelCallFinished,
    ModelCallStarted,
    ObservabilityWarning,
    PlanCreated,
    RepairStarted,
    ReplanOccurred,
    RepositoryExplored,
    RunInterrupted,
    RuntimeEvent,
    TaskStarted,
    ToolFinished,
    ToolStarted,
    ValidationFinished,
    ValidationStarted,
)


class EventEnricher:
    """Build only approved safe fields; arbitrary RuntimeEvent serialization is forbidden."""

    def __init__(
        self,
        workspace: Path,
        tool_sources: dict[str, str] | None = None,
    ) -> None:
        self._workspace = workspace.resolve()
        self._tool_sources = dict(tool_sources or {})
        self._validation_spans: dict[str, str] = {}

    def enrich(
        self,
        event: RuntimeEvent,
        observation: PublishedObservation,
        *,
        tool_call_count: int | None = None,
    ) -> TelemetryEvent:
        event_type, payload, omitted = self._payload(
            event, tool_call_count=tool_call_count
        )
        span_id, parent_span_id = self._spans(event, event_type, observation)
        success = event.success if isinstance(event, ModelCallFinished) else None
        return TelemetryEvent(
            event_id=str(uuid4()),
            event_type=event_type,
            timestamp=event.timestamp,
            sequence=observation.sequence,
            trace_id=observation.context.trace_id,
            execution_id=observation.context.execution_id,
            run_id=event.run_id,
            session_id=event.session_id,
            span_id=span_id,
            parent_span_id=parent_span_id,
            phase=EVENT_PHASES[event_type],
            severity=telemetry_severity(event_type, model_call_success=success),
            payload=payload,
            redaction=RedactionMetadata(
                redacted=bool(omitted),
                omitted_fields=omitted,
            ),
        )

    def _payload(
        self, event: RuntimeEvent, *, tool_call_count: int | None
    ) -> tuple[TelemetryEventType, dict[str, object], tuple[str, ...]]:
        if isinstance(event, TaskStarted):
            return (
                TelemetryEventType.RUN_STARTED,
                {"task_character_count": len(event.task)},
                ("task",),
            )
        if isinstance(event, RepositoryExplored):
            return (
                TelemetryEventType.REPOSITORY_EXPLORED,
                {
                    "instruction_path_count": len(event.instruction_paths),
                    "manifest_path_count": len(event.manifest_paths),
                    "relevant_path_count": len(event.relevant_paths),
                    "exploration_tool_calls": event.exploration_tool_calls,
                    "truncated": event.truncated,
                },
                ("repository_paths",),
            )
        if isinstance(event, ContextBuilt):
            return (
                TelemetryEventType.CONTEXT_BUILT,
                {
                    "selected_path_count": len(event.selected_paths),
                    "retained_characters": event.retained_characters,
                    "truncated": event.truncated,
                    "selected_chunk_count": event.selected_chunk_count,
                    "semantic_retrieval_used": event.semantic_retrieval_used,
                    "semantic_retrieval_status": event.semantic_retrieval_status,
                    "selected_skill_ids": list(event.selected_skill_ids),
                    "selected_skill_count": len(event.selected_skill_ids),
                    "selection_reason_present": bool(event.skill_selection_reason_summary),
                },
                ("repository_paths", "skill_selection_reason"),
            )
        if isinstance(event, PlanCreated):
            return (
                TelemetryEventType.PLAN_CREATED,
                {
                    "plan_id": event.plan_id,
                    "plan_version": event.plan_version,
                    "plan_kind": event.plan_kind.value,
                    "step_count": len(event.step_summaries),
                    "replan_reason_present": bool(event.replan_reason),
                },
                ("step_summaries", "replan_reason"),
            )
        if isinstance(event, ApprovalRequested):
            return (
                TelemetryEventType.APPROVAL_REQUESTED,
                {
                    "approval_id": event.approval_id,
                    "subject": event.subject.value,
                    "invocation_id": event.invocation_id,
                    "plan_id": event.plan_id,
                    "plan_version": event.plan_version,
                    "operation": event.operation,
                    "risk_level": event.risk_level.value,
                },
                ("resource_or_command_summary",),
            )
        if isinstance(event, ApprovalResolved):
            return (
                TelemetryEventType.APPROVAL_RESOLVED,
                {
                    "approval_id": event.approval_id,
                    "subject": event.subject.value,
                    "invocation_id": event.invocation_id,
                    "plan_id": event.plan_id,
                    "plan_version": event.plan_version,
                    "decision": event.decision.value,
                    "actor_category": event.actor_category.value,
                },
                (),
            )
        if isinstance(event, AgentStepCompleted):
            return (
                TelemetryEventType.AGENT_STEP_COMPLETED,
                {
                    "step_count": event.step_count,
                    "decision_kind": event.decision_kind.value,
                    "model_call_id": event.model_call_id,
                },
                (),
            )
        if isinstance(event, ModelCallStarted):
            return (
                TelemetryEventType.MODEL_CALL_STARTED,
                {
                    "model_call_id": event.model_call_id,
                    "model_call_phase": event.phase.value,
                    "provider": event.provider,
                    "model": event.model,
                },
                (),
            )
        if isinstance(event, ModelCallFinished):
            return (
                TelemetryEventType.MODEL_CALL_FINISHED,
                {
                    "model_call_id": event.model_call_id,
                    "model_call_phase": event.phase.value,
                    "success": event.success,
                    "duration_ms": event.duration_ms,
                    "usage": _usage_payload(event.usage),
                    "error_code": event.error_code,
                },
                (),
            )
        if isinstance(event, ToolStarted):
            return (
                TelemetryEventType.TOOL_STARTED,
                {
                    "invocation_id": event.invocation_id,
                    "tool_name": event.tool_name,
                    "source": self._tool_sources.get(event.tool_name, "UNKNOWN"),
                    "proposed_risk": event.risk_level.value,
                    "tool_call_count": tool_call_count,
                },
                (),
            )
        if isinstance(event, ToolFinished):
            return (
                TelemetryEventType.TOOL_FINISHED,
                {
                    "invocation_id": event.invocation_id,
                    "tool_name": event.tool_name,
                    "source": self._tool_sources.get(event.tool_name, "UNKNOWN"),
                    "success": event.success,
                    "final_risk": event.risk_level.value,
                    "policy_decision": event.policy_decision.value,
                    "approval_decision": (
                        None
                        if event.approval_decision is None
                        else event.approval_decision.value
                    ),
                    "duration_ms": event.duration_ms,
                    "error_code": event.error_code,
                },
                (),
            )
        if isinstance(event, ReplanOccurred):
            return (
                TelemetryEventType.REPLAN_OCCURRED,
                {
                    "plan_id": event.plan_id,
                    "previous_version": event.previous_version,
                    "new_version": event.new_version,
                    "replan_count": event.replan_count,
                    "reason_present": bool(event.reason),
                },
                ("reason",),
            )
        if isinstance(event, ValidationStarted):
            return (
                TelemetryEventType.VALIDATION_STARTED,
                {
                    "check_ids": list(event.check_ids),
                    "check_kinds": [value.value for value in event.check_kinds],
                },
                (),
            )
        if isinstance(event, ValidationFinished):
            return (
                TelemetryEventType.VALIDATION_FINISHED,
                {
                    "validation_status": event.validation_status.value,
                    "confidence": event.confidence.value,
                    "executed_check_count": event.executed_check_count,
                    "repair_count": event.repair_count,
                    "duration_ms": event.duration_ms,
                },
                (),
            )
        if isinstance(event, RepairStarted):
            return (
                TelemetryEventType.REPAIR_STARTED,
                {
                    "plan_id": event.plan_id,
                    "plan_version": event.plan_version,
                    "repair_count": event.repair_count,
                    "max_repair_attempts": event.max_repair_attempts,
                    "failure_summary_present": bool(event.failure_summary),
                },
                ("failure_summary",),
            )
        if isinstance(event, ChangedFileRecorded):
            self._validate_changed_path(event.relative_path)
            return (
                TelemetryEventType.CHANGED_FILE_RECORDED,
                {
                    "invocation_id": event.invocation_id,
                    "relative_path": event.relative_path.replace("\\", "/"),
                    "change_kind": event.change_kind.value,
                    "changed_file_count": event.changed_file_count,
                },
                (),
            )
        if isinstance(event, RunInterrupted):
            return TelemetryEventType.RUN_INTERRUPTED, {}, ()
        if isinstance(event, FinalResult):
            return (
                TelemetryEventType.RUN_FINISHED,
                {
                    "runtime_status": event.status.value,
                    "terminal_status": event.terminal_status.value,
                    "changed_file_count": len(event.changed_files),
                    "validation_status": (
                        None
                        if event.validation_result is None
                        else event.validation_result.status.value
                    ),
                },
                ("content", "diff", "validation_evidence"),
            )
        if isinstance(event, ErrorOccurred):
            return (
                TelemetryEventType.ERROR_OCCURRED,
                {"error_code": event.code, "retryable": event.retryable},
                ("error_message",),
            )
        if isinstance(event, ObservabilityWarning):
            return (
                TelemetryEventType.OBSERVABILITY_WARNING,
                {
                    "code": event.code,
                    "sink": event.sink.value,
                    "operation": event.operation.value,
                    "fallback": event.fallback.value,
                },
                (),
            )
        raise ValueError("RuntimeEvent type is not registered for Day 8 telemetry.")

    def _spans(
        self,
        event: RuntimeEvent,
        event_type: TelemetryEventType,
        observation: PublishedObservation,
    ) -> tuple[str | None, str | None]:
        root = observation.context.execution_id
        if event_type in {
            TelemetryEventType.RUN_STARTED,
            TelemetryEventType.RUN_INTERRUPTED,
            TelemetryEventType.RUN_FINISHED,
            TelemetryEventType.ERROR_OCCURRED,
        }:
            return root, None
        if isinstance(event, (ModelCallStarted, ModelCallFinished)):
            return event.model_call_id, root
        if isinstance(event, (ToolStarted, ToolFinished)):
            parent = self._validation_spans.get(observation.context.execution_id, root)
            return event.invocation_id, parent
        if isinstance(event, ValidationStarted):
            span_id = str(uuid4())
            self._validation_spans[observation.context.execution_id] = span_id
            return span_id, root
        if isinstance(event, ValidationFinished):
            return self._validation_spans.pop(observation.context.execution_id, root), root
        return None, root

    def _validate_changed_path(self, relative_path: str) -> None:
        candidate = (self._workspace / relative_path).resolve()
        try:
            candidate.relative_to(self._workspace)
        except ValueError as exc:
            raise ValueError("Changed-file path escapes the workspace.") from exc


def _usage_payload(usage: TokenUsage) -> dict[str, object]:
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "total_tokens": usage.total_tokens,
        "availability": usage.availability.value,
    }


class TelemetryRedactor:
    """Defense-in-depth rejection after the primary event allowlist."""

    _FORBIDDEN = tuple(
        re.compile(pattern, re.IGNORECASE)
        for pattern in (
            r"\b(?:authorization|cookie)\s*[:=]",
            r"\b(?:postgresql|https?)://[^\s/@:]+:[^\s/@]+@",
            r"\bsk-[a-z0-9_-]{12,}",
            r"(?:^|[\s\"'])/[a-z0-9_.-]+/",
            r"[a-z]:\\users\\",
            r"-----BEGIN [A-Z ]+PRIVATE KEY-----",
        )
    )

    def redact(self, event: TelemetryEvent) -> TelemetryEvent:
        serialized = json.dumps(
            {
                "payload": telemetry_payload_dict(event.payload),
                "omitted": event.redaction.omitted_fields,
                "truncated": event.redaction.truncated_fields,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if any(pattern.search(serialized) for pattern in self._FORBIDDEN):
            raise ValueError("Telemetry failed the defense-in-depth redaction boundary.")
        return event
