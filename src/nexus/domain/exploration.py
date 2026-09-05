"""Day 4 selective exploration and bounded context values."""

from __future__ import annotations

from dataclasses import dataclass

from nexus.domain.tooling import ToolResult


@dataclass(frozen=True, slots=True)
class ExplorationRequest:
    task: str
    run_id: str
    session_id: str


@dataclass(frozen=True, slots=True)
class RepositoryInstruction:
    path: str
    scope_path: str
    depth: int
    content: str
    truncated: bool


@dataclass(frozen=True, slots=True)
class RepositoryFileEvidence:
    path: str
    category: str
    discovery_reason: str
    summary: str


@dataclass(frozen=True, slots=True)
class ExplorationResult:
    instructions: tuple[RepositoryInstruction, ...]
    manifests: tuple[RepositoryFileEvidence, ...]
    top_level_paths: tuple[str, ...]
    relevant_files: tuple[RepositoryFileEvidence, ...]
    initial_git_status: ToolResult
    tool_results: tuple[ToolResult, ...]
    truncated: bool


@dataclass(frozen=True, slots=True)
class SelectedFileContext:
    path: str
    content: str
    applicable_instruction_paths: tuple[str, ...]
    discovery_reason: str
    truncated: bool


@dataclass(frozen=True, slots=True)
class WorkingContext:
    task: str
    repository_instructions: tuple[RepositoryInstruction, ...]
    manifest_summaries: tuple[RepositoryFileEvidence, ...]
    top_level_paths: tuple[str, ...]
    selected_files: tuple[SelectedFileContext, ...]
    truncated: bool


@dataclass(frozen=True, slots=True)
class ContextBuildRequest:
    task: str
    exploration: ExplorationResult
    run_id: str
    session_id: str
