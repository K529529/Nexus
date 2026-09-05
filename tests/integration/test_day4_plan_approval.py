from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from nexus.application.plan_approval_service import PlanApprovalService
from nexus.application.session_service import SessionService
from nexus.domain.planning import (
    Plan,
    PlanApprovalResumeInput,
    PlanKind,
    PlanStatus,
    PlanStep,
    PlanStepStatus,
    compute_scope_digest,
    derive_authorization_scope,
)
from nexus.domain.tooling import ApprovalDecision
from nexus.infrastructure.database import DatabaseBootstrap
from nexus.infrastructure.persistence import (
    SqlAlchemyApprovalUnitOfWorkFactory,
    SqlAlchemySessionUnitOfWorkFactory,
)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_plan_approval_persists_typed_decision_and_validates_evidence(
    migrated_database_url: str,
    tmp_path: Path,
) -> None:
    database = DatabaseBootstrap(migrated_database_url)
    run_id = str(uuid4())
    session_service = SessionService(
        SqlAlchemySessionUnitOfWorkFactory(database.session_factory),
        tmp_path,
    )
    run = await session_service.start_run(
        run_id=run_id,
        task="edit alpha",
        session_id=None,
        model_metadata={"model": "fixture"},
    )
    service = PlanApprovalService(
        SqlAlchemyApprovalUnitOfWorkFactory(database.session_factory)
    )
    step = PlanStep(
        str(uuid4()),
        1,
        "Edit alpha",
        "apply_patch",
        ("alpha.py",),
        None,
        None,
        PlanStepStatus.PENDING,
    )
    steps = (step,)
    scope = derive_authorization_scope(steps)
    plan_id = str(uuid4())
    plan = Plan(
        plan_id,
        run.run_id,
        run.session_id,
        1,
        PlanKind.INITIAL,
        PlanStatus.CREATED,
        ApprovalDecision.PENDING,
        steps,
        scope,
        "Bounded edit.",
        None,
        None,
        compute_scope_digest(plan_id, 1, scope),
        datetime.now(UTC),
        None,
    )

    try:
        pending = await service.create_pending(plan)
        approved = await service.persist_user_decision(
            pending.approval_id,
            PlanApprovalResumeInput(ApprovalDecision.APPROVED, "Reviewed scope."),
        )
        evidence = service.evidence(plan, approved)
        persisted = await service.require_approved(evidence)

        assert persisted.approval_id == pending.approval_id
        assert persisted.decision is ApprovalDecision.APPROVED
        assert persisted.actor == "user"
        assert evidence.authorization_scope == plan.authorization_scope
        assert service.activate(plan, persisted).status is PlanStatus.ACTIVE
    finally:
        await database.close()
