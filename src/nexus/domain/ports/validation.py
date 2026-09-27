"""Day 4 validation planning and execution ports."""

from collections.abc import Awaitable, Callable
from typing import Protocol

from nexus.domain.exploration import ExplorationResult, WorkingContext
from nexus.domain.planning import ApprovedPlanEvidence, Plan
from nexus.domain.validation import ValidationPlan, ValidationResult


class ValidationPlanner(Protocol):
    async def plan(
        self,
        *,
        task: str,
        plan: Plan,
        exploration: ExplorationResult,
        context: WorkingContext,
        requires_workspace_change: bool,
        workspace_changed_by_run: bool,
    ) -> ValidationPlan: ...


class ValidationRunner(Protocol):
    async def run(
        self,
        plan: ValidationPlan,
        *,
        run_id: str,
        session_id: str,
        authorization: ApprovedPlanEvidence,
        repair_count: int,
        requires_workspace_change: bool,
        workspace_changed_by_run: bool,
        candidate_has_patch: bool,
        verify_workspace: Callable[[], Awaitable[bool]] | None = None,
    ) -> ValidationResult: ...
