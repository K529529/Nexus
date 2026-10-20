from __future__ import annotations

import os
from pathlib import Path

import pytest

from nexus.core.context import instructions
from nexus.core.types import ExecutionContext
from nexus.tools.execution import execute
from nexus.tools.patch import apply_patch
from tests.conftest import Recorder, python_command


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
async def test_complete_file_sections_missing_begin_apply_once(
    execution: ExecutionContext, newline: str
) -> None:
    (execution.workspace / "existing").write_bytes(b"old\n")
    patch = newline.join(
        [
            "*** Update File: existing",
            "@@",
            "-old",
            "+new",
            "*** Add File: new",
            "+content",
            "*** End Patch",
            "",
        ]
    )
    result = await apply_patch({"patch": patch}, execution, Recorder())
    assert result.ok and result.data["framing_normalized"]
    assert result.data["changed_files"] == 2
    assert (execution.workspace / "existing").read_bytes() == b"new\n"
    assert (execution.workspace / "new").read_bytes() == b"content\n"


@pytest.mark.parametrize(
    "patch",
    [
        "*** Add File: new\n+partial",
        "*** Begin Patch\n*** Add File: new\n+partial",
        "```\n*** Add File: new\n+body\n*** End Patch\n```",
        "prose\n*** Add File: new\n+body\n*** End Patch",
        "*** Add File: new\n+body\n*** End Patch\n*** End Patch",
        "*** Add File: new\n+body\n*** Begin Patch\n*** End Patch",
        "*** Add File: ../escape\n+body\n*** End Patch",
        "*** Add File: new\n+body\n*** Add File: new\n+other\n*** End Patch",
        "*** Update File: existing\n@@\n-missing\n+new\n*** End Patch",
    ],
)
async def test_normalization_never_weakens_validation(
    execution: ExecutionContext, patch: str
) -> None:
    path = execution.workspace / "existing"
    path.write_bytes(b"old\n")
    result = await apply_patch({"patch": patch}, execution, Recorder())
    assert not result.ok and result.data["changed_files"] == 0
    assert path.read_bytes() == b"old\n"
    assert not (execution.workspace / "new").exists()
    assert not (execution.workspace.parent / "escape").exists()


async def test_conflict_feedback_gives_local_repair_context(execution: ExecutionContext) -> None:
    path = execution.workspace / "a"
    path.write_text("current\n", encoding="utf-8")
    result = await apply_patch(
        {"patch": "*** Update File: a\n@@\n-stale\n+new\n*** End Patch"}, execution, Recorder()
    )
    assert result.error_code == "patch_conflict"
    assert result.data["failed_file"] == "a" and result.data["failed_hunk"] == 1
    assert "stale" in result.data["detail"] and "do not retry" in result.data["detail"]
    assert path.read_text(encoding="utf-8") == "current\n"


def test_shell_guidance_uses_selected_shell_not_host_os(tmp_path: Path) -> None:
    classic = instructions(tmp_path, r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
    assert "&& and || are unsupported" in classic
    assert "Write the script directly" in classic and "$LASTEXITCODE" in classic
    assert "Get-ChildItem -Recurse -File | Select-String" in classic
    modern = instructions(tmp_path, "pwsh")
    assert "PowerShell syntax" in modern and "&& and || are unsupported" not in modern
    linux = instructions(tmp_path, "/bin/sh", display=("Linux", "/workspace"))
    assert "Shell semantics:" not in linux and "$LASTEXITCODE" not in linux


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell regression")
async def test_direct_powershell_variables_search_and_native_exit(
    execution: ExecutionContext,
) -> None:
    execution.shell = "powershell.exe"
    (execution.workspace / "nested").mkdir()
    (execution.workspace / "nested/a.txt").write_text("needle", encoding="utf-8")
    result = await execute(
        {
            "command": (
                "$files = Get-ChildItem -Recurse -File; "
                "$files | Select-String -Pattern needle | ForEach-Object { $_.Line }"
            )
        },
        execution,
        Recorder(),
    )
    assert result.ok and "needle" in result.data["stdout"]
    failure = await execute(
        {
            "command": (
                python_command("import sys; sys.exit(7)")
                + "; $testExit = $LASTEXITCODE; Write-Output 'report'; exit $testExit"
            )
        },
        execution,
        Recorder(),
    )
    assert not failure.ok and failure.data["exit_code"] == 7
