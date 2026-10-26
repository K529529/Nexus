"""One sandboxed patch invocation, using the installed patch core and audited IPC.

Launch as ``python -I /trusted/install/nexus/tools/patch_worker.py``. Never import
Nexus from the target workspace. An event acknowledgement is a persistence barrier.
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path

MAX_REQUEST = 4 * 1024 * 1024
TRUSTED_ROOT = str(Path(__file__).resolve().parents[2])


async def main() -> None:
    # -I removes cwd/PYTHONPATH/user site. This root comes from this trusted script.
    sys.path.insert(0, TRUSTED_ROOT)
    from nexus.core.types import ExecutionContext, Json, ToolCancelled, json_text
    from nexus.tools.patch import apply_patch

    def send(value: Json) -> None:
        sys.stdout.buffer.write((json_text(value) + "\n").encode("utf-8"))
        sys.stdout.buffer.flush()

    raw = sys.stdin.buffer.readline(MAX_REQUEST + 1)
    if len(raw) > MAX_REQUEST or not raw.endswith(b"\n"):
        raise ValueError("Invalid request size")
    request = json.loads(raw)
    ident = request["id"]
    context = ExecutionContext(
        Path(request["workspace"]), request["call_id"], "", request["output_limit_bytes"]
    )

    async def emit(kind: str, data: Json, **_: object) -> int:
        if kind != "patch_diff":
            raise ValueError("Unexpected worker event")
        send({"id": ident, "type": "event", "kind": kind, "data": data})
        line = await asyncio.to_thread(sys.stdin.buffer.readline, 4096)
        ack = json.loads(line) if line else {}
        if ack.get("id") != ident or ack.get("method") != "ack":
            raise asyncio.CancelledError
        return 0

    try:
        result = await apply_patch(request["arguments"], context, emit)
        cancelled = False
    except ToolCancelled as exc:
        result, cancelled = exc.result, True
    send({"id": ident, "type": "result", "result": asdict(result), "cancelled": cancelled})


if __name__ == "__main__":
    asyncio.run(main())
