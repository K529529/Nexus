from __future__ import annotations

from collections.abc import AsyncIterator
from typing import cast
from uuid import uuid4

import pytest
from pytest import CaptureFixture, MonkeyPatch

from nexus.application.runtime import NexusRuntime
from nexus.domain.planning import PlanApprovalResumeInput, PlanKind
from nexus.domain.runtime_events import (
    ApprovalActorCategory,
    ApprovalRequested,
    ApprovalResolved,
    ApprovalSubject,
    ExecutionPhase,
    FinalResult,
    PhaseStarted,
    PlanCreated,
    RunInterrupted,
    RuntimeEvent,
    TaskStarted,
    ValidationFinished,
    ValidationStarted,
)
from nexus.domain.tooling import ApprovalDecision, RiskLevel
from nexus.domain.validation import ValidationCheckKind, ValidationConfidence, ValidationStatus
from nexus.interfaces.cli.app import _collect_plan_decision, _consume_with_plan_approval


class ApprovalRuntime:
    def __init__(
        self, run_id: str, session_id: str, plan_id: str, approval_id: str
    ) -> None:
        self.run_id = run_id
        self.session_id = session_id
        self.plan_id = plan_id
        self.approval_id = approval_id
        self.resume_input: PlanApprovalResumeInput | None = None

    async def resume(
        self,
        session_id: str,
        *,
        resume_input: PlanApprovalResumeInput | None = None,
    ) -> AsyncIterator[RuntimeEvent]:
        assert session_id == self.session_id
        self.resume_input = resume_input
        yield TaskStarted(run_id=self.run_id, session_id=self.session_id, task="edit alpha")
        yield ApprovalResolved(
            run_id=self.run_id,
            session_id=self.session_id,
            approval_id=self.approval_id,
            subject=ApprovalSubject.PLAN,
            invocation_id=None,
            plan_id=self.plan_id,
            plan_version=1,
            decision=ApprovalDecision.APPROVED,
            actor_category=ApprovalActorCategory.USER,
        )
        yield PhaseStarted(
            run_id=self.run_id,
            session_id=self.session_id,
            phase=ExecutionPhase.AGENT,
        )
        yield PhaseStarted(
            run_id=self.run_id,
            session_id=self.session_id,
            phase=ExecutionPhase.VALIDATION,
        )
        yield ValidationStarted(
            run_id=self.run_id,
            session_id=self.session_id,
            check_ids=("check-1",),
            check_kinds=(ValidationCheckKind.TEST,),
        )
        yield ValidationFinished(
            run_id=self.run_id,
            session_id=self.session_id,
            validation_status=ValidationStatus.PASS,
            confidence=ValidationConfidence.HIGH,
            executed_check_count=1,
            repair_count=0,
            duration_ms=1234,
        )
        yield FinalResult(
            run_id=self.run_id,
            session_id=self.session_id,
            content="completed",
        )


@pytest.mark.asyncio
async def test_cli_renders_human_scope_before_approval_and_resumes_same_command(
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    run_id = str(uuid4())
    session_id = str(uuid4())
    plan_id = str(uuid4())
    approval_id = str(uuid4())
    runtime = ApprovalRuntime(run_id, session_id, plan_id, approval_id)

    async def initial() -> AsyncIterator[RuntimeEvent]:
        yield TaskStarted(run_id=run_id, session_id=session_id, task="edit alpha")
        yield PhaseStarted(
            run_id=run_id,
            session_id=session_id,
            phase=ExecutionPhase.PLANNING,
        )
        yield PlanCreated(
            run_id=run_id,
            session_id=session_id,
            plan_id=plan_id,
            plan_version=1,
            plan_kind=PlanKind.INITIAL,
            step_summaries=(
                "1. Edit | WRITE apply_patch alpha.py",
                "2. Test | VALIDATE argv=['pytest', '-q'] cwd=.",
            ),
            replan_reason=None,
        )
        yield ApprovalRequested(
            run_id=run_id,
            session_id=session_id,
            approval_id=approval_id,
            invocation_id=None,
            operation="approve_plan",
            risk_level=RiskLevel.WRITE,
            resource_or_command_summary="approve_plan:digest",
            subject=ApprovalSubject.PLAN,
            plan_id=plan_id,
            plan_version=1,
        )
        yield RunInterrupted(run_id=run_id, session_id=session_id)

    prompts: list[tuple[str, bool]] = []

    def confirm(prompt: str, *, default: bool) -> bool:
        prompts.append((prompt, default))
        return True

    monkeypatch.setattr("typer.confirm", confirm)
    succeeded = await _consume_with_plan_approval(
        initial(),
        cast(NexusRuntime, runtime),
    )
    output = capsys.readouterr().out

    assert succeeded
    assert prompts == [("Approve this plan?", True)]
    assert "  1. Edit (alpha.py)" in output
    assert "  2. Test (pytest -q)" in output
    assert "Approved" in output
    assert "Working" in output
    assert "Validating" in output
    assert "Validation passed" in output
    assert output.count("Analyzing repository") == 1
    assert "Task started" not in output
    assert "Run interrupted" not in output
    assert "Plan approval required" not in output
    assert "scope digest" not in output
    assert "approve_plan:digest" not in output
    assert plan_id not in output
    assert "v1" not in output
    assert "WRITE apply_patch" not in output
    assert "VALIDATE argv" not in output
    assert "Validation started (" not in output
    assert "1234" not in output
    assert "APPROVED" not in output
    assert runtime.resume_input is not None
    assert runtime.resume_input.decision is ApprovalDecision.APPROVED


def test_plan_decline_maps_to_denied_without_enum_prompt(monkeypatch: MonkeyPatch) -> None:
    prompts: list[str] = []

    def decline(prompt: str, *, default: bool) -> bool:
        assert default
        prompts.append(prompt)
        return False

    monkeypatch.setattr("typer.confirm", decline)
    decision = _collect_plan_decision()
    assert prompts == ["Approve this plan?"]
    assert decision.decision is ApprovalDecision.DENIED
