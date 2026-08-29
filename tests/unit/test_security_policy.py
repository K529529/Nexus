from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from nexus.domain.approvals import ApprovalRequest
from nexus.domain.tooling import ApprovalDecision, RiskLevel
from nexus.security.approval_policies import AutoApprovalPolicy, InteractiveApprovalPolicy
from nexus.security.command_policy import DefaultCommandPolicy
from nexus.security.executables import TrustedExecutables
from nexus.security.git_commands import diff_argv, log_argv, status_argv


def _executables(tmp_path: Path) -> TrustedExecutables:
    return TrustedExecutables(
        python=str(tmp_path / "python"),
        uv=str(tmp_path / "uv"),
        git=str(tmp_path / "git"),
        pytest=str(tmp_path / "pytest"),
        ruff=str(tmp_path / "ruff"),
        mypy=str(tmp_path / "mypy"),
    )


def test_command_policy_matrix_and_unknown_fail_closed(tmp_path: Path) -> None:
    executables = _executables(tmp_path)
    policy = DefaultCommandPolicy(executables)

    assert policy.classify(operation="read_file", arguments={"path": "a.py"}) is RiskLevel.SAFE
    assert (
        policy.classify(operation="shell", arguments={"argv": ["uv", "run", "pytest"]})
        is RiskLevel.WRITE
    )
    assert (
        policy.classify(operation="shell", arguments={"argv": ["unknown-command"]})
        is RiskLevel.DANGEROUS
    )
    assert (
        policy.classify(operation="shell", arguments={"argv": ["git", "push"]})
        is RiskLevel.DANGEROUS
    )


def test_git_operation_identity_must_match_canonical_argv(tmp_path: Path) -> None:
    executables = _executables(tmp_path)
    assert executables.git is not None
    policy = DefaultCommandPolicy(executables)

    assert (
        policy.classify(
            operation="git_status",
            arguments={"argv": status_argv(executables.git)},
        )
        is RiskLevel.SAFE
    )
    assert (
        policy.classify(
            operation="git_diff",
            arguments={"argv": diff_argv(executables.git, staged=True)},
        )
        is RiskLevel.SAFE
    )
    assert (
        policy.classify(
            operation="git_log",
            arguments={"argv": log_argv(executables.git, max_entries=20)},
        )
        is RiskLevel.SAFE
    )
    assert (
        policy.classify(
            operation="git_status",
            arguments={"argv": [executables.git, "push"]},
        )
        is RiskLevel.DANGEROUS
    )
    assert (
        policy.classify(
            operation="shell",
            arguments={"argv": [executables.git, "status"]},
        )
        is RiskLevel.DANGEROUS
    )


def test_forbidden_git_writes_and_shell_operators_are_dangerous(tmp_path: Path) -> None:
    policy = DefaultCommandPolicy(_executables(tmp_path))

    for argv in (
        ["git", "commit", "-m", "x"],
        ["git", "push"],
        ["git", "reset", "--hard"],
        ["python", "-c", "print('x')"],
        ["uv", "--version", "&&", "another-command"],
    ):
        assert policy.classify(operation="shell", arguments={"argv": argv}) is (
            RiskLevel.DANGEROUS
        )


def test_executable_resolution_rejects_workspace_path_shadowing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shadow = tmp_path / ("git.exe" if os.name == "nt" else "git")
    shadow.write_bytes(b"not a trusted executable")
    shadow.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")

    resolved = TrustedExecutables.resolve(tmp_path)

    if resolved.git is not None:
        assert Path(resolved.git).parent != tmp_path
    assert Path(resolved.python).parent != tmp_path


@pytest.mark.asyncio
async def test_interactive_and_auto_approval_policy_semantics() -> None:
    pending = _pending_approval(RiskLevel.WRITE)

    async def approve(
        request: ApprovalRequest,
    ) -> tuple[ApprovalDecision, str | None]:
        assert request == pending
        return ApprovalDecision.APPROVED, "approved"

    interactive = await InteractiveApprovalPolicy(approve).request(pending)
    automatic = await AutoApprovalPolicy().request(pending)

    assert interactive.decision is ApprovalDecision.APPROVED
    assert interactive.actor == "user"
    assert automatic.decision is ApprovalDecision.DENIED
    assert automatic.actor == "auto_policy"


@pytest.mark.asyncio
async def test_interactive_policy_cannot_override_dangerous_hard_deny() -> None:
    called = False

    async def approve(
        request: ApprovalRequest,
    ) -> tuple[ApprovalDecision, str | None]:
        del request
        nonlocal called
        called = True
        return ApprovalDecision.APPROVED, None

    result = await InteractiveApprovalPolicy(approve).request(
        _pending_approval(RiskLevel.DANGEROUS)
    )

    assert result.decision is ApprovalDecision.DENIED
    assert result.actor == "security_policy"
    assert not called


def _pending_approval(risk: RiskLevel) -> ApprovalRequest:
    return ApprovalRequest(
        str(uuid4()),
        str(uuid4()),
        str(uuid4()),
        "shell",
        risk,
        "shell:test",
        ApprovalDecision.PENDING,
        "runtime",
        None,
        datetime.now(UTC),
        None,
    )
