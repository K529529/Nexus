"""Reproduce shell status vs validation evidence without model or network calls.

Run from the Nexus checkout with its Python environment; pass a JSON output path.
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import shlex
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nexus.app.config import default_shell  # noqa: E402
from nexus.core.types import ExecutionContext, Json  # noqa: E402
from nexus.tools.execution import execute  # noqa: E402


def command(code: str) -> str:
    if os.name == "nt":
        return "& '" + sys.executable.replace("'", "''") + "' -c '" + code.replace("'", "''") + "'"
    return shlex.quote(sys.executable) + " -c " + shlex.quote(code)


async def emit(kind: str, data: Json, *, protocol_data: Json | None = None) -> int:
    return 0


async def probe() -> Json:
    code = "print('CHECK START'); print('x'*100000); print('CHECK FAILED'); raise SystemExit(7)"
    failed = command(code)
    suffix = " | Select-Object -Last 1" if os.name == "nt" else " | tail -n 1"
    cases = {
        "failed_direct": failed,
        "failed_filtered": failed + suffix,
        "success_with_error_text": command("print('SyntaxError: expected fixture text')"),
        "failure_explicit_exit": failed
        + ("; exit $LASTEXITCODE" if os.name == "nt" else "; exit $?"),
        "failure_then_success": failed
        + ("; Write-Output followup" if os.name == "nt" else "; printf followup"),
    }
    rows = []
    with TemporaryDirectory(prefix="nexus-shell-probe-") as directory:
        context = ExecutionContext(
            Path(directory), "probe", default_shell(), output_limit_bytes=4096
        )
        for label, value in cases.items():
            result = await execute({"command": value}, context, emit)
            rows.append(
                {
                    "case": label,
                    "ok": result.ok,
                    "exit_code": result.data["exit_code"],
                    "error_code": result.error_code,
                    "truncated": result.truncated,
                    "stdout_bytes": len(result.data["stdout"].encode()),
                    "stdout_head": result.data["stdout"][:80],
                    "stdout_tail": result.data["stdout"][-80:],
                    "stderr_tail": result.data["stderr"][-200:],
                }
            )
    expected = [False, os.name != "nt", True, False, True]
    assert [row["ok"] for row in rows] == expected, rows
    assert rows[0]["truncated"] and rows[0]["stdout_bytes"] < 4300
    assert "CHECK START" in rows[0]["stdout_head"] and "CHECK FAILED" in rows[0]["stdout_tail"]
    payload = {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "shell": default_shell(),
        "status": "PASS",
        "cases": rows,
        "scope": "Real local executor behavior only; no model/benchmark improvement claim.",
    }
    return payload


if __name__ == "__main__":
    results = asyncio.run(probe())
    Path(sys.argv[1]).write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(results, indent=2))
