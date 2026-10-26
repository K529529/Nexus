"""Bounded Local comparison through the original Gateway; no Docker or model calls."""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
from dataclasses import asdict
from pathlib import Path

from nexus.core.security import SecurityGateway
from nexus.core.types import ExecutionContext, Session, ToolCall
from nexus.tools.registry import default_tools


async def main():
    report = {"python": sys.executable, "rows": []}
    with tempfile.TemporaryDirectory(prefix="nexus-local-4c-") as directory:
        root = Path(directory)
        session = Session(root)
        gateway = SecurityGateway()
        registry = default_tools(session)

        async def emit(*args, **kw):
            return 0

        calls = [("exec_command", {"command": "printf local-ok"})] * 3 + [
            (
                "apply_patch",
                {"patch": "*** Begin Patch\n*** Add File: local.txt\n+ok\n*** End Patch"},
            )
        ]
        for index, (name, args) in enumerate(calls):
            started = time.monotonic()
            ident = str(index)
            result = await gateway.execute(
                ToolCall(ident, name, json.dumps(args)),
                registry,
                session,
                ExecutionContext(root, ident, "/bin/sh"),
                emit,
            )
            report["rows"].append(
                {"tool": name, "ms": (time.monotonic() - started) * 1000, "result": asdict(result)}
            )
            assert result.ok
        assert (root / "local.txt").read_text() == "ok\n"
        report["docker_module_loaded"] = "nexus.tools.linux_docker" in sys.modules
        assert not report["docker_module_loaded"] and gateway.backend is None
    await asyncio.to_thread(
        Path(sys.argv[1]).write_text, json.dumps(report, indent=2), encoding="utf-8"
    )


asyncio.run(main())
