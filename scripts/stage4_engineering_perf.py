"""Offline engineering measurements; fake manager is NOT isolation acceptance.

Run from checkout: python -m scripts.stage4_engineering_perf
"""

from __future__ import annotations

import asyncio
import json
import platform
import statistics
import sys
import tempfile
import time
from pathlib import Path

from nexus.app.config import default_shell
from nexus.app.events import Events
from nexus.app.session import SessionLog
from nexus.core.security import SecurityGateway
from nexus.core.types import ExecutionContext, Json, RuntimeEvent, Session
from nexus.tools.registry import native_tools
from tests.conftest import Recorder, call
from tests.test_sandbox_stage4 import connected


async def main() -> None:
    report: Json = {
        "platform": platform.platform(),
        "python": sys.version,
        "isolation_verified": False,
        "paid_inference": False,
    }

    async def consume(event: RuntimeEvent) -> None:
        pass

    with tempfile.TemporaryDirectory(prefix="nexus-stage4-perf-") as folder:
        root = Path(folder)
        session = Session(root, run_id="perf")
        context = ExecutionContext(root, "perf", default_shell())
        registry = native_tools()
        gateway = SecurityGateway()
        writer = SessionLog.create(session, "perf", {}, root / "logs")
        events = Events(session, writer, consume)
        durations: dict[str, list[float]] = {"direct_local_ms": [], "gateway_local_ms": []}
        command = "Write-Output ok" if sys.platform == "win32" else "printf ok"
        args = {"command": command}
        target = call("exec_command", args, "perf")
        try:
            # Interleave the same actual shell, argv and output budget; no model/network.
            for _ in range(10):
                for label in durations:
                    start = time.perf_counter()
                    value = (
                        await registry["exec_command"].execute(args, context, events)
                        if label == "direct_local_ms"
                        else await gateway.execute(target, registry, session, context, events)
                    )
                    assert value.ok
                    durations[label].append((time.perf_counter() - start) * 1000)
            report["local_shell"] = {
                name: {"samples": values, "median": statistics.median(values)}
                for name, values in durations.items()
            }
        finally:
            writer.close()
        startups: list[float] = []
        workers: list[float] = []
        for i in range(5):
            directory = root / str(i)
            directory.mkdir()
            start = time.perf_counter()
            client = await connected(directory)
            startups.append((time.perf_counter() - start) * 1000)
            try:
                for j in range(3):
                    start = time.perf_counter()
                    value = await client.execute(
                        "apply_patch",
                        {"patch": f"*** Begin Patch\n*** Add File: {j}.txt\n+ok\n*** End Patch"},
                        ExecutionContext(directory / "work", "perf", ""),
                        Recorder(),
                    )
                    assert value.ok
                    workers.append((time.perf_counter() - start) * 1000)
            finally:
                await client.close()
        report["mock_bridge_init_ms"] = {"samples": startups, "median": statistics.median(startups)}
        report["worker_spawn_patch_audit_ms"] = {
            "samples": workers,
            "median": statistics.median(workers),
        }
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
