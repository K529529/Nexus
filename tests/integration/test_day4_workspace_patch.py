from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest

from nexus.application.diff_service import FinalDiffCollector
from nexus.application.tool_runtime import ToolRuntime
from nexus.domain.tooling import ToolInvocation, ToolResult
from nexus.infrastructure.sandbox import LocalProcessSandbox
from nexus.security.command_policy import DefaultCommandPolicy
from nexus.security.executables import TrustedExecutables
from nexus.security.workspace import WorkspaceGuard
from nexus.tools.native import GitDiffTool, GitStatusTool, ReadFileTool


def _git(root: Path, *args: str, input: str | None = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        input=None if input is None else input.encode("utf-8"),
        capture_output=True,
    )
    assert result.returncode == 0, (
        result.stderr.decode("utf-8", errors="replace") + "\n" + (input or "")
    )
    return result.stdout.decode("utf-8")


def _repository(root: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("Git is unavailable")
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "core.autocrlf", "false")
    (root / "alpha.py").write_bytes(b"VALUE = 1\n")
    (root / "deleted.txt").write_bytes(b"remove me\n")
    (root / "staged.txt").write_bytes(b"before\n")
    _git(root, "add", ".")
    _git(
        root,
        "-c",
        "user.name=Nexus Test",
        "-c",
        "user.email=nexus@example.invalid",
        "commit",
        "-m",
        "fixture",
    )


class NativeReadRuntime:
    def __init__(self, root: Path) -> None:
        guard = WorkspaceGuard(root)
        executables = TrustedExecutables.resolve(guard.root)
        sandbox = LocalProcessSandbox(guard, DefaultCommandPolicy(executables), executables)
        self.status_tool = GitStatusTool(sandbox, executables)
        self.diff_tool = GitDiffTool(sandbox, executables)
        self.read_tool = ReadFileTool(guard)

    async def execute(self, invocation: ToolInvocation, **kwargs: object) -> ToolResult:
        del kwargs
        if invocation.tool_name == "git_status":
            return await self.status_tool.execute(invocation)
        if invocation.tool_name == "git_diff":
            return await self.diff_tool.execute(invocation)
        return await self.read_tool.execute(invocation)


def _collector(root: Path) -> FinalDiffCollector:
    return FinalDiffCollector(cast(ToolRuntime, NativeReadRuntime(root)))


@pytest.mark.asyncio
async def test_workspace_patch_covers_head_state_and_applies_to_clean_checkout(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "source"
    replay = tmp_path / "replay"
    _repository(repository)
    (repository / "alpha.py").write_bytes(b"VALUE = 2\n")
    (repository / "deleted.txt").unlink()
    (repository / "staged.txt").write_bytes(b"after\n")
    _git(repository, "add", "staged.txt")
    (repository / "nested").mkdir()
    (repository / "nested" / "new.txt").write_bytes(b"hello\nworld")
    (repository / "nested" / "space name.txt").write_bytes(b"text\n")
    (repository / "nested" / "empty.txt").write_bytes(b"")

    evidence = await _collector(repository).collect(run_id=str(uuid4()), session_id=str(uuid4()))
    assert evidence.error_code is None
    assert evidence.diff is not None
    for marker in (
        "+VALUE = 2", "deleted.txt", "+after", "nested/new.txt", "space name.txt", "empty.txt"
    ):
        assert marker in evidence.diff
    subprocess.run(  # noqa: ASYNC221
        ["git", "-c", "core.autocrlf=false", "clone", "-q", str(repository), str(replay)],
        check=True,
    )
    _git(replay, "config", "core.autocrlf", "false")
    _git(replay, "apply", "--check", "-", input=evidence.diff)
    _git(replay, "apply", "-", input=evidence.diff)
    for path in (
        "alpha.py", "staged.txt", "nested/new.txt", "nested/space name.txt", "nested/empty.txt"
    ):
        assert (replay / path).read_bytes() == (repository / path).read_bytes()
    assert not (replay / "deleted.txt").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["binary", "oversize"])
async def test_untracked_unsupported_file_makes_whole_patch_unavailable(
    tmp_path: Path, kind: str
) -> None:
    repository = tmp_path / "source"
    _repository(repository)
    (repository / "alpha.py").write_bytes(b"VALUE = 2\n")
    content = b"\x00binary" if kind == "binary" else b"x" * 1_048_577
    (repository / "extra.bin").write_bytes(content)
    evidence = await _collector(repository).collect(run_id=str(uuid4()), session_id=str(uuid4()))
    assert evidence.diff is None
    assert evidence.error_code == "DIFF_UNAVAILABLE"
