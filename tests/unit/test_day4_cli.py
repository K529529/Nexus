from __future__ import annotations

from collections.abc import AsyncIterator
from typing import cast
from uuid import uuid4

import pytest
from pytest import CaptureFixture, MonkeyPatch

from nexus.application.runtime import NexusRuntime
from nexus.domain.planning import PlanApprovalResumeInput, PlanKind
from nexus.domain.runtime_events import (
    ApprovalRequested,
    ApprovalSubject,
    FinalResult,
    PlanCreated,
    RunInterrupted,
    RuntimeEvent,
)
from nexus.domain.tooling import ApprovalDecision, RiskLevel
from nexus.interfaces.cli.app import _consume_with_plan_approval


class ApprovalRuntime:
    def __init__(self, run_id: str, session_id: str) -> None:
        self.run_id = run_id
        self.session_id = session_id
        self.resume_input: PlanApprovalResumeInput | None = None

    async def resume(
        self,
        session_id: str,
        *,
        resume_input: PlanApprovalResumeInput | None = None,
    ) -> AsyncIterator[RuntimeEvent]:
        assert session_id == self.session_id
        self.resume_input = resume_input
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
    runtime = ApprovalRuntime(run_id, session_id)

    async def initial() -> AsyncIterator[RuntimeEvent]:
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

    monkeypatch.setattr("typer.prompt", lambda prompt: "APPROVED")
    succeeded = await _consume_with_plan_approval(
        initial(),
        cast(NexusRuntime, runtime),
    )
    output = capsys.readouterr().out

    assert succeeded
    assert output.index("WRITE apply_patch alpha.py") < output.index(
        "Plan approval required"
    )
    assert "VALIDATE argv=['pytest', '-q'] cwd=." in output
    assert "scope digest" in output
    assert runtime.resume_input is not None
    assert runtime.resume_input.decision is ApprovalDecision.APPROVED
