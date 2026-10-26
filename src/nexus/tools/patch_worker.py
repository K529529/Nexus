"""One sandboxed patch invocation, using the installed patch core and audited IPC.

Launch as ``python -I /trusted/install/nexus/tools/patch_worker.py``. Never import
Nexus from the target workspace. An event acknowledgement is a persistence barrier.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

MAX_REQUEST = 4 * 1024 * 1024
TRUSTED_ROOT = str(Path(__file__).resolve().parents[2])


def read_request() -> bytes:
    if len(sys.argv) >= 3 and sys.argv[1] == "--request-file":
        with Path(sys.argv[2]).open("rb") as stream:
            return stream.read(MAX_REQUEST + 1)
    return sys.stdin.buffer.readline(MAX_REQUEST + 1)


def wait_ack(path: str, ident: str, index: int) -> None:
    while True:
        try:
            ack = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            ack = {}
        if ack.get("id") == ident:
            if ack.get("method") == "cancel":
                raise asyncio.CancelledError
            if ack.get("method") == "ack" and ack.get("index") == index:
                return
        time.sleep(0.01)


async def main() -> None:
    # -I removes cwd/PYTHONPATH/user site. This root comes from this trusted script.
    sys.path.insert(0, TRUSTED_ROOT)
    from nexus.core.types import ExecutionContext, Json, ToolCancelled, json_text
    from nexus.tools.patch import apply_patch

    def send(value: Json) -> None:
        sys.stdout.buffer.write((json_text(value) + "\n").encode("utf-8"))
        sys.stdout.buffer.flush()

    raw = read_request()
    if len(raw) > MAX_REQUEST or not raw.endswith(b"\n"):
        raise ValueError("Invalid request size")
    request = json.loads(raw)
    ident = request["id"]
    context = ExecutionContext(
        Path(request["workspace"]), request["call_id"], "", request["output_limit_bytes"]
    )

    async def emit(kind: str, data: Json, **_: object) -> int:
        prewrite = request.get("audit_before_write") is True
        if kind != "patch_diff" and not (prewrite and kind == "patch_committed"):
            raise ValueError("Unexpected worker event")
        send({"id": ident, "type": "event", "kind": kind, "data": data})
        if request.get("ack_path"):
            wait_ack(request["ack_path"], ident, data["file_index"])
            return 0
        # This is a one-call process. Blocking for the host ACK avoids a Windows
        # asyncio loop's loopback socketpair, without weakening SRT networking.
        line = sys.stdin.buffer.readline(4096)
        ack = json.loads(line) if line else {}
        if ack.get("id") != ident or ack.get("method") != "ack":
            raise asyncio.CancelledError
        if prewrite and (ack.get("index") != data["file_index"] or ack.get("event_kind") != kind):
            raise asyncio.CancelledError
        return 0

    try:
        result = await apply_patch(
            request["arguments"],
            context,
            emit,
            audit_before_write=request.get("audit_before_write") is True,
        )
        cancelled = False
    except ToolCancelled as exc:
        result, cancelled = exc.result, True
    send({"id": ident, "type": "result", "result": asdict(result), "cancelled": cancelled})


if __name__ == "__main__":
    # Patch Core only awaits emit(), which completes synchronously in this worker.
    # Drive that coroutine without creating sockets or an OS event loop. A future
    # asynchronous dependency must fail closed here, never silently lose an await.
    operation = main()
    try:
        operation.send(None)
    except StopIteration:
        pass
    else:
        raise RuntimeError("Patch Worker yielded an unsupported asynchronous operation")
    finally:
        operation.close()
