from __future__ import annotations

from typing import cast
from uuid import uuid4

import pytest

from nexus.application.diff_service import FinalDiffCollector
from nexus.application.tool_runtime import ToolRuntime
from nexus.domain.planning import ApprovedPlanEvidence, ChangedFile, ChangeKind
from nexus.domain.tooling import (
    PolicyDecision,
    RiskLevel,
    ToolInvocation,
    ToolResult,
)


def _success(invocation: ToolInvocation, output: dict[str, object]) -> ToolResult:
    return ToolResult(
        invocation.invocation_id,
        invocation.tool_name,
        True,
        output,
        None,
        RiskLevel.SAFE,
        PolicyDecision.ALLOWED,
        None,
        1,
    )


def _status(run_id: str, session_id: str, stdout: str) -> ToolResult:
    return ToolResult(
        str(uuid4()),
        "git_status",
        True,
        {"stdout": stdout, "truncated": False},
        None,
        RiskLevel.SAFE,
        PolicyDecision.ALLOWED,
        None,
        1,
    )


class DiffRuntime:
    def __init__(
        self,
        *,
        status: str,
        tracked_diff: str,
        files: dict[str, str],
        truncate_diff: bool = False,
    ) -> None:
        self.status = status
        self.tracked_diff = tracked_diff
        self.files = files
        self.truncate_diff = truncate_diff

    async def execute(
        self,
        invocation: ToolInvocation,
        *,
        authorization: ApprovedPlanEvidence | None = None,
    ) -> ToolResult:
        del authorization
        if invocation.tool_name == "git_status":
            return _success(
                invocation,
                {"stdout": self.status, "truncated": False},
            )
        if invocation.tool_name == "git_diff":
            return _success(
                invocation,
                {
                    "stdout": self.tracked_diff,
                    "truncated": self.truncate_diff,
                },
            )
        path = invocation.arguments["path"]
        assert isinstance(path, str)
        content = self.files[path]
        return _success(
            invocation,
            {
                "path": path,
                "content": content,
                "end_line": max(1, len(content.splitlines())),
                "truncated": False,
            },
        )


@pytest.mark.asyncio
async def test_exact_diff_appends_run_created_file_and_ignores_clean_branch_header() -> None:
    run_id = str(uuid4())
    session_id = str(uuid4())
    invocation_id = str(uuid4())
    runtime = DiffRuntime(
        status="## feature/day04\n?? new.txt\n",
        tracked_diff="diff --git a/alpha.py b/alpha.py\n-old\n+new\n",
        files={"new.txt": "hello\nworld"},
    )
    collector = FinalDiffCollector(cast(ToolRuntime, runtime))
    evidence = await collector.collect(
        run_id=run_id,
        session_id=session_id,
        changed_files=(
            ChangedFile(
                "new.txt",
                ChangeKind.ADDED,
                invocation_id,
                invocation_id,
            ),
        ),
        initial_git_status=_status(run_id, session_id, "## feature/day04\n"),
    )

    assert evidence.error_code is None
    assert evidence.includes_preexisting_changes is False
    assert evidence.diff is not None
    assert "diff --git a/alpha.py b/alpha.py" in evidence.diff
    assert "diff --git a/new.txt b/new.txt" in evidence.diff
    assert "+hello\n+world\n\\ No newline at end of file" in evidence.diff


@pytest.mark.asyncio
async def test_diff_truncation_fails_closed_and_dirty_evidence_is_retained() -> None:
    run_id = str(uuid4())
    session_id = str(uuid4())
    invocation_id = str(uuid4())
    collector = FinalDiffCollector(
        cast(
            ToolRuntime,
            DiffRuntime(
                status="## branch\n M alpha.py\n",
                tracked_diff="partial",
                files={},
                truncate_diff=True,
            ),
        )
    )
    evidence = await collector.collect(
        run_id=run_id,
        session_id=session_id,
        changed_files=(
            ChangedFile(
                "alpha.py",
                ChangeKind.MODIFIED,
                invocation_id,
                invocation_id,
            ),
        ),
        initial_git_status=_status(run_id, session_id, "## branch\n M alpha.py\n"),
    )

    assert evidence.diff is None
    assert evidence.error_code == "DIFF_UNAVAILABLE"
    assert evidence.includes_preexisting_changes is True
