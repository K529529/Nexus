"""Regression: SRT ACL operations must never target Windows-managed installs."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from nexus.core.types import ExecutionContext
from nexus.tools.sandbox import BridgeClient, BridgeError
from tests.conftest import Recorder

BRIDGE = Path(__file__).resolve().parents[1] / "src/nexus/tools/srt_bridge.mjs"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(sys.platform != "win32" or not NODE, reason="Windows Node policy")


@pytest.mark.parametrize("mode", ["read-only", "workspace-write"])
async def test_system_powershell_never_reaches_acl_manager(tmp_path: Path, mode: str) -> None:
    shell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    workspace = tmp_path / "work"
    workspace.mkdir()
    captured = tmp_path / "config.json"
    entry = tmp_path / "capture.mjs"
    entry.write_text(
        f"import {{serve}} from {json.dumps(BRIDGE.as_uri())};\n"
        "import {writeFileSync} from 'node:fs';\n"
        f"await serve({{initialize: async c=>writeFileSync({json.dumps(str(captured))},"
        "JSON.stringify(c)), reset:async()=>{},"
        "wrapWithSandboxArgv:async(text,shell)=>({argv:[shell.exe,...shell.args,text],"
        "env:process.env})});\n",
        encoding="utf-8",
    )
    client = BridgeClient((str(NODE), str(entry)), cwd=tmp_path)
    try:
        await client.initialize(
            {
                "mode": mode,
                "workspace": str(workspace),
                "python": sys.executable,
                "read_roots": [str(shell.parent)],
            }
        )
        config = json.loads(captured.read_text())["filesystem"]
        for paths in config.values():
            assert not any(Path(p).is_relative_to(Path(os.environ["SystemRoot"])) for p in paths)
        assert str(workspace) in config["allowRead"]
        if mode == "workspace-write":
            assert str(workspace / ".git/config") in config["denyWrite"]
        result = await client.execute(
            "exec_command",
            {"command": "Write-Output system-shell"},
            ExecutionContext(workspace, "ps", str(shell)),
            Recorder(),
        )
        assert result.ok and "system-shell" in result.data["stdout"]
    finally:
        await client.close()


def test_all_system_runtime_sources_are_filtered_but_other_denies_remain(tmp_path: Path) -> None:
    windows = Path(os.environ["SystemRoot"])
    programs = Path(os.environ["ProgramFiles"])
    inputs = {
        "workspace": str(tmp_path / "work"),
        "roots": [str(tmp_path / "work")],
        "ipcDirectory": str(tmp_path / "ipc"),
        "worker": str(programs / "Nexus/Lib/site-packages/nexus/tools/patch_worker.py"),
        "python": str(windows / "System32/python.exe"),
        "readRoots": [
            str(programs / "Python"),
            str(windows / "System32"),
            str(tmp_path / "runtime"),
            str(windows) + "-other",
        ],
    }
    script = (
        f"import {{filesystemPolicy}} from {json.dumps(BRIDGE.as_uri())};"
        "const [p,roots]=JSON.parse(process.argv[1]);"
        "console.log(JSON.stringify(filesystemPolicy(p,roots)));"
    )
    value = subprocess.run(
        [
            str(NODE),
            "--input-type=module",
            "-e",
            script,
            json.dumps([inputs, [str(windows), str(programs)]]),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    policy = json.loads(value.stdout)
    for key in ("allowRead", "denyWrite"):
        assert str(tmp_path / "runtime") in policy[key]
        assert str(windows) + "-other" in policy[key]
        assert not any(
            Path(p).is_relative_to(windows) or Path(p).is_relative_to(programs) for p in policy[key]
        )
    assert str(tmp_path / "work/.git/hooks") in policy["denyWrite"]


@pytest.mark.parametrize("target", ["workspace", "writable", "runtime-ancestor"])
async def test_system_acl_overlap_rejected_before_initialize(tmp_path: Path, target: str) -> None:
    workspace = tmp_path / "work"
    workspace.mkdir()
    windows = Path(os.environ["SystemRoot"])
    entry = tmp_path / "never-initialize.mjs"
    entry.write_text(
        f"import {{serve}} from {json.dumps(BRIDGE.as_uri())};"
        "await serve({initialize:async()=>{throw Error('MANAGER MUST NOT RUN');},"
        "reset:async()=>{}});",
        encoding="utf-8",
    )
    client = BridgeClient((str(NODE), str(entry)), cwd=tmp_path)
    params = {"mode": "workspace-write", "workspace": str(workspace), "python": sys.executable}
    if target == "workspace":
        params["workspace"] = str(windows / "System32")
    elif target == "writable":
        params["writable_roots"] = [str(windows)]  # type: ignore[assignment]
    else:
        params["read_roots"] = [str(windows.parent)]  # type: ignore[assignment]
    with pytest.raises(BridgeError, match="System directory|Runtime ACL root"):
        await client.initialize(params)
    assert client.process is None
