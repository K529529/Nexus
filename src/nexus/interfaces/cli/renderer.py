"""Render safe RuntimeEvents for a terminal consumer."""

from __future__ import annotations

import typer

from nexus.domain.runtime_events import (
    AgentStepCompleted,
    ApprovalRequested,
    ApprovalResolved,
    ApprovalSubject,
    ChangedFileRecorded,
    ContextBuilt,
    ErrorOccurred,
    FinalResult,
    ModelCallFinished,
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


def render_event(event: RuntimeEvent) -> bool:
    """Render one event and return whether it represents success/continuation."""

    if isinstance(event, TaskStarted):
        typer.echo("Task started")
        return True
    if isinstance(event, RepositoryExplored):
        typer.echo(
            f"Repository explored ({len(event.relevant_paths)} relevant paths; "
            f"{event.exploration_tool_calls} tool calls)"
        )
        return True
    if isinstance(event, ContextBuilt) and event.semantic_retrieval_status:
        code = event.semantic_retrieval_status
        remediation = ("Run nexus index --rebuild." if code == "INDEX_INCOMPATIBLE"
                       else "Run nexus index." if code == "INDEX_NOT_FOUND"
                       else "Check embedding/database availability and retry.")
        typer.echo(f"Context warning [{code}]: using lexical context. {remediation}", err=True)
        return True
    if isinstance(event, FinalResult):
        typer.echo(event.content)
        if event.diff:
            typer.echo(event.diff)
        return event.status.value == "COMPLETED"
    if isinstance(event, PlanCreated):
        typer.echo(f"Plan {event.plan_id} v{event.plan_version}")
        for summary in event.step_summaries:
            typer.echo(f"  {summary}")
        return True
    if isinstance(event, ApprovalRequested) and event.subject is ApprovalSubject.PLAN:
        typer.echo("Plan approval required")
        typer.echo(f"  scope digest: {event.resource_or_command_summary}")
        return True
    if isinstance(event, ApprovalResolved):
        typer.echo(f"Approval {event.decision.value.lower()} ({event.subject.value.lower()})")
        return True
    if isinstance(event, AgentStepCompleted):
        typer.echo(
            f"Agent step {event.step_count} completed ({event.decision_kind.value.lower()})"
        )
        return True
    if isinstance(event, ModelCallFinished):
        usage = event.usage.availability.value.lower()
        typer.echo(
            f"Model {event.phase.value.lower()} finished in {event.duration_ms} ms "
            f"(reported tokens: {usage})"
        )
        return True
    if isinstance(event, ToolStarted):
        typer.echo(f"Tool {event.tool_name} started")
        return True
    if isinstance(event, ToolFinished):
        outcome = "succeeded" if event.success else f"failed [{event.error_code}]"
        typer.echo(f"Tool {event.tool_name} {outcome} in {event.duration_ms} ms")
        return True
    if isinstance(event, ReplanOccurred):
        typer.echo(f"Plan regenerated (replan {event.replan_count})")
        return True
    if isinstance(event, ValidationStarted):
        typer.echo(f"Validation started ({len(event.check_ids)} checks)")
        return True
    if isinstance(event, ValidationFinished):
        typer.echo(
            f"Validation {event.validation_status.value.lower()} "
            f"({event.duration_ms if event.duration_ms is not None else 'unobserved'} ms)"
        )
        return True
    if isinstance(event, RepairStarted):
        typer.echo(f"Repair attempt {event.repair_count} started")
        return True
    if isinstance(event, ChangedFileRecorded):
        typer.echo(f"Changed file: {event.relative_path}")
        return True
    if isinstance(event, ObservabilityWarning):
        typer.echo(
            f"Observability warning [{event.code}]: {event.sink.value.lower()} "
            f"{event.operation.value.lower()} failed; fallback={event.fallback.value.lower()}",
            err=True,
        )
        return True
    if isinstance(event, RunInterrupted):
        typer.echo(event.message)
        return True
    if isinstance(event, ErrorOccurred):
        typer.echo(f"Error [{event.code}]: {event.message}", err=True)
        return False
    return True
