"""Trusted local shell with shared bounded output and process-tree cleanup."""

from __future__ import annotations

import asyncio
import codecs
import os
import signal
import subprocess
import sys
import time
from collections import deque
from pathlib import Path

from nexus.core.types import Emit, ExecutionContext, Json, ToolCancelled, ToolResult


class OutputBuffer:
    """Byte-bounded head/tail, shared by both pipes (not split per stream)."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.head: list[tuple[str, bytes]] = []
        self.tail: deque[tuple[str, bytes]] = deque()
        self.head_size = 0
        self.tail_size = 0
        self.total = {"stdout": 0, "stderr": 0}

    def add(self, stream: str, data: bytes) -> None:
        self.total[stream] += len(data)
        count = min(len(data), self.limit // 2 - self.head_size)
        if count:
            self.head.append((stream, data[:count]))
            self.head_size += count
        data = data[count:]
        if data:
            self.tail.append((stream, data))
            self.tail_size += len(data)
        while self.tail_size > self.limit - self.head_size:
            name, first = self.tail.popleft()
            excess = self.tail_size - (self.limit - self.head_size)
            removed = min(excess, len(first))
            self.tail_size -= removed
            if removed < len(first):
                self.tail.appendleft((name, first[removed:]))

    def output(self) -> Json:
        data: Json = {}
        discarded: dict[str, int] = {}
        invalid = False
        for name in ("stdout", "stderr"):
            head = b"".join(b for s, b in self.head if s == name)
            tail = b"".join(b for s, b in self.tail if s == name)
            discarded[name] = self.total[name] - len(head) - len(tail)
            parts = [head, tail] if discarded[name] else [head + tail]
            decoded: list[str] = []
            for part in parts:
                try:
                    decoded.append(part.decode("utf-8"))
                except UnicodeDecodeError:
                    invalid = True
                    decoded.append(part.decode("utf-8", errors="replace"))
            data[name] = "\n[... output truncated ...]\n".join(decoded)
        data["discarded_bytes"] = discarded
        data["truncated"] = any(discarded.values())
        data["decode_replaced"] = invalid
        return data


async def _cleanup(process: asyncio.subprocess.Process) -> bool:
    confirmed = True
    try:
        if sys.platform == "win32":
            if process.returncode is None:
                killer = await asyncio.create_subprocess_exec(
                    "taskkill",
                    "/PID",
                    str(process.pid),
                    "/T",
                    "/F",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                async with asyncio.timeout(2):
                    await killer.wait()
                if killer.returncode != 0 and process.returncode is None:
                    process.kill()
                    confirmed = False
        else:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                return True
            await asyncio.sleep(0.15)
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        async with asyncio.timeout(2):
            await process.wait()
        return confirmed
    except (OSError, TimeoutError):
        return False


async def execute(arguments: Json, context: ExecutionContext, emit: Emit) -> ToolResult:
    started = time.monotonic()
    command = arguments.get("command")
    workdir = arguments.get("workdir", ".")
    timeout = arguments.get("timeout_ms", 120000)
    if (
        arguments.keys() - {"command", "workdir", "timeout_ms"}
        or not isinstance(command, str)
        or not command.strip()
        or not isinstance(workdir, str)
        or not workdir
        or type(timeout) is not int
        or not 1 <= timeout <= 600000
    ):
        return ToolResult(
            context.call_id,
            False,
            {"detail": "Invalid exec_command arguments"},
            "invalid_arguments",
        )
    cwd = (context.workspace / Path(workdir)).resolve()
    data: Json = {
        "command": command,
        "cwd": str(cwd),
        "shell": context.shell,
        "exit_code": None,
        "stdout": "",
        "stderr": "",
        "timed_out": False,
        "cancelled": False,
        "truncated": False,
        "cleanup_incomplete": False,
    }
    if not cwd.is_dir():
        return ToolResult(context.call_id, False, data, "invalid_workdir")
    spawn_options: Json
    if sys.platform == "win32":
        argv = [
            context.shell,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new(); "
            "$OutputEncoding = [Console]::OutputEncoding; " + command,
        ]
        spawn_options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    else:
        argv = [context.shell, "-c", command]
        spawn_options = {"start_new_session": True}
    output = OutputBuffer(context.output_limit_bytes)
    streamed = 0

    async def drain(pipe: asyncio.StreamReader, name: str) -> None:
        nonlocal streamed
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while block := await pipe.read(8192):
            output.add(name, block)
            available = max(0, context.output_limit_bytes - streamed)
            visible = block[:available]
            streamed += len(visible)
            text = decoder.decode(visible)
            if text:
                await emit(
                    "tool_output_delta",
                    {
                        "call_id": context.call_id,
                        "stream": name,
                        "text": text,
                    },
                )
        last = decoder.decode(b"", final=True)
        if last:
            await emit(
                "tool_output_delta",
                {
                    "call_id": context.call_id,
                    "stream": name,
                    "text": last,
                },
            )

    process: asyncio.subprocess.Process | None = None
    readers: list[asyncio.Task[None]] = []
    error: str | None = None
    # Shield process creation so a cancellation during spawn cannot orphan it.
    spawn = asyncio.create_task(
        asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **spawn_options,
        )
    )
    try:
        async with asyncio.timeout(timeout / 1000):
            process = await asyncio.shield(spawn)
            assert process.stdout is not None and process.stderr is not None
            readers = [
                asyncio.create_task(drain(process.stdout, "stdout")),
                asyncio.create_task(drain(process.stderr, "stderr")),
            ]
            await process.wait()
            async with asyncio.timeout(2):
                await asyncio.gather(*readers)
    except (TimeoutError, asyncio.CancelledError) as exc:
        data["cancelled"] = isinstance(exc, asyncio.CancelledError)
        data["timed_out"] = not data["cancelled"]
        error = "cancelled" if data["cancelled"] else "command_timeout"
        if process is None:
            try:
                async with asyncio.timeout(2):
                    process = await asyncio.shield(spawn)
            except (OSError, TimeoutError):
                data["cleanup_incomplete"] = True
        if process is not None:
            data["cleanup_incomplete"] = not await _cleanup(process)
    except OSError:
        error = "command_start_failed"
    except BaseException:
        if process is not None:
            await _cleanup(process)
        raise
    finally:
        if readers:
            try:
                async with asyncio.timeout(2):
                    await asyncio.gather(*readers)
            except (TimeoutError, asyncio.CancelledError):
                data["cleanup_incomplete"] = True
                for reader in readers:
                    reader.cancel()
                await asyncio.gather(*readers, return_exceptions=True)
                if process is not None:
                    # asyncio exposes no public pipe-close method on Process. On an
                    # unconfirmed cleanup, close local handles without claiming child exit.
                    process._transport.close()  # type: ignore[attr-defined]
    data.update(output.output())
    data["exit_code"] = process.returncode if process else None
    data["duration_ms"] = int((time.monotonic() - started) * 1000)
    if data["cleanup_incomplete"]:
        error = "cleanup_incomplete"
    elif error is None and data["exit_code"] != 0:
        error = "command_exit_nonzero"
    result = ToolResult(
        context.call_id, error is None, data, error, data["duration_ms"], data["truncated"]
    )
    if data["cancelled"]:
        raise ToolCancelled(result)
    return result
