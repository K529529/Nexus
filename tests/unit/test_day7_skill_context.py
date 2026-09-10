from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest

from nexus.application.execution_ledger import ToolExecutionLedger
from nexus.application.planning import _agent_messages, _planning_messages, _repair_messages
from nexus.application.tool_runtime import ToolRuntime
from nexus.config.models import RuntimeConfig
from nexus.context.chunking import LineWindowChunker
from nexus.context.integration import ManagedContextBuilder
from nexus.context.manager import BoundedContextManager, context_payload, model_input_tokens
from nexus.context.retrieval import ToolRepositoryAccess
from nexus.domain.agent_decision import AgentDecisionRequest, Observation
from nexus.domain.context import ContextBudget, ContextRequest
from nexus.domain.exploration import ContextBuildRequest, ExplorationResult, WorkingContext
from nexus.domain.model import ModelMessage
from nexus.domain.persistence import SessionTurn
from nexus.domain.planning import (
    AuthorizationScope,
    Plan,
    PlanKind,
    PlanStatus,
    PlanStep,
    PlanStepStatus,
    compute_scope_digest,
)
from nexus.domain.ports.context import ContextProvider
from nexus.domain.ports.model_gateway import ModelGateway
from nexus.domain.ports.planning import PlanningRequest, RepairPlanningRequest
from nexus.domain.skills import (
    SelectedSkill,
    SkillLocation,
    SkillMetadata,
    SkillSelectionResult,
    SkillSource,
)
from nexus.domain.tooling import ApprovalDecision
from nexus.domain.validation import ValidationConfidence, ValidationResult, ValidationStatus
from nexus.errors import ContextError
from nexus.infrastructure.bootstrap.composition import _build_day7_context_services


def base_context(task: str = "task") -> WorkingContext:
    return WorkingContext(task, (), (), (), (), False)


def selected(skill_id: str, body_text: str) -> SelectedSkill:
    source = SkillSource.BUILTIN
    metadata = SkillMetadata(
        skill_id,
        skill_id.replace("-", " ").title(),
        f"Description for {skill_id}.",
        f"Use {skill_id} for matching work.",
        source,
        50,
        "1.0.0",
        SkillLocation(source, f"{skill_id}/SKILL.md"),
    )
    return SelectedSkill(metadata, body_text.rstrip() + "\n", "Matches the explicit task.")


def render(context: WorkingContext) -> Sequence[ModelMessage]:
    return (ModelMessage("user", json.dumps(context_payload(context), ensure_ascii=False)),)


def bounded_manager(maximum: int) -> BoundedContextManager:
    return BoundedContextManager(
        cast(ContextProvider, object()),
        ToolRepositoryAccess(cast(ToolRuntime, object())),
        LineWindowChunker(),
        ContextBudget(
            max_retrieved_chunks=12,
            max_exploration_seed_chunks=6,
            max_code_context_tokens=min(maximum, 12000),
            max_recent_observations=8,
        ),
        maximum,
    )


def test_context_payload_contains_only_approved_selected_skill_fields() -> None:
    skill = selected("debug-python", "PRIVATE SELECTED BODY")
    context = replace(
        base_context(),
        selected_skills=(skill,),
        skill_selection_result=SkillSelectionResult(
            ("debug-python",), "Matches the explicit task."
        ),
    )
    payload = context_payload(context)
    assert payload["selected_skills"] == [
        {
            "skill_id": "debug-python",
            "name": "Debug Python",
            "version": "1.0.0",
            "source": "builtin",
            "selected_reason": "Matches the explicit task.",
            "body": "PRIVATE SELECTED BODY\n",
        }
    ]
    serialized = json.dumps(payload)
    assert "usage_scenario" not in serialized
    assert "relative_path" not in serialized
    assert "skill_selection_result" not in serialized


def test_second_selected_skill_is_removed_whole_before_first() -> None:
    first = selected("debug-python", "FIRST-BODY " * 80)
    second = selected("write-tests", "SECOND-BODY " * 80)
    one = replace(base_context(), selected_skills=(first,), truncated=True)
    two = replace(base_context(), selected_skills=(first, second))
    maximum = model_input_tokens(render(one))
    assert model_input_tokens(render(two)) > maximum

    fitted = bounded_manager(maximum).fit_model_input(two, render)

    assert fitted.selected_skills == (first,)
    assert fitted.selected_skills[0].body == first.body
    assert "SECOND-BODY" not in json.dumps(context_payload(fitted))
    assert fitted.truncated


def test_first_selected_skill_overflow_has_skill_specific_error() -> None:
    skill = selected("debug-python", "ATOMIC-BODY " * 200)
    without_skill = base_context("mandatory task")
    maximum = model_input_tokens(render(without_skill))
    with pytest.raises(ContextError) as caught:
        bounded_manager(maximum).fit_model_input(
            replace(without_skill, selected_skills=(skill,)),
            render,
        )
    assert caught.value.code == "SKILL_CONTEXT_BUDGET_EXCEEDED"
    assert skill.body == ("ATOMIC-BODY " * 200).rstrip() + "\n"


def test_no_selection_mandatory_overflow_keeps_day5_error() -> None:
    with pytest.raises(ContextError) as caught:
        bounded_manager(1).fit_model_input(base_context("mandatory task"), render)
    assert caught.value.code == "CONTEXT_BUILD_FAILED"


async def test_context_refresh_preserves_selected_skill_and_selection_result() -> None:
    skill = selected("debug-python", "PRESERVED BODY")
    selection = SkillSelectionResult(("debug-python",), "Matches the explicit task.")
    original = replace(
        base_context(),
        selected_skills=(skill,),
        skill_selection_result=selection,
    )
    refreshed = await bounded_manager(24000).prepare_agent_context(
        working_context=original,
        plan=None,
        observations=(),
        conversation_turns=(),
    )
    assert refreshed.selected_skills == (skill,)
    assert refreshed.skill_selection_result is selection


def test_composition_root_shares_gateway_ledger_and_model_input_ceiling(
    tmp_path: Path,
) -> None:
    gateway = cast(ModelGateway, object())
    ledger = ToolExecutionLedger()
    maximum = 777
    services = _build_day7_context_services(
        config=RuntimeConfig(
            max_model_input_tokens=maximum,
            max_code_context_tokens=700,
        ),
        gateway=gateway,
        ledger=ledger,
        workspace=tmp_path,
        provider=cast(ContextProvider, object()),
        access=ToolRepositoryAccess(cast(ToolRuntime, object())),
        chunker=LineWindowChunker(),
    )

    assert services.context_manager._maximum == maximum
    assert services.budget_guard._maximum == maximum
    assert services.skill_selector._budget_guard is services.budget_guard
    assert services.skill_selector._model_gateway is gateway
    services.skill_selector._begin_model("shared-run")
    assert ledger.model_count("shared-run") == 1


def plan(run_id: str, session_id: str) -> Plan:
    plan_id = str(uuid4())
    scope = AuthorizationScope((), ())
    return Plan(
        plan_id,
        run_id,
        session_id,
        1,
        PlanKind.INITIAL,
        PlanStatus.CREATED,
        ApprovalDecision.PENDING,
        (
            PlanStep(
                str(uuid4()),
                1,
                "Use the selected guidance",
                None,
                (),
                None,
                None,
                PlanStepStatus.PENDING,
            ),
        ),
        scope,
        "Selected guidance applies.",
        None,
        None,
        compute_scope_digest(plan_id, 1, scope),
        datetime.now(UTC),
        None,
    )


def test_initial_replan_repair_and_agent_messages_reuse_same_selected_body() -> None:
    marker = "SAME SELECTED BODY"
    context = replace(base_context(), selected_skills=(selected("debug-python", marker),))
    run_id = str(uuid4())
    session_id = str(uuid4())
    current_plan = plan(run_id, session_id)
    validation = ValidationResult(
        (),
        (),
        ValidationStatus.FAIL,
        ValidationConfidence.LOW,
        False,
        0,
        "Validation failed without a repairable check.",
    )
    message_sets = (
        _planning_messages(
            PlanningRequest(
                context.task,
                context,
                PlanKind.INITIAL,
                None,
                None,
                run_id,
                session_id,
            )
        ),
        _planning_messages(
            PlanningRequest(
                context.task,
                context,
                PlanKind.REPLAN,
                current_plan,
                "Material scope evidence changed.",
                run_id,
                session_id,
            )
        ),
        _repair_messages(
            RepairPlanningRequest(context.task, context, current_plan, validation, 1)
        ),
        _agent_messages(AgentDecisionRequest(context.task, context, current_plan, ())),
    )
    assert all(marker in messages[1].content for messages in message_sets)


class Manager:
    def __init__(self) -> None:
        self.request: ContextRequest | None = None
        self.prepared: WorkingContext | None = None

    async def build(self, request: ContextRequest) -> WorkingContext:
        self.request = request
        return base_context(request.task)

    async def prepare_agent_context(
        self,
        *,
        working_context: WorkingContext,
        plan: Plan | None,
        observations: Sequence[Observation],
        conversation_turns: Sequence[SessionTurn],
    ) -> WorkingContext:
        assert plan is None and observations == () and conversation_turns == ()
        self.prepared = working_context
        return working_context


class Registry:
    def __init__(self, skill: SelectedSkill) -> None:
        self.skill = skill
        self.loaded: SkillSelectionResult | None = None

    async def scan_metadata(self) -> tuple[SkillMetadata, ...]:
        return (self.skill.metadata,)

    def resolve_selected(
        self,
        selection: SkillSelectionResult,
    ) -> tuple[SkillMetadata, ...]:
        return (self.skill.metadata,)

    async def load_selected(
        self,
        selection: SkillSelectionResult,
    ) -> tuple[SelectedSkill, ...]:
        self.loaded = selection
        return (self.skill,)


class Selector:
    def __init__(self) -> None:
        self.arguments: tuple[str, str, Sequence[SkillMetadata]] | None = None

    async def select(
        self,
        *,
        run_id: str,
        task: str,
        available_skills: Sequence[SkillMetadata],
    ) -> SkillSelectionResult:
        self.arguments = (run_id, task, available_skills)
        return SkillSelectionResult(("debug-python",), "Matches the explicit task.")


async def test_managed_builder_preserves_established_run_and_explicit_task() -> None:
    skill = selected("debug-python", "SELECTED BODY")
    manager = Manager()
    registry = Registry(skill)
    selector = Selector()
    builder = ManagedContextBuilder(
        manager,
        repository_id=lambda: _repository_id(),
        workspace="workspace",
        skill_registry=registry,
        skill_selector=selector,
    )
    task = "  explicit task remains unchanged  "
    result = await builder.build(
        ContextBuildRequest(
            task,
            cast(ExplorationResult, object()),
            "established-run-id",
            "session-id",
        )
    )

    assert selector.arguments == ("established-run-id", task, (skill.metadata,))
    assert manager.request is not None
    assert manager.request.run_id == "established-run-id"
    assert manager.request.task == task
    assert result.selected_skills == (skill,)
    assert result.skill_selection_result == registry.loaded


async def _repository_id() -> str:
    return "repository-id"
