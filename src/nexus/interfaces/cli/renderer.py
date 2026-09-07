"""Render safe RuntimeEvents for a terminal consumer."""

from __future__ import annotations

import typer

from nexus.domain.runtime_events import (
    ApprovalRequested,
    ApprovalSubject,
    ContextBuilt,
    ErrorOccurred,
    FinalResult,
    PlanCreated,
    RunInterrupted,
    RuntimeEvent,
    TaskStarted,
)


def render_event(event: RuntimeEvent) -> bool:
    """Render one event and return whether it represents success/continuation."""

    if isinstance(event, TaskStarted):
        typer.echo("Task started")
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
    if isinstance(event, RunInterrupted):
        typer.echo(event.message)
        return True
    if isinstance(event, ErrorOccurred):
        typer.echo(f"Error [{event.code}]: {event.message}", err=True)
        return False
    return True
