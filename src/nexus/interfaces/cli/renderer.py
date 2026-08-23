"""Render safe RuntimeEvents for a terminal consumer."""

from __future__ import annotations

import typer

from nexus.domain.runtime_events import ErrorOccurred, FinalResult, RuntimeEvent, TaskStarted


def render_event(event: RuntimeEvent) -> bool:
    """Render one event and return whether it represents success/continuation."""

    if isinstance(event, TaskStarted):
        typer.echo("Task started")
        return True
    if isinstance(event, FinalResult):
        typer.echo(event.content)
        return True
    if isinstance(event, ErrorOccurred):
        typer.echo(f"Error [{event.code}]: {event.message}", err=True)
        return False
    return True

