"""Render safe RuntimeEvents for a terminal consumer."""

from __future__ import annotations

import ast
import shlex
import sys
import threading
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
from nexus.domain.tooling import ApprovalDecision
from nexus.domain.validation import ValidationStatus

_PHASE_MESSAGES = {
    ExecutionPhase.REPOSITORY: "Analyzing repository",
    ExecutionPhase.CONTEXT: "Building context",
    ExecutionPhase.PLANNING: "Planning",
    ExecutionPhase.AGENT: "Working",
    ExecutionPhase.VALIDATION: "Validating",
    ExecutionPhase.REPLAN: "Replanning",
    ExecutionPhase.REPAIR: "Preparing repair",
}


class ProgressRenderer:
    """Human progress projection with one bounded heartbeat while events are quiet."""

    def __init__(self, *, resuming: bool = False) -> None:
        self._status = "Starting"
        self._status_since = time.monotonic()
        self._last_phase: ExecutionPhase | None = None
        self._task_announced = False
        self._resuming = resuming
        self._heartbeat: threading.Thread | None = None
        self._heartbeat_stop = threading.Event()
        self._output_lock = threading.RLock()
        self._live_line = False
        self._displayed_elapsed: int | None = None
        self._running = False

    def start(self) -> None:
        with self._output_lock:
            if self._heartbeat is not None:
                return
            self._heartbeat_stop = threading.Event()
            self._running = True
            self._heartbeat = threading.Thread(
                target=self._pulse,
                args=(self._heartbeat_stop,),
                daemon=True,
            )
            self._draw_line()
            self._heartbeat.start()

    async def stop(self) -> None:
        with self._output_lock:
            self._running = False
            heartbeat = self._heartbeat
            self._heartbeat = None
            self._heartbeat_stop.set()
        if heartbeat is not None:
            heartbeat.join()
        with self._output_lock:
            self._clear_line()

    def render(self, event: RuntimeEvent) -> bool:
        with self._output_lock:
            return self._render_locked(event)

    def _render_locked(self, event: RuntimeEvent) -> bool:
        permanent = self._prints_output(event)
        if permanent:
            self._clear_line()
        previous_status = self._status
        if isinstance(event, PhaseStarted):
            self._set_status(_PHASE_MESSAGES[event.phase])
            if event.phase is not self._last_phase and event.phase is not ExecutionPhase.CONTEXT:
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
            self._set_status("Validating")
        elif isinstance(event, TaskStarted):
            if self._task_announced:
                self._set_status("Working")
            else:
                self._set_status("Working" if self._resuming else "Analyzing repository")
                typer.echo(self._status)
                self._task_announced = True
                if not self._resuming:
                    self._last_phase = ExecutionPhase.REPOSITORY
        result = render_event(event)
        if isinstance(event, (FinalResult, ErrorOccurred)):
            self._running = False
            self._heartbeat_stop.set()
        elif permanent or self._status != previous_status:
            self._draw_line()
        return result

    def _prints_output(self, event: RuntimeEvent) -> bool:
        if isinstance(event, TaskStarted):
            return not self._task_announced
        if isinstance(event, PhaseStarted):
            return event.phase is not self._last_phase and event.phase is not ExecutionPhase.CONTEXT
        if isinstance(event, ApprovalResolved):
            return event.subject is ApprovalSubject.PLAN
        return isinstance(
            event,
            (ContextBuilt, FinalResult, PlanCreated, ValidationFinished, ErrorOccurred),
        )

    def _set_status(self, status: str) -> None:
        if status != self._status:
            self._status = status
            self._status_since = time.monotonic()

    def _pulse(self, stop: threading.Event) -> None:
        next_log = time.monotonic() + 15
        while not stop.wait(0.2):
            with self._output_lock:
                if not self._running:
                    return
                elapsed = int(time.monotonic() - self._status_since)
                if sys.stdout.isatty():
                    if elapsed != self._displayed_elapsed:
                        self._draw_line(elapsed)
                elif time.monotonic() >= next_log:
                    typer.echo(f"{self._status}... {elapsed}s")
                    next_log = time.monotonic() + 15

    def _draw_line(self, elapsed: int | None = None) -> None:
        if not self._running or not sys.stdout.isatty():
            return
        seconds = int(time.monotonic() - self._status_since) if elapsed is None else elapsed
        sys.stdout.write(f"\r{self._status}... {seconds}s\x1b[K")
        sys.stdout.flush()
        self._live_line = True
        self._displayed_elapsed = seconds

    def _clear_line(self) -> None:
        if self._live_line:
            sys.stdout.write("\r\x1b[K")
            sys.stdout.flush()
            self._live_line = False
            self._displayed_elapsed = None


def render_event(event: RuntimeEvent) -> bool:
    """Render one event and return whether it represents success/continuation."""

    if isinstance(event, TaskStarted):
        return True
    if isinstance(event, RepositoryExplored):
        return True
    if isinstance(event, ContextBuilt):
        typer.echo("Context ready")
        if event.semantic_retrieval_status:
            code = event.semantic_retrieval_status
            remediation = (
                "Run nexus index --rebuild."
                if code == "INDEX_INCOMPATIBLE"
                else "Run nexus index."
                if code == "INDEX_NOT_FOUND"
                else "Check embedding/database availability and retry."
            )
            typer.echo(f"Using lexical context. {remediation}", err=True)
        return True
    if isinstance(event, FinalResult):
        typer.echo("Done" if event.status.value == "COMPLETED" else "Finished with errors")
        typer.echo(event.content)
        if event.diff:
            typer.echo(event.diff)
        return event.status.value == "COMPLETED"
    if isinstance(event, PlanCreated):
        for summary in event.step_summaries:
            typer.echo(f"  {_human_plan_step(summary)}")
        return True
    if isinstance(event, ApprovalRequested) and event.subject is ApprovalSubject.PLAN:
        return True
    if isinstance(event, ApprovalResolved):
        if event.subject is ApprovalSubject.PLAN:
            typer.echo("Approved" if event.decision is ApprovalDecision.APPROVED else "Declined")
        return True
    if isinstance(event, (AgentStepCompleted, ModelCallFinished, ToolStarted, ToolFinished)):
        return True
    if isinstance(event, ReplanOccurred):
        return True
    if isinstance(event, ValidationStarted):
        return True
    if isinstance(event, ValidationFinished):
        result = {
            ValidationStatus.PASS: "Validation passed",
            ValidationStatus.FAIL: "Validation failed",
            ValidationStatus.UNKNOWN: "Validation inconclusive",
        }
        typer.echo(result[event.validation_status])
        return True
    if isinstance(event, RepairStarted):
        return True
    if isinstance(event, ChangedFileRecorded):
        return True
    if isinstance(event, ObservabilityWarning):
        return True
    if isinstance(event, RunInterrupted):
        return True
    if isinstance(event, ErrorOccurred):
        typer.echo(f"Error [{event.code}]: {event.message}", err=True)
        return False
    return True


def _human_plan_step(summary: str) -> str:
    description, marker, detail = summary.rpartition(" | WRITE ")
    if marker:
        _, separator, path = detail.partition(" ")
        return f"{description} ({path})" if separator and path else description
    description, marker, detail = summary.rpartition(" | VALIDATE argv=")
    if marker:
        arguments, separator, _cwd = detail.rpartition(" cwd=")
        if separator:
            try:
                argv = ast.literal_eval(arguments)
            except (SyntaxError, ValueError, RecursionError):
                return description
            if isinstance(argv, list) and all(isinstance(part, str) for part in argv):
                return f"{description} ({shlex.join(argv)})"
        return description
    return summary
