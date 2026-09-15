"""Day 4 validation planning and execution ports."""

from typing import Protocol

from nexus.domain.exploration import ExplorationResult, WorkingContext
from nexus.domain.planning import ApprovedPlanEvidence, ChangedFile, Plan
from nexus.domain.validation import ValidationPlan, ValidationResult


class ValidationPlanner(Protocol):
    async def plan(
        self,
        *,
        task: str,
        plan: Plan,
        exploration: ExplorationResult,
        context: WorkingContext,
        changed_files: tuple[ChangedFile, ...],
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
        changed: bool,
    ) -> ValidationResult: ...
