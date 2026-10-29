from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path

import pytest

from nexus.app.config import default_shell
from nexus.core.types import ExecutionContext, Json, ToolCancelled
from nexus.tools.sandbox import BridgeClient, BridgeError
from tests.conftest import Recorder, python_command

TOOLS = Path(__file__).resolve().parents[1] / "src/nexus/tools"
NODE = shutil.which("node")


def fake_entry(tmp_path: Path) -> Path:
    """UNSANDBOXED test adapter. Only fixed fixtures; never a production config option."""
    path = tmp_path / "fake-manager.mjs"
    path.write_text(
        f"import {{serve}} from {json.dumps((TOOLS / 'srt_bridge.mjs').as_uri())};\n"
        "await serve({initialize: async () => {}, reset: async () => {},\n"
        "wrapWithSandboxArgv: async (command, shell) => ({\n"
        " argv: [shell.exe, ...shell.args, command], env: process.env})});\n",
        encoding="utf-8",
    )
    return path


async def connected(tmp_path: Path, mode: str = "workspace-write") -> BridgeClient:
    if not NODE:
        pytest.skip("Node unavailable; Bridge functional tests require Node")
    client = BridgeClient((NODE, str(fake_entry(tmp_path))), cwd=tmp_path)
    workspace = tmp_path / "work"
    workspace.mkdir(exist_ok=True)
    await client.initialize(
        {"mode": mode, "workspace": str(workspace), "python": sys.executable, "writable_roots": []}
    )
    return client


@pytest.mark.skipif(not NODE, reason="Node required")
async def test_real_entry_is_closed_before_srt_initialization(tmp_path: Path) -> None:
    client = BridgeClient((str(NODE), str(TOOLS / "srt_bridge.mjs")), cwd=tmp_path)
    with pytest.raises(BridgeError, match="backend_unavailable"):
        await client.initialize(
            {"mode": "workspace-write", "workspace": str(tmp_path), "python": sys.executable}
        )
    assert client.broken and client.process is None


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell Bridge target")
async def test_bridge_reuse_streams_unicode_truncation_and_timeout(tmp_path: Path) -> None:
    client = await connected(tmp_path)
    context = ExecutionContext(tmp_path / "work", "c1", default_shell(), 1024)
    recorder = Recorder()
    try:
        process = client.process
        value = await client.execute(
            "exec_command",
            {
                "command": python_command(
                    "import sys; print('hello'); sys.stderr.write('error\\n'); print('x'*20000)"
                )
            },
            context,
            recorder,
        )
        assert value.ok and value.truncated
        assert value.data["discarded_bytes"]["stdout"] > 10000
        assert recorder.output_started.is_set()
        assert client.process is process
        value = await client.execute(
            "exec_command", {"command": "Write-Output '你好'"}, context, Recorder()
        )
        assert value.ok and "你好" in value.data["stdout"]
        value = await client.execute(
            "exec_command",
            {"command": "Start-Sleep -Seconds 10", "timeout_ms": 200},
            context,
            Recorder(),
        )
        assert not value.ok and value.data["timed_out"]
        assert value.error_code == "command_timeout"
    finally:
        await client.close()
    assert process is not None and process.returncode is not None


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell Bridge target")
async def test_bridge_cancel_reaps_active_shell(tmp_path: Path) -> None:
    client = await connected(tmp_path)
    recorder = Recorder()
    try:
        task = asyncio.create_task(
            client.execute(
                "exec_command",
                {
                    "command": "Write-Output started; Start-Sleep -Seconds 30",
                },
                ExecutionContext(tmp_path / "work", "c", default_shell()),
                recorder,
            )
        )
        await asyncio.wait_for(recorder.output_started.wait(), 5)
        task.cancel()
        with pytest.raises(ToolCancelled) as error:
            await task
        assert error.value.result.data["cancelled"]
        assert not error.value.result.data["cleanup_incomplete"]
    finally:
        await client.close()


async def test_bridge_worker_reuses_patch_core_and_audit(tmp_path: Path) -> None:
    client = await connected(tmp_path)
    workspace = tmp_path / "work"
    # Poisoned workspace code must never be imported by python -I trusted-worker.py.
    (workspace / "nexus").mkdir()
    (workspace / "nexus/__init__.py").write_text("raise RuntimeError('workspace imported')")
    (workspace / "sitecustomize.py").write_text("raise RuntimeError('workspace imported')")
    recorder = Recorder()
    context = ExecutionContext(workspace, "patch-call", "", 4096)
    try:
        patch = "*** Begin Patch\n*** Add File: a.txt\n+hello\n*** End Patch"
        value = await client.execute("apply_patch", {"patch": patch}, context, recorder)
        assert value.ok and (workspace / "a.txt").read_text() == "hello\n"
        assert value.data["changed_files"] == 1
        assert [k for k, _ in recorder.events] == [
            "patch_manifest", "patch_diff", "patch_committed"]
        assert recorder.events[0][1]["call_id"] == context.call_id
        conflict = await client.execute("apply_patch", {"patch": patch}, context, Recorder())
        assert not conflict.ok and conflict.error_code == "patch_conflict"
        bad = await client.execute(
            "apply_patch",
            {"patch": "*** Begin Patch\n*** Add File: ../outside\n+x\n*** End Patch"},
            context,
            Recorder(),
        )
        assert not bad.ok and not (tmp_path / "outside").exists()
    finally:
        await client.close()


async def test_completed_patch_can_close_immediately_without_recreating_ack(tmp_path: Path) -> None:
    client = await connected(tmp_path)
    try:
        result = await client.execute(
            "apply_patch",
            {"patch": "*** Begin Patch\n*** Add File: once.txt\n+done\n*** End Patch"},
            ExecutionContext(tmp_path / "work", "once", ""),
            Recorder(),
        )
        assert result.ok
    finally:
        await client.close()
    assert client.cleanup_confirmed, client.stderr


@pytest.mark.skipif(sys.platform != "win32" or not NODE, reason="Windows Node Bridge target")
async def test_patch_terminal_result_waits_for_ipc_cleanup(tmp_path: Path) -> None:
    entry = fake_entry(tmp_path)
    original = entry.read_text(encoding="utf-8")
    marker = tmp_path / "cleaned.txt"
    entry.write_text(
        "import fs from 'node:fs'; import {syncBuiltinESMExports} from 'node:module';\n"
        "const unlink=fs.promises.unlink; fs.promises.unlink=async path=>{\n"
        "await new Promise(r=>setTimeout(r,150)); await unlink(path);\n"
        f"fs.writeFileSync({json.dumps(str(marker))},'cleaned');}};\n"
        "syncBuiltinESMExports();\n" + original,
        encoding="utf-8",
    )
    workspace = tmp_path / "work"
    workspace.mkdir()
    client = BridgeClient((str(NODE), str(entry)), cwd=tmp_path)
    await client.initialize(
        {"mode": "workspace-write", "workspace": str(workspace), "python": sys.executable}
    )
    try:
        context = ExecutionContext(workspace, "sequential", default_shell())
        patched = await client.execute(
            "apply_patch",
            {"patch": "*** Begin Patch\n*** Add File: ready.txt\n+ready\n*** End Patch"},
            context,
            Recorder(),
        )
        assert patched.ok and marker.exists()
        following = await client.execute(
            "exec_command", {"command": "Get-Content ready.txt"}, context, Recorder()
        )
        assert following.ok and "ready" in following.data["stdout"]
    finally:
        await client.close()


@pytest.mark.skipif(sys.platform != "win32" or not NODE, reason="Windows Node Bridge target")
async def test_read_only_deny_is_owned_by_each_command_not_kept_session(tmp_path: Path) -> None:
    path = tmp_path / "deny-contract.mjs"
    path.write_text(
        f"import {{serve}} from {json.dumps((TOOLS / 'srt_bridge.mjs').as_uri())};\n"
        "let root; await serve({initialize: async config => {\n"
        "root=config.filesystem.allowRead[0];\n"
        "if(config.filesystem.allowWrite.length || config.filesystem.denyWrite.includes(root))\n"
        "throw Error('RO root deny must not be persisted');}, reset: async()=>{},\n"
        "wrapWithSandboxArgv: async(text,shell,perCommand)=>{\n"
        "if(perCommand?.filesystem?.denyWrite?.[0] !== root) throw Error('RO deny missing');\n"
        "return {argv:[shell.exe,...shell.args,text],env:process.env};}});\n",
        encoding="utf-8",
    )
    workspace = tmp_path / "work"
    workspace.mkdir()
    client = BridgeClient((str(NODE), str(path)), cwd=tmp_path)
    try:
        await client.initialize(
            {
                "mode": "read-only",
                "workspace": str(workspace),
                "python": sys.executable,
                "writable_roots": [],
            }
        )
        result = await client.execute(
            "exec_command",
            {"command": "Write-Output protected"},
            ExecutionContext(workspace, "ro", default_shell()),
            Recorder(),
        )
        assert result.ok and "protected" in result.data["stdout"]
    finally:
        await client.close()
    assert client.cleanup_confirmed


async def test_worker_read_only_rejected(tmp_path: Path) -> None:
    client = await connected(tmp_path, "read-only")
    try:
        value = await client.execute(
            "apply_patch",
            {"patch": "unused"},
            ExecutionContext(tmp_path / "work", "c", ""),
            Recorder(),
        )
        assert value.error_code == "policy_denied"
    finally:
        await client.close()


async def test_worker_cancel_at_audit_barrier_preserves_partial(tmp_path: Path) -> None:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        str(TOOLS / "patch_worker.py"),
        cwd=tmp_path,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert process.stdin is not None and process.stdout is not None
    request = {
        "id": "req",
        "call_id": "c",
        "workspace": str(tmp_path),
        "output_limit_bytes": 4096,
        "arguments": {
            "patch": (
                "*** Begin Patch\n*** Add File: a\n+first\n*** Add File: b\n+second\n*** End Patch"
            )
        },
    }
    try:
        process.stdin.write((json.dumps(request) + "\n").encode())
        await process.stdin.drain()
        event = json.loads(await asyncio.wait_for(process.stdout.readline(), 5))
        assert event["kind"] == "patch_diff"
        assert (tmp_path / "a").exists() and not (tmp_path / "b").exists()
        process.stdin.write(b'{"id":"req","method":"cancel"}\n')
        await process.stdin.drain()
        result = json.loads(await asyncio.wait_for(process.stdout.readline(), 5))
        assert result["cancelled"] and result["result"]["data"]["partial"]
        assert result["result"]["data"]["changed_files"] == 1
        await asyncio.wait_for(process.wait(), 5)
        assert process.returncode == 0
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


@pytest.mark.parametrize("response", ["eof", "wrong-id", "bad-json", "incomplete-result"])
async def test_bridge_failure_is_unknown_and_never_retried(tmp_path: Path, response: str) -> None:
    code = (
        "import sys,json\n"
        "r=json.loads(sys.stdin.readline())\n"
        "print(json.dumps({'id':r['id'],'type':'result','result':{'initialized':True}}),flush=True)\n"
        "r=json.loads(sys.stdin.readline())\n"
    )
    if response == "wrong-id":
        code += "print(json.dumps({'id':'wrong','type':'result'}),flush=True)\n"
    elif response == "bad-json":
        code += "print('not json',flush=True)\n"
    elif response == "incomplete-result":
        code += "print(json.dumps({'id':r['id'],'type':'result','result':{}}),flush=True)\n"
    client = BridgeClient((sys.executable, "-u", "-c", code), cwd=tmp_path)
    await client.initialize({})
    value = await client.execute(
        "exec_command", {"command": "fixed"}, ExecutionContext(tmp_path, "c", ""), Recorder()
    )
    assert value.error_code == "cleanup_incomplete"
    assert value.data["execution_state"] == "interrupted_unknown"
    assert client.broken
    again = await client.execute(
        "exec_command", {}, ExecutionContext(tmp_path, "c", ""), Recorder()
    )
    assert again.error_code == "backend_unavailable"


async def test_worker_audit_failure_stops_further_files(tmp_path: Path) -> None:
    client = await connected(tmp_path)

    async def failing(kind: str, data: Json, **_: object) -> int:
        raise OSError("disk full")

    try:
        value = await client.execute(
            "apply_patch",
            {
                "patch": "*** Begin Patch\n"
                "*** Add File: a\n+first\n*** Add File: b\n+second\n*** End Patch"
            },
            ExecutionContext(tmp_path / "work", "c", ""),
            failing,
        )
        assert value.error_code == "cleanup_incomplete"
        assert not (tmp_path / "work/a").exists()
        assert not (tmp_path / "work/b").exists()
    finally:
        await client.close()
