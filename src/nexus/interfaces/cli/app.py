"""Thin CLI adapter for the approved Day 1 command surface."""

from __future__ import annotations

import asyncio
from typing import Annotated

import typer

from nexus.config import load_runtime_config
from nexus.config.models import RuntimeConfig
from nexus.errors import NexusError
from nexus.infrastructure.bootstrap import bootstrap_application
from nexus.interfaces.cli.renderer import render_event

app = typer.Typer(
    name="nexus",
    help="Transparent coding-agent runtime (Day 1 runtime skeleton).",
    add_completion=False,
    invoke_without_command=True,
    no_args_is_help=False,
)


@app.callback()
def root(context: typer.Context) -> None:
    """Show help successfully when no Day 1 subcommand is supplied."""

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
    succeeded = True
    async with bootstrap_application(config) as application:
        async for event in application.runtime.run(task):
            succeeded = render_event(event) and succeeded
    return succeeded


def main() -> None:
    """Installed console-script entry point."""

    app(prog_name="nexus")


if __name__ == "__main__":
    main()
