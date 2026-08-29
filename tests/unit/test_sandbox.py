from __future__ import annotations

import sys
from pathlib import Path
from typing import cast

import pytest
from pytest import MonkeyPatch

from nexus.domain.tooling import JsonObject, PolicyDecision, RiskLevel, SandboxRequest
from nexus.infrastructure.sandbox import LocalProcessSandbox
from nexus.security.command_policy import DefaultCommandPolicy
from nexus.security.executables import TrustedExecutables
from nexus.security.workspace import WorkspaceGuard


class AlwaysSafePolicy:
    def classify(self, *, operation: str, arguments: JsonObject) -> RiskLevel:
        del operation, arguments
        return RiskLevel.SAFE


def _sandbox(
    workspace: Path,
    *,
    permissive_for_mechanics: bool,
) -> tuple[LocalProcessSandbox, TrustedExecutables]:
    guard = WorkspaceGuard(workspace)
    executables = TrustedExecutables.resolve(guard.root)
    policy = AlwaysSafePolicy() if permissive_for_mechanics else DefaultCommandPolicy(executables)
    return LocalProcessSandbox(guard, policy, executables), executables


@pytest.mark.asyncio
async def test_safe_command_captures_structured_success(tmp_path: Path) -> None:
    sandbox, executables = _sandbox(tmp_path, permissive_for_mechanics=False)
    result = await sandbox.execute(
        SandboxRequest("shell", [executables.python, "--version"], ".", 10.0)
    )

    assert result.policy_decision is PolicyDecision.ALLOWED
    assert result.exit_code == 0
    assert not result.timed_out
    assert "Python" in result.stdout + result.stderr
    assert result.error is None


@pytest.mark.asyncio
async def test_command_failure_captures_stdout_stderr_and_exit_code(tmp_path: Path) -> None:
    sandbox, executables = _sandbox(tmp_path, permissive_for_mechanics=True)
    result = await sandbox.execute(
        SandboxRequest(
            "shell",
            [
                executables.python,
                "-c",
                "import sys; print('out'); print('err', file=sys.stderr); sys.exit(7)",
            ],
            ".",
            10.0,
        )
    )

    assert result.exit_code == 7
    assert result.stdout.strip() == "out"
    assert result.stderr.strip() == "err"
    assert result.error is not None and result.error.code == "COMMAND_EXIT_NONZERO"


@pytest.mark.asyncio
async def test_timeout_terminates_process_and_returns_structured_failure(tmp_path: Path) -> None:
    sandbox, executables = _sandbox(tmp_path, permissive_for_mechanics=True)
    result = await sandbox.execute(
        SandboxRequest(
            "shell",
            [executables.python, "-c", "import time; time.sleep(30)"],
            ".",
            0.05,
        )
    )

    assert result.timed_out
    assert result.exit_code is None
    assert result.error is not None and result.error.code == "SANDBOX_TIMEOUT"


@pytest.mark.asyncio
async def test_environment_is_allowlisted_and_workspace_escape_is_denied(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("NEXUS_TEST_SECRET", "must-not-leak")
    sandbox, executables = _sandbox(tmp_path, permissive_for_mechanics=True)
    environment_result = await sandbox.execute(
        SandboxRequest(
            "shell",
            [
                executables.python,
                "-c",
                "import os; print(os.environ.get('NEXUS_TEST_SECRET', 'absent'))",
            ],
            ".",
            10.0,
        )
    )
    escaped = await sandbox.execute(
        SandboxRequest("shell", [executables.python, "--version"], "..", 10.0)
    )

    assert environment_result.stdout.strip() == "absent"
    assert escaped.policy_decision is PolicyDecision.DENIED
    assert escaped.risk_level is RiskLevel.DANGEROUS
    assert escaped.error is not None and escaped.error.code == "WORKSPACE_PATH_DENIED"


@pytest.mark.asyncio
async def test_operation_name_spoofing_is_denied_before_process(tmp_path: Path) -> None:
    sandbox, executables = _sandbox(tmp_path, permissive_for_mechanics=False)
    if executables.git is None:
        pytest.skip("Git is unavailable")
    result = await sandbox.execute(
        SandboxRequest("git_status", [executables.git, "push"], ".", 10.0)
    )

    assert result.policy_decision is PolicyDecision.DENIED
    assert result.risk_level is RiskLevel.DANGEROUS
    assert result.exit_code is None


@pytest.mark.asyncio
async def test_dangerous_shell_and_git_write_cannot_create_fixture_state(
    tmp_path: Path,
) -> None:
    sandbox, executables = _sandbox(tmp_path, permissive_for_mechanics=False)
    marker = tmp_path / "must-not-exist.txt"
    shell_result = await sandbox.execute(
        SandboxRequest(
            "shell",
            [
                executables.python,
                "-c",
                f"from pathlib import Path; Path({str(marker)!r}).write_text('unsafe')",
            ],
            ".",
            10.0,
        )
    )

    assert shell_result.policy_decision is PolicyDecision.DENIED
    assert not marker.exists()

    if executables.git is not None:
        git_result = await sandbox.execute(
            SandboxRequest("shell", [executables.git, "init"], ".", 10.0)
        )
        assert git_result.policy_decision is PolicyDecision.DENIED
        assert not (tmp_path / ".git").exists()


@pytest.mark.asyncio
async def test_each_process_stream_is_bounded_while_reading(tmp_path: Path) -> None:
    sandbox, executables = _sandbox(tmp_path, permissive_for_mechanics=True)
    result = await sandbox.execute(
        SandboxRequest(
            "shell",
            [
                executables.python,
                "-c",
                "import sys; sys.stdout.write('o' * 1500000); "
                "sys.stderr.write('e' * 1500000)",
            ],
            ".",
            10.0,
        )
    )

    assert len(result.stdout.encode("utf-8")) == 1_048_576
    assert len(result.stderr.encode("utf-8")) == 1_048_576
    assert result.output_truncated


def test_test_uses_current_interpreter() -> None:
    assert Path(sys.executable).exists()


def test_sandbox_request_rejects_non_string_argv_before_normalization() -> None:
    with pytest.raises(ValueError, match="non-empty strings"):
        SandboxRequest(
            "shell",
            ["python", cast(str, 1)],
            ".",
            10.0,
        )
