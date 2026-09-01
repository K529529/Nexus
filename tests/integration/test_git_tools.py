from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from nexus.domain.tooling import JsonObject, RiskLevel, SandboxRequest, ToolInvocation
from nexus.infrastructure.sandbox import LocalProcessSandbox
from nexus.security.command_policy import DefaultCommandPolicy
from nexus.security.executables import TrustedExecutables
from nexus.security.workspace import WorkspaceGuard
from nexus.tools.native import GitDiffTool, GitLogTool, GitStatusTool


class FixtureSetupPolicy:
    def classify(self, *, operation: str, arguments: JsonObject) -> RiskLevel:
        del operation, arguments
        return RiskLevel.WRITE


@pytest.mark.asyncio
async def test_read_only_git_tools_use_isolated_fixture_repository(tmp_path: Path) -> None:
    guard = WorkspaceGuard(tmp_path)
    executables = TrustedExecutables.resolve(guard.root)
    if executables.git is None:
        pytest.skip("Git is unavailable")
    setup_sandbox = LocalProcessSandbox(guard, FixtureSetupPolicy(), executables)
    await _run(setup_sandbox, [executables.git, "init"])
    (tmp_path / "tracked.txt").write_text("first\n", encoding="utf-8")
    await _run(setup_sandbox, [executables.git, "add", "tracked.txt"])
    await _run(
        setup_sandbox,
        [
            executables.git,
            "-c",
            "user.email=nexus@example.invalid",
            "-c",
            "user.name=Nexus Test",
            "commit",
            "-m",
            "fixture",
        ],
    )
    (tmp_path / "tracked.txt").write_text("changed\n", encoding="utf-8")

    policy = DefaultCommandPolicy(executables)
    sandbox = LocalProcessSandbox(guard, policy, executables)
    status = await GitStatusTool(sandbox, executables).execute(_invocation("git_status", {}))
    diff = await GitDiffTool(sandbox, executables).execute(_invocation("git_diff", {}))
    log = await GitLogTool(sandbox, executables).execute(
        _invocation("git_log", {"max_entries": 1})
    )

    assert status.success
    assert status.output is not None
    assert set(status.output) == {"exit_code", "stdout", "stderr", "truncated"}
    assert status.output is not None and "tracked.txt" in str(status.output["stdout"])
    assert diff.success
    assert diff.output is not None and "-first" in str(diff.output["stdout"])
    assert "+changed" in str(diff.output["stdout"])
    assert log.success
    assert log.output is not None and "fixture" in str(log.output["stdout"])


async def _run(sandbox: LocalProcessSandbox, argv: list[str]) -> None:
    result = await sandbox.execute(SandboxRequest("shell", argv, ".", 10.0))
    assert result.error is None, result


def _invocation(tool_name: str, arguments: dict[str, object]) -> ToolInvocation:
    return ToolInvocation(
        str(uuid4()),
        tool_name,
        arguments,
        str(uuid4()),
        str(uuid4()),
    )
