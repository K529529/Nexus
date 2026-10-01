from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from nexus.core.types import ExecutionContext, ToolCancelled
from nexus.tools.execution import execute
from nexus.tools.patch import apply_patch
from tests.conftest import Recorder, python_command


async def test_shell_output_cwd_unicode_stdin_and_nonzero(execution: ExecutionContext) -> None:
    folder = execution.workspace / "space 中文"
    folder.mkdir()
    result = await execute(
        {
            "command": python_command(
                "import os,sys; print(os.getcwd()); print('hello'); "
                "print('error',file=sys.stderr); assert sys.stdin.read()==''; raise SystemExit(7)"
            ),
            "workdir": str(folder),
        },
        execution,
        Recorder(),
    )
    # PowerShell -Command returns the shell's exit code, not the child's LASTEXITCODE.
    assert not result.ok and result.data["exit_code"] == (1 if os.name == "nt" else 7)
    assert result.data["cwd"] == str(folder)
    assert "hello" in result.data["stdout"] and "error" in result.data["stderr"]


@pytest.mark.parametrize("stream", ["stdout", "stderr", "both"])
async def test_shared_budget_drains_and_keeps_tail(
    execution: ExecutionContext, stream: str
) -> None:
    execution.output_limit_bytes = 4096
    target = "sys.stdout" if stream != "stderr" else "sys.stderr"
    code = f"import sys; {target}.write('HEAD'+('x'*100000)+'TAIL'); {target}.flush()"
    if stream == "both":
        code += "; sys.stderr.write('e'*100000); sys.stderr.flush()"
    recorder = Recorder()
    result = await execute({"command": python_command(code)}, execution, recorder)
    assert result.ok and result.truncated
    assert sum(len(result.data[s].encode()) for s in ("stdout", "stderr")) < 4300
    assert "HEAD" in result.data["stderr" if stream == "stderr" else "stdout"]
    if stream != "both":
        assert "TAIL" in result.data[stream]
        assert result.data["discarded_bytes"][stream] == 100008 - 4096
    assert (
        sum(len(d["text"].encode()) for k, d in recorder.events if k == "tool_output_delta") <= 4096
    )


async def test_timeout_and_cancel_reap_child(execution: ExecutionContext) -> None:
    child = execution.workspace / "child.pid"
    code = (
        "import subprocess,sys,time,pathlib; "
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
        "pathlib.Path('child.pid').write_text(str(p.pid)); time.sleep(30)"
    )
    result = await execute(
        {"command": python_command(code), "timeout_ms": 1800}, execution, Recorder()
    )
    assert result.data["timed_out"] and not result.data["cleanup_incomplete"]
    assert child.exists()
    pid = int(child.read_text())
    check = (
        (
            f"import ctypes; h=ctypes.windll.kernel32.OpenProcess(0x1000,False,{pid}); "
            "code=ctypes.c_ulong(); "
            "ctypes.windll.kernel32.GetExitCodeProcess(h,ctypes.byref(code)); "
            "assert not h or code.value != 259"
        )
        if os.name == "nt"
        else (
            f"import pathlib; p=pathlib.Path('/proc/{pid}/stat'); "
            "assert not p.exists() or p.read_text().split()[2]=='Z'"
        )
    )
    checked = await execute({"command": python_command(check)}, execution, Recorder())
    assert checked.ok
    recorder = Recorder()
    task = asyncio.create_task(
        execute(
            {"command": python_command("import time; print('ready',flush=True); time.sleep(30)")},
            execution,
            recorder,
        )
    )
    await asyncio.wait_for(recorder.output_started.wait(), timeout=10)
    task.cancel()
    with pytest.raises(ToolCancelled) as caught:
        await task
    assert caught.value.result.data["cancelled"]
    assert not caught.value.result.data["cleanup_incomplete"]


async def test_patch_multifile_crlf_no_newline(execution: ExecutionContext) -> None:
    root = execution.workspace
    (root / "a.txt").write_bytes(b"first\r\nold\r\nlast")
    (root / "delete.txt").write_bytes(b"bye\n")
    patch = (
        "--- a/a.txt\n+++ b/a.txt\n@@ -1,3 +1,3 @@\n first\n-old\n+new\n last\n"
        "\\ No newline at end of file\n"
        "--- /dev/null\n+++ b/sub/new.txt\n@@ -0,0 +1 @@\n+created\n"
        "--- a/delete.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-bye\n"
    )
    result = await apply_patch({"patch": patch}, execution, Recorder())
    assert result.ok, result
    assert (root / "a.txt").read_bytes() == b"first\r\nnew\r\nlast"
    assert (root / "sub/new.txt").read_bytes() == b"created\n"
    assert not (root / "delete.txt").exists()
    assert (
        result.data["files"][0]["after_hash"] == hashlib.sha256(b"first\r\nnew\r\nlast").hexdigest()
    )
    assert "No newline" in result.data["files"][0]["diff"]
    assert result.data["created_directories"] == ["sub"]


@pytest.mark.parametrize("path", ["../outside", "/absolute", "C:/file", "a/../x", "x:stream"])
async def test_patch_path_boundary(execution: ExecutionContext, path: str) -> None:
    result = await apply_patch(
        {"patch": f"--- /dev/null\n+++ b/{path}\n@@ -0,0 +1 @@\n+x\n"}, execution, Recorder()
    )
    assert result.error_code == "path_escape"


async def test_patch_preflight_conflict_relocation_and_ambiguity(
    execution: ExecutionContext,
) -> None:
    path = execution.workspace / "a.txt"
    path.write_text("prefix\nold\n", encoding="utf-8")
    patch = "--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-old\n+new\n"
    result = await apply_patch({"patch": patch}, execution, Recorder())
    assert result.ok and path.read_text() == "prefix\nnew\n"
    path.write_text("prefix\nold\nold\n", encoding="utf-8")
    result = await apply_patch({"patch": patch}, execution, Recorder())
    assert result.error_code == "patch_conflict"
    assert path.read_text() == "prefix\nold\nold\n"
    result = await apply_patch(
        {"patch": "--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1 @@\n+x\n" + patch},
        execution,
        Recorder(),
    )
    assert not result.ok and not (execution.workspace / "new.txt").exists()


async def test_patch_partial_io_and_concurrent_change(
    execution: ExecutionContext, monkeypatch: Any
) -> None:
    from nexus.tools import patch as module

    original = module._commit
    calls = 0

    def fail_second(path: Path, change: Any) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("disk failure")
        original(path, change)

    monkeypatch.setattr(module, "_commit", fail_second)
    patch = "".join(f"--- /dev/null\n+++ b/{name}\n@@ -0,0 +1 @@\n+x\n" for name in ("a", "b"))
    result = await apply_patch({"patch": patch}, execution, Recorder())
    assert result.error_code == "patch_io_error" and result.data["partial"]
    assert result.data["changed_files"] == 1 and result.data["failed_file"] == "b"


async def test_patch_symlink_rejected(execution: ExecutionContext) -> None:
    target = execution.workspace / "target"
    target.write_text("old\n")
    link = execution.workspace / "link"
    try:
        link.symlink_to(target)
    except OSError:
        if os.name != "nt":
            raise
        folder = execution.workspace / "junction-target"
        folder.mkdir()
        (folder / "target").write_text("old\n")
        junction = execution.workspace / "junction"
        created = await asyncio.to_thread(
            subprocess.run,
            ["cmd", "/c", "mklink", "/J", str(junction), str(folder)],
            capture_output=True,
            check=False,
        )
        assert created.returncode == 0
        result = await apply_patch(
            {"patch": "--- a/junction/target\n+++ b/junction/target\n@@ -1 +1 @@\n-old\n+new\n"},
            execution,
            Recorder(),
        )
        assert result.error_code == "path_escape"
        assert (folder / "target").read_text() == "old\n"
        return
    result = await apply_patch(
        {"patch": "--- a/link\n+++ b/link\n@@ -1 +1 @@\n-old\n+new\n"}, execution, Recorder()
    )
    assert result.error_code == "path_escape" and target.read_text() == "old\n"
