"""Opt-in normal SRT functional acceptance. No model, attacks, or machine provisioning.

Run with a wheel installed outside all test write roots. Evidence goes to --output.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from nexus.app.config import default_shell
from nexus.core.security import SecurityPolicy
from nexus.core.types import ExecutionContext, Json, ToolCancelled
from nexus.tools.sandbox import SRTBackend


async def run(
    output: Path,
    temp_root: Path | None = None,
    workspace_only: bool = False,
    skip_stop_tests: bool = False,
) -> None:
    # Inherit ordinary directory permissions: mkdtemp uses an owner-only Windows
    # ACL, and SRT-created files have a different OS owner from this test runner.
    root = (temp_root or Path(tempfile.gettempdir())) / ("nexus-stage4b-functional-" + uuid4().hex)
    root.mkdir()
    workspace = root / "work"
    workspace.mkdir()
    await asyncio.to_thread(subprocess.run, ["git", "init", "-q", str(workspace)], check=True)
    (workspace / "hello.txt").write_text("hello sandbox", encoding="utf-8")
    rows: list[Json] = []
    cancel_ready = asyncio.Event()

    async def emit(kind: str, data: Json) -> None:
        rows.append({"event": kind, "data": data})
        if (
            kind == "tool_output_delta"
            and data.get("call_id") == "cancel"
            and "ready" in data.get("text", "")
        ):
            cancel_ready.set()

    (workspace / "test_example.py").write_text("def test_ok(): assert 1 + 1 == 2\n")
    outside = root / "outside"
    outside.mkdir()
    backend = SRTBackend(SecurityPolicy(mode="read-only"))

    async def invoke(label: str, command: str | None = None, **args: object) -> Json:
        started = time.perf_counter()
        value = await backend.execute(
            "exec_command" if command is not None else "apply_patch",
            {"command": command, **args} if command is not None else dict(args),
            ExecutionContext(workspace, label, default_shell()),
            emit,
        )
        row = {
            "test": label,
            "elapsed_ms": (time.perf_counter() - started) * 1000,
            "result": asdict(value),
        }
        rows.append(row)
        print(json.dumps(row, ensure_ascii=True), flush=True)
        if value.error_code == "cleanup_incomplete":
            raise RuntimeError("Stop acceptance: cleanup is unconfirmed")
        return row

    try:
        if not workspace_only:
            await invoke("read-only-identity", "whoami; Get-Content hello.txt")
            await invoke("read-only-deny-write", "Set-Content forbidden.txt no -ErrorAction Stop")
            await invoke(
                "read-only-deny-patch",
                patch="*** Begin Patch\n*** Add File: denied.txt\n+x\n*** End Patch",
            )
        await backend.close()
        backend = SRTBackend(
            SecurityPolicy(mode="workspace-write", allowed_domains=("example.com",))
        )
        await invoke(
            "workspace-write-identity", "whoami; Set-Content new.txt yes; Get-Content new.txt"
        )
        await invoke(
            "outside-deny-write", f"Set-Content '{outside / 'forbidden.txt'}' no -ErrorAction Stop"
        )
        await invoke("python-pytest", f"& '{sys.executable}' -m pytest -q -p no:cacheprovider")
        await invoke(
            "patch",
            patch="*** Begin Patch\n*** Add File: patched.txt\n+patched by SRT\n*** End Patch",
        )
        await invoke(
            "git",
            "git add hello.txt; git -c user.name=NexusTest "
            "-c user.email=nexus-test@example.invalid commit -m acceptance; git status --short",
        )
        await invoke("git-config-denied", "Set-Content .git/config forbidden -ErrorAction Stop")
        await invoke(
            "git-hook-denied", "Set-Content .git/hooks/pre-commit forbidden -ErrorAction Stop"
        )
        await invoke(
            "allowlisted-network",
            f"& '{sys.executable}' -c "
            '"import urllib.request; '
            "print(urllib.request.urlopen('https://example.com', timeout=20).status)\"",
        )
        if not skip_stop_tests:
            await invoke("timeout", "Start-Sleep -Seconds 10", timeout_ms=1500)
            task = asyncio.create_task(
                invoke("cancel", "Write-Output ready; Start-Sleep -Seconds 30")
            )
            await asyncio.wait_for(cancel_ready.wait(), 150)
            task.cancel()
            try:
                await task
            except ToolCancelled as exc:
                rows.append({"test": "cancel", "result": asdict(exc.result)})
            await invoke("reuse-after-cancel", "Get-Content patched.txt")
        else:
            rows.append({"test": "timeout-cancel", "status": "NOT RUN in this attempt"})
        rows.append(
            {
                "test": "host-files",
                "read_only_write_denied": not (workspace / "forbidden.txt").exists(),
                "outside_write_denied": not (outside / "forbidden.txt").exists(),
                "patch_contents": (workspace / "patched.txt").read_text()
                if (workspace / "patched.txt").exists()
                else None,
            }
        )
        await backend.close()
        backend = SRTBackend(SecurityPolicy(mode="read-only"))
        await invoke("roundtrip-read-only", "Get-Content patched.txt")
        await invoke("roundtrip-deny-write", "Set-Content ro-denied.txt no -ErrorAction Stop")
        await backend.close()
        backend = SRTBackend(SecurityPolicy(mode="workspace-write"))
        await invoke("roundtrip-workspace-write", "Set-Content roundtrip.txt yes -ErrorAction Stop")
        rows.append(
            {"test": "roundtrip-host-read", "contents": (workspace / "roundtrip.txt").read_text()}
        )
    finally:
        try:
            await backend.close()
            rows.append({"test": "close", "confirmed": True})
        except OSError as exc:
            rows.append({"test": "close", "confirmed": False, "detail": str(exc)})
        output.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(
            output.write_text,
            json.dumps({"root": str(root), "rows": rows}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--temp-root", type=Path)
    parser.add_argument("--workspace-only", action="store_true")
    parser.add_argument("--skip-stop-tests", action="store_true")
    arguments = parser.parse_args()
    asyncio.run(
        run(
            arguments.output,
            arguments.temp_root,
            arguments.workspace_only,
            arguments.skip_stop_tests,
        )
    )
