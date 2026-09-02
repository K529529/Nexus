from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import pytest

from nexus.config.models import RuntimeConfig
from nexus.domain.model import ModelChunk, ModelMessage, ModelResponse
from nexus.domain.planning import PlanApprovalResumeInput, TerminalStatus
from nexus.domain.runtime_events import (
    ApprovalRequested,
    FinalResult,
    PlanCreated,
    RunInterrupted,
    ValidationFinished,
)
from nexus.domain.tooling import ApprovalDecision
from nexus.infrastructure.bootstrap import bootstrap_application


class CodingLoopGateway:
    def __init__(self) -> None:
        plan = {
            "rationale_summary": "Update the target and run its focused test.",
            "steps": [
                {
                    "description": "Update alpha value",
                    "tool_name": "apply_patch",
                    "target_paths": ["alpha.py"],
                    "command_argv": None,
                    "command_cwd": None,
                },
                {
                    "description": "TEST: run focused alpha test",
                    "tool_name": "shell",
                    "target_paths": [],
                    "command_argv": ["pytest", "-q", "test_alpha.py"],
                    "command_cwd": ".",
                },
            ],
        }
        edit = {
            "kind": "TOOL_ACTION",
            "summary": "Apply the approved alpha change.",
            "action": {
                "tool_name": "apply_patch",
                "arguments": {
                    "path": "alpha.py",
                    "patch": (
                        "--- a/alpha.py\n+++ b/alpha.py\n"
                        "@@ -1 +1 @@\n-VALUE = 1\n+VALUE = 2\n"
                    ),
                },
            },
        }
        ready = {
            "kind": "TASK_READY",
            "summary": "The approved edit is ready for validation.",
            "action": None,
        }
        self._responses = [json.dumps(plan), json.dumps(edit), json.dumps(ready)]

    async def complete(self, messages: Sequence[ModelMessage]) -> ModelResponse:
        del messages
        return ModelResponse(self._responses.pop(0))

    async def stream(
        self,
        messages: Sequence[ModelMessage],
    ) -> AsyncIterator[ModelChunk]:
        del messages
        if False:
            yield ModelChunk("")


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_realistic_day4_coding_loop_interrupts_edits_validates_and_diffs(
    migrated_database_url: str,
    tmp_path: Path,
) -> None:
    git = shutil.which("git")
    if git is None:
        pytest.skip("Git is unavailable for the Day 4 fixture repository.")
    (tmp_path / "alpha.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "test_alpha.py").write_text(
        "from alpha import VALUE\n\ndef test_value():\n    assert VALUE == 2\n",
        encoding="utf-8",
    )
    _git(git, tmp_path, "init")
    _git(git, tmp_path, "config", "core.autocrlf", "false")
    _git(git, tmp_path, "add", "alpha.py", "test_alpha.py")
    _git(
        git,
        tmp_path,
        "-c",
        "user.name=Nexus Test",
        "-c",
        "user.email=nexus@example.invalid",
        "commit",
        "-m",
        "fixture",
    )
    config = RuntimeConfig(
        database_url=migrated_database_url,
        model_name="fixture-model",
    )

    async with bootstrap_application(
        config,
        model_gateway=CodingLoopGateway(),
        workspace_path=tmp_path,
    ) as application:
        interrupted_events = [
            event
            async for event in application.runtime.run(
                "Update alpha.py VALUE so the focused test passes"
            )
        ]
        interrupted = interrupted_events[-1]
        assert isinstance(interrupted, RunInterrupted)
        assert interrupted.session_id is not None
        assert any(isinstance(event, PlanCreated) for event in interrupted_events)
        assert any(isinstance(event, ApprovalRequested) for event in interrupted_events)

        resumed_events = [
            event
            async for event in application.runtime.resume(
                interrupted.session_id,
                resume_input=PlanApprovalResumeInput(
                    ApprovalDecision.APPROVED,
                    "Fixture scope approved.",
                ),
            )
        ]

    assert (tmp_path / "alpha.py").read_text(encoding="utf-8") == "VALUE = 2\n"
    assert any(isinstance(event, ValidationFinished) for event in resumed_events)
    final = resumed_events[-1]
    assert isinstance(final, FinalResult)
    assert final.terminal_status is TerminalStatus.SUCCEEDED
    assert final.validation_result is not None
    assert final.validation_result.status.value == "PASS"
    assert final.diff is not None and "+VALUE = 2" in final.diff
    assert final.includes_preexisting_changes is False


def _git(executable: str, cwd: Path, *arguments: str) -> None:
    subprocess.run(
        [executable, *arguments],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
