"""Reproducible Day 3 SAFE native Tool demo through the Composition Root."""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from nexus.config import RuntimeConfig
from nexus.domain.runtime_events import RuntimeEvent
from nexus.domain.tooling import ToolInvocation
from nexus.infrastructure.bootstrap import bootstrap_tool_application


def main() -> None:
    arguments = _parser().parse_args()
    asyncio.run(_run(arguments.workspace.resolve(strict=True), arguments.read))


async def _run(workspace: Path, read_path: str) -> None:
    events: list[RuntimeEvent] = []

    async def collect(event: RuntimeEvent) -> None:
        events.append(event)

    run_id = str(uuid4())
    session_id = str(uuid4())
    invocations = [
        _invocation("list_files", {"path": ".", "recursive": False}, run_id, session_id),
        _invocation(
            "search_files",
            {"pattern": "*.py", "path": "src"},
            run_id,
            session_id,
        ),
        _invocation(
            "read_file",
            {"path": read_path, "start_line": 1, "max_lines": 20},
            run_id,
            session_id,
        ),
        _invocation("git_status", {}, run_id, session_id),
    ]
    async with bootstrap_tool_application(
        RuntimeConfig(),
        workspace_path=workspace,
        tool_event_emitter=collect,
    ) as application:
        results = []
        for invocation in invocations:
            results.append(asdict(await application.tool_runtime.execute(invocation)))

    print(
        json.dumps(
            {
                "events": [event.to_dict() for event in events],
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def _invocation(
    tool_name: str,
    arguments: dict[str, object],
    run_id: str,
    session_id: str,
) -> ToolInvocation:
    return ToolInvocation(
        invocation_id=str(uuid4()),
        tool_name=tool_name,
        arguments=arguments,
        run_id=run_id,
        session_id=session_id,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--read", default="README.md", help="Workspace-relative UTF-8 file")
    return parser


if __name__ == "__main__":
    main()
