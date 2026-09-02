"""Bounded, ToolRuntime-only Day 4 repository exploration."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from uuid import uuid4

from nexus.application.tool_runtime import ToolRuntime
from nexus.domain.exploration import (
    ExplorationRequest,
    ExplorationResult,
    RepositoryFileEvidence,
    RepositoryInstruction,
)
from nexus.domain.tooling import ToolInvocation, ToolResult
from nexus.errors import ContextError

_MANIFEST_NAMES = {
    "pyproject.toml",
    "package.json",
    "pom.xml",
    "cargo.toml",
    "go.mod",
    "requirements.txt",
}
_TERM_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_.\-/]{2,}")


class SelectiveRepositoryExplorer:
    def __init__(self, tool_runtime: ToolRuntime) -> None:
        self._tool_runtime = tool_runtime

    async def explore(self, request: ExplorationRequest) -> ExplorationResult:
        results: list[ToolResult] = []

        async def invoke(name: str, arguments: dict[str, object]) -> ToolResult | None:
            if len(results) >= 24:
                return None
            result = await self._tool_runtime.execute(
                ToolInvocation(
                    str(uuid4()),
                    name,
                    arguments,
                    request.run_id,
                    request.session_id,
                )
            )
            results.append(result)
            return result

        initial_status = await invoke("git_status", {})
        listing = await invoke("list_files", {"path": ".", "recursive": False})
        if (
            initial_status is None
            or listing is None
            or not initial_status.success
            or not listing.success
        ):
            raise ContextError(
                "Nexus could not establish repository exploration evidence.",
                code="REPOSITORY_EXPLORATION_FAILED",
                retryable=True,
            )
        top_level = tuple(_string_items(listing, "paths"))
        instructions: list[RepositoryInstruction] = []
        manifests: list[RepositoryFileEvidence] = []

        if "AGENTS.md" in top_level:
            root_instruction = await invoke("read_file", {"path": "AGENTS.md", "max_lines": 400})
            if root_instruction is not None and root_instruction.success:
                instructions.append(_instruction(root_instruction, "AGENTS.md", ".", 0))

        manifest_paths = [
            path
            for path in top_level
            if _is_manifest(path)
        ]
        config_search = await invoke("search_files", {"pattern": ".nexus/config.toml"})
        if config_search is not None and config_search.success:
            manifest_paths.extend(_string_items(config_search, "paths"))
        for path in _unique(manifest_paths)[:8]:
            result = await invoke("read_file", {"path": path, "max_lines": 120})
            if result is not None and result.success:
                manifests.append(
                    RepositoryFileEvidence(
                        path,
                        "manifest",
                        "Root project/configuration manifest.",
                        _bounded_summary(result),
                    )
                )

        relevant: dict[str, RepositoryFileEvidence] = {}
        terms = _task_terms(request.task)
        for term in terms:
            if len(results) >= 22 or len(relevant) >= 12:
                break
            pattern = term if "/" in term else f"*{term}*"
            found = await invoke("search_files", {"pattern": pattern})
            if found is not None and found.success:
                for path in _string_items(found, "paths"):
                    if path not in relevant and _is_candidate(path):
                        relevant[path] = RepositoryFileEvidence(
                            path,
                            "task_relevant",
                            f"Filename matched task token {term!r}.",
                            f"Task-relevant file: {path}",
                        )
                        if len(relevant) >= 12:
                            break
            if len(results) >= 22 or len(relevant) >= 12:
                break
            matched = await invoke(
                "lexical_search",
                {"pattern": term, "path": ".", "case_sensitive": False},
            )
            if matched is not None and matched.success:
                for item in _dict_items(matched, "matches"):
                    matched_path = item.get("path")
                    if (
                        isinstance(matched_path, str)
                        and matched_path not in relevant
                        and _is_candidate(matched_path)
                    ):
                        relevant[matched_path] = RepositoryFileEvidence(
                            matched_path,
                            "task_relevant",
                            f"Content matched task token {term!r}.",
                            f"Observed lexical match in {matched_path}.",
                        )
                        if len(relevant) >= 12:
                            break

        nested_candidates = _ancestor_instruction_paths(tuple(relevant))
        known = {item.path for item in instructions}
        for path in nested_candidates:
            if len(results) >= 24 or path in known:
                continue
            result = await invoke("read_file", {"path": path, "max_lines": 400})
            if result is not None and result.success:
                scope = str(PurePosixPath(path).parent)
                content = _content(result)
                if _weakens_nexus_safety(content):
                    raise ContextError(
                        "A nested repository instruction conflicts with Nexus safety.",
                        code="REPOSITORY_INSTRUCTION_CONFLICT",
                    )
                instructions.append(
                    _instruction(result, path, scope, len(PurePosixPath(scope).parts))
                )
                known.add(path)

        return ExplorationResult(
            tuple(sorted(instructions, key=lambda item: (item.depth, item.path))),
            tuple(manifests[:8]),
            top_level,
            tuple(list(relevant.values())[:12]),
            initial_status,
            tuple(results),
            len(results) >= 24 or _truncated(listing),
        )


def _instruction(
    result: ToolResult,
    path: str,
    scope: str,
    depth: int,
) -> RepositoryInstruction:
    return RepositoryInstruction(path, scope, depth, _content(result), _truncated(result))


def _content(result: ToolResult) -> str:
    value = None if result.output is None else result.output.get("content")
    return value if isinstance(value, str) else ""


def _bounded_summary(result: ToolResult) -> str:
    compact = " ".join(_content(result).split())[:512]
    return compact or "Manifest inspected; no textual summary was retained."


def _string_items(result: ToolResult, key: str) -> list[str]:
    value = None if result.output is None else result.output.get(key)
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _dict_items(result: ToolResult, key: str) -> list[dict[str, object]]:
    value = None if result.output is None else result.output.get(key)
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _truncated(result: ToolResult) -> bool:
    return bool(result.output and result.output.get("truncated") is True)


def _task_terms(task: str) -> tuple[str, ...]:
    ignored = {"the", "and", "for", "with", "from", "into", "this", "that"}
    values: list[str] = []
    for match in _TERM_PATTERN.findall(task):
        value = match.strip("./")
        if value.casefold() in ignored or value in values:
            continue
        values.append(value)
        if len(values) == 8:
            break
    return tuple(values)


def _is_manifest(path: str) -> bool:
    name = PurePosixPath(path).name.casefold()
    return name in _MANIFEST_NAMES or name.startswith("readme")


def _is_candidate(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return ".git" not in parts and not _is_manifest(path) and path != "AGENTS.md"


def _ancestor_instruction_paths(paths: tuple[str, ...]) -> tuple[str, ...]:
    values: set[str] = set()
    for path in paths:
        parent = PurePosixPath(path).parent
        while str(parent) not in {".", ""}:
            values.add(str(parent / "AGENTS.md"))
            parent = parent.parent
    return tuple(sorted(values, key=lambda value: (len(PurePosixPath(value).parts), value)))


def _weakens_nexus_safety(content: str) -> bool:
    normalized = " ".join(content.casefold().split())
    forbidden = (
        "ignore frozen",
        "bypass security",
        "disable approval",
        "allow dangerous",
        "ignore nexus safety",
    )
    return any(value in normalized for value in forbidden)


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))
