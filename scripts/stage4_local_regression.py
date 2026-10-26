"""Small installed-package Local behavior/timing comparison; no model or ACL changes."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

import nexus
from nexus.core.security import SecurityGateway
from nexus.core.types import ExecutionContext, Session, ToolCall
from nexus.tools.registry import default_tools


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--parent", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    root = args.parent.resolve(strict=True) / ("nexus-local-hardening-" + uuid4().hex)
    root.mkdir()
    shell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    # No Node/Koffi resolution possible through PATH; Local still uses its own executor.
    os.environ["PATH"] = str(shell.parent) + os.pathsep + str(shell.parents[2])
    os.environ.pop("PSModulePath", None)
    gateway = SecurityGateway()
    session = Session(root)
    registry = default_tools(session)
    report = {
        "python": sys.executable,
        "package": list(nexus.__path__),
        "root": str(root),
        "rows": [],
    }

    async def emit(*args):
        pass

    async def invoke(name, arguments):
        call_id = uuid4().hex
        start = time.perf_counter()
        result = await gateway.execute(
            ToolCall(call_id, name, json.dumps(arguments)),
            registry,
            session,
            ExecutionContext(root, call_id, str(shell)),
            emit,
        )
        report["rows"].append(
            {"name": name, "ms": (time.perf_counter() - start) * 1000, "result": asdict(result)}
        )
        return result

    for _ in range(3):
        assert (await invoke("exec_command", {"command": "Write-Output local-ok"})).ok
    assert (
        await invoke(
            "apply_patch", {"patch": "*** Begin Patch\n*** Add File: local.txt\n+ok\n*** End Patch"}
        )
    ).ok
    assert (root / "local.txt").read_text().strip() == "ok"
    assert (await invoke("exec_command", {"command": "Set-Content shell.txt ok"})).ok
    assert (root / "shell.txt").exists()
    report["native_module_loaded"] = "nexus.tools.windows_native" in sys.modules
    report["backend"] = gateway.backend
    assert not report["native_module_loaded"] and gateway.backend is None
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
