from __future__ import annotations

import os
import shutil
from pathlib import Path
from uuid import uuid4

import pytest

from nexus.domain.tooling import (
    JsonObject,
    PolicyDecision,
    RiskLevel,
    SandboxRequest,
    ToolInvocation,
)
from nexus.infrastructure.sandbox import LocalProcessSandbox
from nexus.security.executables import TrustedExecutables
from nexus.security.workspace import WorkspaceGuard
from nexus.tools.native import LexicalSearchTool, ListFilesTool, ReadFileTool, SearchFilesTool


def _invocation(tool_name: str, arguments: dict[str, object]) -> ToolInvocation:
    return ToolInvocation(
        str(uuid4()),
        tool_name,
        arguments,
        str(uuid4()),
        str(uuid4()),
    )


@pytest.mark.asyncio
async def test_native_file_tools_list_search_read_and_lexical(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "alpha.py").write_bytes(b"first\nneedle here\n")
    (tmp_path / "README.md").write_bytes(b"hello\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "secret").write_text("hidden", encoding="utf-8")
    guard = WorkspaceGuard(tmp_path)

    listed = await ListFilesTool(guard).execute(_invocation("list_files", {}))
    assert listed.success
    assert listed.output == {
        "paths": ["README.md", "src/alpha.py"],
        "truncated": False,
    }

    searched = await SearchFilesTool(guard).execute(
        _invocation("search_files", {"pattern": "*.py"})
    )
    assert searched.output == {"paths": ["src/alpha.py"], "truncated": False}

    read = await ReadFileTool(guard).execute(
        _invocation("read_file", {"path": "src/alpha.py", "start_line": 2})
    )
    assert read.success
    assert read.output is not None and read.output["content"] == "needle here\n"

    lexical = await LexicalSearchTool(guard).execute(
        _invocation("lexical_search", {"pattern": "needle"})
    )
    assert lexical.success
    assert lexical.output is not None
    assert lexical.output["matches"] == [
        {"path": "src/alpha.py", "line": 2, "column": 1, "text": "needle here"}
    ]

    hidden = await ListFilesTool(guard).execute(
        _invocation("list_files", {"path": ".git"})
    )
    assert hidden.output == {"paths": [], "truncated": False}


@pytest.mark.asyncio
async def test_path_traversal_and_absolute_escape_escalate_to_dangerous(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("do not read", encoding="utf-8")
    tool = ReadFileTool(WorkspaceGuard(workspace))

    for path in ("../outside.txt", str(outside.resolve())):
        result = await tool.execute(_invocation("read_file", {"path": path}))
        assert not result.success
        assert result.risk_level is RiskLevel.DANGEROUS
        assert result.policy_decision is PolicyDecision.DENIED
        assert result.error is not None and result.error.code == "WORKSPACE_PATH_DENIED"


@pytest.mark.asyncio
async def test_lexical_search_orders_matches_by_path_then_line(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "match.txt").write_text("needle\n", encoding="utf-8")
    (tmp_path / "z.txt").write_text("needle\n", encoding="utf-8")

    result = await LexicalSearchTool(WorkspaceGuard(tmp_path)).execute(
        _invocation("lexical_search", {"pattern": "needle"})
    )

    assert result.output is not None
    matches = result.output["matches"]
    assert isinstance(matches, list)
    paths: list[object] = []
    for match in matches:
        assert isinstance(match, dict)
        paths.append(match["path"])
    assert paths == ["a/match.txt", "z.txt"]


@pytest.mark.asyncio
async def test_symlink_or_junction_escape_is_denied_before_read(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("do not read", encoding="utf-8")
    link = workspace / "outside-link"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            raise
        command = shutil.which("cmd")
        assert command is not None
        guard = WorkspaceGuard(workspace)
        executables = TrustedExecutables.resolve(guard.root)
        sandbox = LocalProcessSandbox(guard, _AlwaysSafePolicy(), executables)
        setup = await sandbox.execute(
            SandboxRequest(
                "shell",
                [command, "/d", "/c", "mklink", "/J", str(link), str(outside)],
                ".",
                10.0,
            )
        )
        assert setup.error is None, setup

    result = await ReadFileTool(WorkspaceGuard(workspace)).execute(
        _invocation("read_file", {"path": "outside-link/secret.txt"})
    )
    assert not result.success
    assert result.risk_level is RiskLevel.DANGEROUS
    assert result.error is not None and result.error.code == "WORKSPACE_PATH_DENIED"


class _AlwaysSafePolicy:
    def classify(self, *, operation: str, arguments: JsonObject) -> RiskLevel:
        del operation, arguments
        return RiskLevel.SAFE
