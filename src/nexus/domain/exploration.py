"""Day 4 selective exploration and bounded context values."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from nexus.domain.context import ContextCandidate
from nexus.domain.persistence import SessionTurn
from nexus.domain.skills import SelectedSkill, SkillSelectionResult

if TYPE_CHECKING:
    from nexus.domain.agent_decision import Observation

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
    retrieved_candidates: tuple[ContextCandidate, ...] = ()
    compacted_conversation: str | None = None
    semantic_retrieval_used: bool = False
    semantic_retrieval_status: str | None = None
    recent_observations: tuple[Observation, ...] = ()
    recent_conversation_turns: tuple[SessionTurn, ...] = ()
    compacted_observations: str | None = None
    selected_skills: tuple[SelectedSkill, ...] = ()
    skill_selection_result: SkillSelectionResult | None = None


@dataclass(frozen=True, slots=True)
class ContextBuildRequest:
    task: str
    exploration: ExplorationResult
    run_id: str
    session_id: str
