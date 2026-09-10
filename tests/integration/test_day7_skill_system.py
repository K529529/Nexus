from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest

from nexus.application.execution_ledger import RuntimeEventBuffer, ToolExecutionLedger
from nexus.application.planning import ModelPlanner
from nexus.config.models import ApprovalMode
from nexus.context.budget import Day5ModelInputBudgetGuard
from nexus.context.integration import ManagedContextBuilder
from nexus.domain.agent_decision import Observation
from nexus.domain.agent_state import AgentState
from nexus.domain.context import ContextRequest
from nexus.domain.exploration import ContextBuildRequest, ExplorationResult, WorkingContext
from nexus.domain.model import ModelChunk, ModelMessage, ModelResponse
from nexus.domain.persistence import SessionTurn
from nexus.domain.planning import Plan, PlanKind
from nexus.domain.ports.context import ContextManager
from nexus.domain.ports.planning import PlanningRequest
from nexus.domain.ports.repository_context import ContextBuilder, RepositoryExplorer
from nexus.domain.ports.validation import ValidationPlanner, ValidationRunner
from nexus.domain.runtime_events import ContextBuilt, RuntimeStatus
from nexus.domain.skills import SkillMetadata
from nexus.infrastructure.graph.day4_runtime import Day4LangGraphRuntime
from nexus.skills import DefaultSkillRegistry, FileSkillLoader, ModelSkillSelector

_EXPECTED = {
    "debug Python failure": ("debug-python", "REPOSITORY OVERRIDE DEBUG WORKFLOW"),
    "write focused tests": ("write-tests", "Test observable contracts"),
    "review repository changes": ("review-repository", "Lead with concrete findings"),
}


class SelectionGateway:
    def __init__(self) -> None:
        self.messages: list[Sequence[ModelMessage]] = []

    async def complete(self, messages: Sequence[ModelMessage]) -> ModelResponse:
        self.messages.append(tuple(messages))
        payload = json.loads(messages[1].content)
        skill_id, _ = _EXPECTED[payload["task"]]
        return ModelResponse(
            json.dumps(
                {
                    "selected_skill_ids": [skill_id],
                    "selection_reason_summary": f"{skill_id} matches the explicit task.",
                }
            )
        )

    async def stream(self, messages: Sequence[ModelMessage]) -> AsyncIterator[ModelChunk]:
        del messages
        if False:
            yield ModelChunk("")


class PlanningGateway:
    def __init__(self, expected_marker: str) -> None:
        self.expected_marker = expected_marker
        self.messages: Sequence[ModelMessage] = ()

    async def complete(self, messages: Sequence[ModelMessage]) -> ModelResponse:
        self.messages = tuple(messages)
        assert self.expected_marker in messages[1].content
        return ModelResponse(
            json.dumps(
                {
                    "rationale_summary": "Apply the selected Skill guidance.",
                    "steps": [
                        {
                            "description": f"Follow {self.expected_marker}",
                            "tool_name": None,
                            "target_paths": [],
                            "command_argv": None,
                            "command_cwd": None,
                        }
                    ],
                }
            )
        )

    async def stream(self, messages: Sequence[ModelMessage]) -> AsyncIterator[ModelChunk]:
        del messages
        if False:
            yield ModelChunk("")


class RecordingLoader(FileSkillLoader):
    def __init__(self, repository_root: Path, user_root: Path) -> None:
        super().__init__(repository_root, user_root)
        self.body_calls: list[tuple[str, str]] = []

    async def load_body(self, metadata: SkillMetadata) -> str:
        self.body_calls.append((metadata.skill_id, metadata.source.value))
        return await super().load_body(metadata)


class Manager:
    async def build(self, request: ContextRequest) -> WorkingContext:
        return WorkingContext(request.task, (), (), (), (), False)

    async def prepare_agent_context(
        self,
        *,
        working_context: WorkingContext,
        plan: Plan | None,
        observations: Sequence[Observation],
        conversation_turns: Sequence[SessionTurn],
    ) -> WorkingContext:
        del plan, observations, conversation_turns
        return working_context


def repository_override(root: Path) -> None:
    folder = root / "debug-python"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        """+++
id = "debug-python"
name = "Repository Debug Python"
description = "Diagnose Python failures with repository-specific evidence."
usage_scenario = "Use for Python debugging in this repository."
priority = 1
version = "0.1.0"
+++

## Execution Principles

REPOSITORY OVERRIDE DEBUG WORKFLOW

## Recommended Tools

Use bounded repository evidence.

## Workflow

Reproduce, diagnose, and validate.

## Constraints

Preserve policy and approved scope.
""",
        encoding="utf-8",
    )


@pytest.mark.parametrize("task", tuple(_EXPECTED))
async def test_demo_skills_select_inject_and_influence_planning(
    tmp_path: Path,
    task: str,
) -> None:
    repository_root = tmp_path / ".nexus" / "skills"
    repository_override(repository_root)
    loader = RecordingLoader(repository_root, tmp_path / "missing-user")
    registry = DefaultSkillRegistry(loader, repository_root, tmp_path / "missing-user")
    selection_gateway = SelectionGateway()
    ledger = ToolExecutionLedger()
    selector = ModelSkillSelector(
        selection_gateway,
        2,
        ledger.begin_model,
        Day5ModelInputBudgetGuard(24000),
    )
    builder = ManagedContextBuilder(
        cast(ContextManager, Manager()),
        repository_id=_repository_id,
        workspace=str(tmp_path),
        skill_registry=registry,
        skill_selector=selector,
    )
    run_id = str(uuid4())
    session_id = str(uuid4())

    context = await builder.build(
        ContextBuildRequest(
            task,
            cast(ExplorationResult, object()),
            run_id,
            session_id,
        )
    )

    skill_id, marker = _EXPECTED[task]
    assert [item.metadata.skill_id for item in context.selected_skills] == [skill_id]
    assert marker in context.selected_skills[0].body
    expected_source = "repository" if skill_id == "debug-python" else "builtin"
    assert loader.body_calls == [(skill_id, expected_source)]
    assert ledger.model_count(run_id) == 1
    selection_prompt = selection_gateway.messages[0][1].content
    assert "REPOSITORY OVERRIDE DEBUG WORKFLOW" not in selection_prompt
    assert "Test observable contracts" not in selection_prompt
    assert "Lead with concrete findings" not in selection_prompt

    planning_gateway = PlanningGateway(marker)
    plan = await ModelPlanner(planning_gateway, ledger=ledger).create_plan(
        _planning_request(task, context, run_id, session_id)
    )
    assert marker in plan.steps[0].description
    assert ledger.model_count(run_id) == 2
    visible = planning_gateway.messages[1].content
    for other_task, (other_id, other_marker) in _EXPECTED.items():
        if other_task != task:
            assert other_id not in [item.metadata.skill_id for item in context.selected_skills]
            assert other_marker not in visible

    event_buffer = RuntimeEventBuffer()
    runtime = _event_runtime(FixedBuilder(context), event_buffer, ledger)
    state = AgentState(
        task,
        [ModelMessage("user", task)],
        run_id,
        session_id,
        RuntimeStatus.STARTED,
        exploration=cast(ExplorationResult, object()),
    )
    await runtime._build_context(state)  # noqa: SLF001
    event = cast(ContextBuilt, event_buffer.drain(run_id)[0])
    assert event.selected_skill_ids == (skill_id,)
    assert event.skill_selection_reason_summary == f"{skill_id} matches the explicit task."
    assert marker not in repr(event)


class FixedBuilder:
    def __init__(self, context: WorkingContext) -> None:
        self.context = context

    async def build(self, request: ContextBuildRequest) -> WorkingContext:
        del request
        return self.context


def _planning_request(
    task: str,
    context: WorkingContext,
    run_id: str,
    session_id: str,
) -> PlanningRequest:
    return PlanningRequest(task, context, PlanKind.INITIAL, None, None, run_id, session_id)


def _event_runtime(
    builder: FixedBuilder,
    events: RuntimeEventBuffer,
    ledger: ToolExecutionLedger,
) -> Day4LangGraphRuntime:
    unused = cast(Any, object())
    return Day4LangGraphRuntime(
        explorer=cast(RepositoryExplorer, unused),
        context_builder=cast(ContextBuilder, builder),
        planner=unused,
        agent=unused,
        tool_runtime=unused,
        validation_planner=cast(ValidationPlanner, unused),
        validation_runner=cast(ValidationRunner, unused),
        plan_approval_service=unused,
        diff_collector=unused,
        event_buffer=events,
        ledger=ledger,
        approval_mode=ApprovalMode.AUTO,
        max_steps=1,
        max_repair_attempts=0,
        max_replans=0,
        context_manager=None,
    )


async def _repository_id() -> str:
    return "repository-id"
