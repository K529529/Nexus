from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from pytest import MonkeyPatch

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
from nexus.tools import native
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
async def test_native_search_prunes_generated_directories_before_reading(
    tmp_path: Path, monkeypatch: MonkeyPatch,
) -> None:
    source = tmp_path / "src"
    source.mkdir()
    (source / "valid.py").write_text("needle\n", encoding="utf-8")
    excluded = (
        ".git", "node_modules", ".venv", "venv", "__pycache__", "dist",
        "build", "target", "coverage", "htmlcov", ".pytest_cache",
        ".mypy_cache", ".ruff_cache", ".uv-cache", ".pytest-tmp",
        ".pytest-tmp-day8-langsmith", ".tmp-docker-config",
    )
    for name in excluded:
        directory = tmp_path / name
        directory.mkdir()
        (directory / "hidden.py").write_text("needle\n", encoding="utf-8")
    nested = source / "node_modules"
    nested.mkdir()
    (nested / "hidden.py").write_text("needle\n", encoding="utf-8")

    visited: list[Path] = []
    read: list[Path] = []
    original_walk = os.walk
    original_read = native._read_text_file

    def tracked_walk(
        root: Path, *, followlinks: bool,
    ) -> Iterator[tuple[str, list[str], list[str]]]:
        for current, directories, files in original_walk(root, followlinks=followlinks):
            visited.append(Path(current))
            yield current, directories, files

    def tracked_read(path: Path) -> str:
        read.append(path)
        return original_read(path)

    monkeypatch.setattr(os, "walk", tracked_walk)
    monkeypatch.setattr(native, "_read_text_file", tracked_read)
    guard = WorkspaceGuard(tmp_path)

    searched = await SearchFilesTool(guard).execute(
        _invocation("search_files", {"pattern": "*.py"})
    )
    assert searched.output == {"paths": ["src/valid.py"], "truncated": False}

    listed = await ListFilesTool(guard).execute(_invocation("list_files", {}))
    assert listed.output == {"paths": ["src/valid.py"], "truncated": False}

    lexical = await LexicalSearchTool(guard).execute(
        _invocation("lexical_search", {"pattern": "needle"})
    )
    assert lexical.output == {
        "matches": [{"path": "src/valid.py", "line": 1, "column": 1, "text": "needle"}],
        "truncated": False,
    }
    assert [path.relative_to(tmp_path).as_posix() for path in read] == ["src/valid.py"]
    assert visited
    assert all(
        not any(part in excluded for part in path.relative_to(tmp_path).parts)
        for path in visited
    )

    for tool_name, tool in (
        ("search_files", SearchFilesTool(guard)),
        ("lexical_search", LexicalSearchTool(guard)),
    ):
        paths = (".venv", ".venv/hidden.py") if tool_name == "lexical_search" else (".venv",)
        for path in paths:
            result = await tool.execute(
                _invocation(tool_name, {"path": path, "pattern": "needle"})
            )
            assert result.success
            assert result.output == {
                "paths" if tool_name == "search_files" else "matches": [],
                "truncated": False,
            }


@pytest.mark.asyncio
async def test_path_traversal_and_absolute_escape_escalate_to_dangerous(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("do not read", encoding="utf-8")
    guard = WorkspaceGuard(workspace)

    for path in ("../outside.txt", str(outside.resolve())):
        for tool_name, tool in (
            ("read_file", ReadFileTool(guard)),
            ("search_files", SearchFilesTool(guard)),
            ("lexical_search", LexicalSearchTool(guard)),
        ):
            arguments: dict[str, object] = {"path": path}
            if tool_name != "read_file":
                arguments["pattern"] = "*"
            result = await tool.execute(_invocation(tool_name, arguments))
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
