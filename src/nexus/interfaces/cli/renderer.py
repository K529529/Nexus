"""Render safe RuntimeEvents for a terminal consumer."""

from __future__ import annotations

import asyncio
import sys
import time

import typer

from nexus.domain.runtime_events import (
    AgentStepCompleted,
    ApprovalRequested,
    ApprovalResolved,
    ApprovalSubject,
    ChangedFileRecorded,
    ContextBuilt,
    ErrorOccurred,
    ExecutionPhase,
    FinalResult,
    ModelCallFinished,
    ModelCallStarted,
    ObservabilityWarning,
    PhaseStarted,
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

_PHASE_MESSAGES = {
    ExecutionPhase.REPOSITORY: "Analyzing repository",
    ExecutionPhase.CONTEXT: "Building context",
    ExecutionPhase.PLANNING: "Planning",
    ExecutionPhase.AGENT: "Working",
    ExecutionPhase.VALIDATION: "Validating changes",
    ExecutionPhase.REPLAN: "Replanning",
    ExecutionPhase.REPAIR: "Preparing repair",
}


class ProgressRenderer:
    """Human progress projection with one bounded heartbeat while events are quiet."""

    def __init__(self) -> None:
        self._status = "Starting"
        self._status_since = time.monotonic()
        self._last_phase: ExecutionPhase | None = None
        self._heartbeat: asyncio.Task[None] | None = None
        self._live_line = False

    def start(self) -> None:
        if self._heartbeat is None:
            self._heartbeat = asyncio.create_task(self._pulse())

    async def stop(self) -> None:
        task = self._heartbeat
        self._heartbeat = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._clear_line()

    def render(self, event: RuntimeEvent) -> bool:
        self._clear_line()
        if isinstance(event, PhaseStarted):
            self._set_status(_PHASE_MESSAGES[event.phase])
            if event.phase is not self._last_phase:
                typer.echo(self._status)
                self._last_phase = event.phase
        elif isinstance(event, ModelCallStarted):
            self._set_status("Thinking")
        elif isinstance(event, ToolStarted):
            self._set_status(
                "Editing files"
                if event.tool_name in {"apply_patch", "write_file"}
                else "Reading relevant files"
            )
        elif isinstance(event, ValidationStarted):
            self._set_status("Validating changes")
        elif isinstance(event, TaskStarted):
            self._set_status("Analyzing repository")
        return render_event(event)

    def _set_status(self, status: str) -> None:
        if status != self._status:
            self._status = status
            self._status_since = time.monotonic()

    async def _pulse(self) -> None:
        next_log = time.monotonic() + 15
        while True:
            await asyncio.sleep(1)
            elapsed = int(time.monotonic() - self._status_since)
            if sys.stdout.isatty():
                sys.stdout.write(f"\r{self._status}... {elapsed}s\x1b[K")
                sys.stdout.flush()
                self._live_line = True
            elif time.monotonic() >= next_log:
                typer.echo(f"{self._status}... {elapsed}s")
                next_log = time.monotonic() + 15

    def _clear_line(self) -> None:
        if self._live_line:
            sys.stdout.write("\r\x1b[K")
            sys.stdout.flush()
            self._live_line = False


def render_event(event: RuntimeEvent) -> bool:
    """Render one event and return whether it represents success/continuation."""

    if isinstance(event, TaskStarted):
        typer.echo("Task started")
        return True
    if isinstance(event, RepositoryExplored):
        typer.echo("Repository analyzed")
        return True
    if isinstance(event, ContextBuilt):
        typer.echo("Context ready")
        if event.semantic_retrieval_status:
            code = event.semantic_retrieval_status
            remediation = ("Run nexus index --rebuild." if code == "INDEX_INCOMPATIBLE"
                           else "Run nexus index." if code == "INDEX_NOT_FOUND"
                           else "Check embedding/database availability and retry.")
            typer.echo(f"Context warning [{code}]: using lexical context. {remediation}", err=True)
        return True
    if isinstance(event, FinalResult):
        typer.echo("Done" if event.status.value == "COMPLETED" else "Finished with errors")
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
    if isinstance(event, (AgentStepCompleted, ModelCallFinished, ToolStarted, ToolFinished)):
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
        return True
    if isinstance(event, RunInterrupted):
        typer.echo(event.message)
        return True
    if isinstance(event, ErrorOccurred):
        typer.echo(f"Error [{event.code}]: {event.message}", err=True)
        return False
    return True
