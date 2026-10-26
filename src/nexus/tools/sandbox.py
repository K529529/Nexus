"""Restricted backend acceptance gate and bounded, sequential Bridge IPC client."""

from __future__ import annotations

import asyncio
import base64
import codecs
import json
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

from nexus.core.types import Emit, ExecutionContext, Json, ToolCancelled, ToolResult, json_text
from nexus.tools.execution import OutputBuffer, _cleanup

BLOCKED_REASON = "Windows SRT isolation acceptance is unresolved; no local fallback is permitted."
MAX_FRAME = 5 * 1024 * 1024


class SRTBackend:
    """Production entry: no configurable escape hatch until OS acceptance is complete."""

    async def execute(
        self,
        name: str,
        arguments: Json,
        context: ExecutionContext,
        emit: Emit,
    ) -> ToolResult:
        return ToolResult(
            context.call_id,
            False,
            {
                "detail": BLOCKED_REASON,
                "execution_state": "not_executed",
                "backend": "srt",
                "isolation_verified": False,
            },
            "backend_blocked",
        )

    async def request_write_access(
        self,
        paths: tuple[str, ...],
        context: ExecutionContext,
    ) -> ToolResult:
        """Reserved for future per-call scope expansion; never elevate to Local."""
        return ToolResult(
            context.call_id,
            False,
            {
                "execution_state": "not_executed",
                "detail": "Dynamic filesystem grants are not supported.",
            },
            "dynamic_scope_unsupported",
        )


class BridgeError(RuntimeError):
    pass


class BridgeClient:
    """Engineering transport, injected by tests; production SRTBackend stays closed.

    The owner must close this object. There is no automatic retry after dispatch.
    Frame IDs are independent of model call IDs and never reused across runs.
    """

    def __init__(self, argv: tuple[str, ...], *, cwd: Path) -> None:
        self.argv, self.cwd = argv, cwd
        self.process: asyncio.subprocess.Process | None = None
        self.lock = asyncio.Lock()
        self.broken = False

    async def start(self) -> None:
        if self.broken:
            raise BridgeError("bridge_closed")
        if self.process is not None:
            return
        options: Json = (
            {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
            if sys.platform == "win32"
            else {"start_new_session": True}
        )
        spawn = asyncio.create_task(
            asyncio.create_subprocess_exec(
                *self.argv,
                cwd=self.cwd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                limit=MAX_FRAME,
                **options,
            )
        )
        try:
            self.process = await asyncio.shield(spawn)
        except asyncio.CancelledError:
            self.process = await asyncio.shield(spawn)
            await self.close()
            raise

    async def send(self, value: Json) -> None:
        assert self.process is not None and self.process.stdin is not None
        raw = (json_text(value) + "\n").encode()
        if len(raw) > MAX_FRAME:
            raise BridgeError("frame_too_large")
        self.process.stdin.write(raw)
        await self.process.stdin.drain()

    async def receive(self, ident: str) -> Json:
        assert self.process is not None and self.process.stdout is not None
        line = await self.process.stdout.readline()
        if not line:
            raise BridgeError("bridge_disconnected")
        value = json.loads(line)
        if not isinstance(value, dict) or value.get("id") != ident:
            raise BridgeError("bridge_correlation_error")
        return value

    async def initialize(self, params: Json) -> None:
        async with self.lock:
            try:
                async with asyncio.timeout(10):
                    await self.start()
                    ident = uuid4().hex
                    await self.send({"id": ident, "method": "initialize", "params": params})
                    value = await self.receive(ident)
                    if value.get("type") != "result":
                        raise BridgeError("backend_unavailable")
            except BaseException:
                await self.close()
                raise

    async def execute(
        self,
        name: str,
        arguments: Json,
        context: ExecutionContext,
        emit: Emit,
    ) -> ToolResult:
        async with self.lock:
            if self.broken or self.process is None:
                return ToolResult(context.call_id, False, {}, "backend_unavailable")
            ident = uuid4().hex
            output = OutputBuffer(context.output_limit_bytes)
            decoders = {
                stream: codecs.getincrementaldecoder("utf-8")(errors="replace")
                for stream in ("stdout", "stderr")
            }
            streamed = 0
            started = time.monotonic()
            dispatched = False

            async def collect() -> ToolResult:
                nonlocal streamed
                while True:
                    row = await self.receive(ident)
                    kind = row.get("type")
                    if kind == "output":
                        stream = row["stream"]
                        if stream not in {"stdout", "stderr"}:
                            raise BridgeError("invalid_stream")
                        raw = base64.b64decode(row["data"], validate=True)
                        output.add(stream, raw)
                        visible = raw[: max(0, context.output_limit_bytes - streamed)]
                        streamed += len(visible)
                        text = decoders[stream].decode(visible)
                        if text:
                            await emit(
                                "tool_output_delta",
                                {
                                    "call_id": context.call_id,
                                    "stream": stream,
                                    "text": text,
                                },
                            )
                    elif kind == "event":
                        if (
                            name != "apply_patch"
                            or row.get("kind") != "patch_diff"
                            or row["data"].get("call_id") != context.call_id
                        ):
                            raise BridgeError("invalid_worker_event")
                        await emit("patch_diff", row["data"])
                        await self.send({"id": ident, "method": "ack"})
                    elif kind == "result":
                        for stream, decoder in decoders.items():
                            tail = decoder.decode(b"", final=True)
                            if tail:
                                await emit(
                                    "tool_output_delta",
                                    {"call_id": context.call_id, "stream": stream, "text": tail},
                                )
                        if name == "apply_patch":
                            result = ToolResult(**row["result"])
                            if (
                                result.call_id != context.call_id
                                or type(result.ok) is not bool
                                or not isinstance(result.data, dict)
                                or result.ok != (result.error_code is None)
                            ):
                                raise BridgeError("invalid_worker_result")
                            return result
                        result_data = row["result"]
                        required = {
                            "exit_code",
                            "error_code",
                            "timed_out",
                            "cancelled",
                            "cleanup_incomplete",
                        }
                        if (
                            not isinstance(result_data, dict)
                            or required - result_data.keys()
                            or any(
                                type(result_data[key]) is not bool
                                for key in ("timed_out", "cancelled", "cleanup_incomplete")
                            )
                            or (
                                result_data["error_code"] is None
                                and (
                                    result_data["exit_code"] != 0
                                    or any(
                                        result_data[key]
                                        for key in ("timed_out", "cancelled", "cleanup_incomplete")
                                    )
                                )
                            )
                        ):
                            raise BridgeError("invalid_shell_result")
                        data = {**row["result"], **output.output()}
                        error = data.get("error_code")
                        return ToolResult(
                            context.call_id,
                            error is None,
                            data,
                            error,
                            int((time.monotonic() - started) * 1000),
                            data["truncated"],
                        )
                    elif kind == "error":
                        return ToolResult(
                            context.call_id,
                            False,
                            {"execution_state": row.get("state", "unknown")},
                            row.get("code", "bridge_error"),
                        )
                    else:
                        raise BridgeError("invalid_frame")

            reader: asyncio.Task[ToolResult] | None = None
            try:
                timeout = arguments.get("timeout_ms", 120000)
                if type(timeout) is not int or not 1 <= timeout <= 600000:
                    return ToolResult(context.call_id, False, {}, "invalid_arguments")
                dispatched = True  # A failed drain may still have delivered the request.
                await self.send(
                    {
                        "id": ident,
                        "method": "execute",
                        "params": {
                            "name": name,
                            "arguments": arguments,
                            "call_id": context.call_id,
                            "workspace": str(context.workspace),
                            "shell": context.shell,
                            "output_limit_bytes": context.output_limit_bytes,
                        },
                    }
                )
                reader = asyncio.create_task(collect())
                async with asyncio.timeout(timeout / 1000 + 5):
                    return await asyncio.shield(reader)
            except (asyncio.CancelledError, TimeoutError) as exc:
                try:
                    async with asyncio.timeout(4):
                        await self.send({"id": ident, "method": "cancel"})
                        assert reader is not None
                        value = await asyncio.shield(reader)
                except (Exception, asyncio.CancelledError):
                    await self.close()
                    value = ToolResult(
                        context.call_id,
                        False,
                        {"execution_state": "interrupted_unknown", "cleanup_incomplete": True},
                        "cleanup_incomplete",
                    )
                if isinstance(exc, asyncio.CancelledError):
                    raise ToolCancelled(value) from None
                return value
            except (OSError, ValueError, TypeError, KeyError, BridgeError):
                await self.close()
                return ToolResult(
                    context.call_id,
                    False,
                    {
                        "execution_state": "interrupted_unknown" if dispatched else "not_executed",
                        "cleanup_incomplete": dispatched,
                    },
                    "cleanup_incomplete" if dispatched else "backend_unavailable",
                )
            except BaseException:
                await self.close()
                raise
            finally:
                if reader is not None and not reader.done():
                    reader.cancel()
                    await asyncio.gather(reader, return_exceptions=True)

    async def close(self) -> None:
        self.broken = True
        if self.process is None:
            return
        process = self.process
        if process.returncode is None:
            try:
                async with asyncio.timeout(3):
                    await self.send({"id": uuid4().hex, "method": "shutdown"})
                    await process.wait()
            except (OSError, TimeoutError):
                await _cleanup(process)
        self.process = None
