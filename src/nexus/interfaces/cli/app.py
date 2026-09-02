"""Thin CLI adapter for the approved Day 1 command surface."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Annotated

import typer

from nexus.application.runtime import NexusRuntime
from nexus.config import load_runtime_config
from nexus.config.models import RuntimeConfig
from nexus.domain.persistence import SessionSummary
from nexus.domain.planning import PlanApprovalResumeInput
from nexus.domain.runtime_events import (
    ApprovalRequested,
    ApprovalSubject,
    RunInterrupted,
    RuntimeEvent,
)
from nexus.domain.tooling import ApprovalDecision
from nexus.errors import NexusError
from nexus.infrastructure.bootstrap import bootstrap_application
from nexus.interfaces.cli.renderer import render_event

app = typer.Typer(
    name="nexus",
    help="Transparent coding-agent runtime (through Day 2 persistence).",
    add_completion=False,
    invoke_without_command=True,
    no_args_is_help=False,
)
session_app = typer.Typer(help="List and resume durable Nexus sessions.")
app.add_typer(session_app, name="session")


@app.callback()
def root(context: typer.Context) -> None:
    """Show help successfully when no subcommand is supplied."""

    if context.invoked_subcommand is None:
        typer.echo(context.get_help())


@app.command()
def chat(
    task: Annotated[
        str,
        typer.Argument(
            help="Non-empty task to send to the configured model.",
            metavar="TASK",
        ),
    ],
    model: Annotated[
        str | None,
        typer.Option("--model", help="Override the configured model name."),
    ] = None,
    base_url: Annotated[
        str | None,
        typer.Option("--base-url", help="Override the OpenAI-compatible base URL."),
    ] = None,
) -> None:
    """Run the minimal model-backed Day 1 lifecycle."""

    try:
        config = load_runtime_config(cli_model=model, cli_base_url=base_url)
        succeeded = asyncio.run(_run_chat(config, task))
    except NexusError as exc:
        typer.echo(f"Error [{exc.code}]: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    if not succeeded:
        raise typer.Exit(code=1)


async def _run_chat(config: RuntimeConfig, task: str) -> bool:
    async with bootstrap_application(config) as application:
        return await _consume_with_plan_approval(
            application.runtime.run(task),
            application.runtime,
        )


@session_app.command("list")
def list_sessions() -> None:
    """List sessions owned by the current canonical repository."""

    try:
        config = load_runtime_config()
        summaries = asyncio.run(_list_sessions(config))
    except NexusError as exc:
        typer.echo(f"Error [{exc.code}]: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    _render_session_summaries(summaries)


@session_app.command()
def resume(
    session_id: Annotated[
        str,
        typer.Argument(help="Session UUID to resume.", metavar="SESSION_ID"),
    ],
) -> None:
    """Resume the latest interrupted run for a current-repository session."""

    try:
        config = load_runtime_config()
        succeeded = asyncio.run(_run_resume(config, session_id))
    except NexusError as exc:
        typer.echo(f"Error [{exc.code}]: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    if not succeeded:
        raise typer.Exit(code=1)


async def _list_sessions(config: RuntimeConfig) -> list[SessionSummary]:
    async with bootstrap_application(config) as application:
        return await application.session_service.list_sessions()


async def _run_resume(config: RuntimeConfig, session_id: str) -> bool:
    async with bootstrap_application(config) as application:
        return await _consume_with_plan_approval(
            application.runtime.resume(session_id),
            application.runtime,
            requested_session_id=session_id,
        )


async def _consume_with_plan_approval(
    events: AsyncIterator[RuntimeEvent],
    runtime: NexusRuntime,
    *,
    requested_session_id: str | None = None,
) -> bool:
    succeeded = True
    current_events = events
    session_id = requested_session_id
    while True:
        pending: ApprovalRequested | None = None
        interrupted = False
        async for event in current_events:
            succeeded = render_event(event) and succeeded
            if event.session_id is not None:
                session_id = event.session_id
            if (
                isinstance(event, ApprovalRequested)
                and event.subject is ApprovalSubject.PLAN
            ):
                pending = event
            interrupted = interrupted or isinstance(event, RunInterrupted)
        if pending is None:
            return succeeded
        if not interrupted or session_id is None:
            raise NexusError(
                "Plan approval was not paired with a durable interrupt.",
                code="GRAPH_INVALID_STATE",
            )
        resume_input = _collect_plan_decision()
        current_events = runtime.resume(
            session_id,
            resume_input=resume_input,
        )


def _collect_plan_decision() -> PlanApprovalResumeInput:
    while True:
        value = typer.prompt("Plan decision (APPROVED/DENIED)").strip().upper()
        try:
            decision = ApprovalDecision(value)
            return PlanApprovalResumeInput(decision, None)
        except ValueError:
            typer.echo("Enter APPROVED or DENIED.", err=True)


def _render_session_summaries(summaries: list[SessionSummary]) -> None:
    typer.echo("session_id\tlast_active_at\tresumable")
    for summary in summaries:
        typer.echo(
            f"{summary.session_id}\t{summary.last_active_at.isoformat()}\t"
            f"{'yes' if summary.resumable else 'no'}"
        )


def main() -> None:
    """Installed console-script entry point."""

    app(prog_name="nexus")


if __name__ == "__main__":
    main()
