"""Bounded Day 4 context seed construction."""

from __future__ import annotations

from pathlib import PurePosixPath
from uuid import uuid4

from nexus.application.tool_runtime import ToolRuntime
from nexus.domain.exploration import (
    ContextBuildRequest,
    RepositoryInstruction,
    SelectedFileContext,
    WorkingContext,
)
from nexus.domain.tooling import ToolInvocation
from nexus.errors import ContextError


class BoundedContextBuilder:
    def __init__(self, tool_runtime: ToolRuntime) -> None:
        self._tool_runtime = tool_runtime

    async def build(self, request: ContextBuildRequest) -> WorkingContext:
        selected: list[SelectedFileContext] = []
        retained = 0
        truncated = request.exploration.truncated
        for evidence in request.exploration.relevant_files[:8]:
            result = await self._tool_runtime.execute(
                ToolInvocation(
                    str(uuid4()),
                    "read_file",
                    {"path": evidence.path, "start_line": 1, "max_lines": 400},
                    request.run_id,
                    request.session_id,
                )
            )
            if not result.success or result.output is None:
                truncated = True
                continue
            content = result.output.get("content")
            if not isinstance(content, str):
                truncated = True
                continue
            remaining = 48_000 - retained
            if remaining <= 0:
                truncated = True
                break
            bounded = content[:remaining]
            retained += len(bounded)
            item_truncated = result.output.get("truncated") is True or len(bounded) < len(content)
            truncated = truncated or item_truncated
            selected.append(
                SelectedFileContext(
                    evidence.path,
                    bounded,
                    _applicable_instructions(
                        evidence.path,
                        request.exploration.instructions,
                    ),
                    evidence.discovery_reason,
                    item_truncated,
                )
            )
        if request.exploration.relevant_files and not selected:
            raise ContextError(
                "Nexus could not build context from selected repository evidence.",
                code="CONTEXT_BUILD_FAILED",
                retryable=True,
            )
        return WorkingContext(
            request.task,
            request.exploration.instructions,
            request.exploration.manifests,
            request.exploration.top_level_paths,
            tuple(selected),
            truncated,
        )


def _applicable_instructions(
    path: str,
    instructions: tuple[RepositoryInstruction, ...],
) -> tuple[str, ...]:
    target = PurePosixPath(path)
    applicable = [
        instruction
        for instruction in instructions
        if instruction.scope_path == "."
        or PurePosixPath(instruction.scope_path) == target.parent
        or PurePosixPath(instruction.scope_path) in target.parents
    ]
    return tuple(item.path for item in sorted(applicable, key=lambda item: (item.depth, item.path)))
