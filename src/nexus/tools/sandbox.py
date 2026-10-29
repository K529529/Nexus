"""Lazy Windows SRT execution and bounded, sequential Bridge IPC."""

from __future__ import annotations

import asyncio
import base64
import codecs
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from nexus.core.security import SecurityPolicy
from nexus.core.types import Emit, ExecutionContext, Json, ToolCancelled, ToolResult, json_text
from nexus.tools.execution import OutputBuffer, _cleanup

MAX_FRAME = 5 * 1024 * 1024


class SRTBackend:
    """Lazy, conversation-owned SRT session. Never falls back to host execution."""

    def __init__(self, policy: SecurityPolicy | None = None) -> None:
        self.policy = policy or SecurityPolicy(mode="read-only")
        self.client: BridgeClient | None = None
        self.status = "not_initialized"
        self.detail = "SRT starts on the first restricted tool call."
        self.lock = asyncio.Lock()
        self.shell: str | None = None

    def installation(self, workspace: Path, shell: str) -> tuple[tuple[str, ...], Json]:
        if sys.platform != "win32":
            raise BridgeError("This SRT adapter currently supports Windows only.")
        roots = [workspace.resolve(), *(Path(p).resolve() for p in self.policy.writable_roots)]

        def trusted(value: str | Path) -> Path:
            path = Path(value).resolve(strict=True)
            if any(path.is_relative_to(root) for root in roots):
                raise BridgeError(
                    f"Untrusted installation inside a workspace/write root: {path}. "
                    "Install the Nexus wheel and Node/SRT outside authorized workspaces."
                )
            return path

        node_name = self.policy.node_path or shutil.which("node")
        if not node_name:
            raise BridgeError("Node.js >=22.12 is required; install it outside the workspace.")
        node = trusted(node_name)
        package = self.policy.srt_package
        if package is None:
            # Inspect global launchers, never execute npm or search workspace node_modules.
            candidates = [node.parent / "node_modules/@anthropic-ai/sandbox-runtime"]
            for folder in os.get_exec_path():
                base = Path(folder)
                if base.is_absolute() and any(
                    (base / n).is_file() for n in ("srt", "srt.cmd", "srt.ps1")
                ):
                    candidates.append(base / "node_modules/@anthropic-ai/sandbox-runtime")
            package = next((str(p) for p in candidates if p.is_dir()), None)
        if not package:
            raise BridgeError(
                "SRT missing. Install npm install -g @anthropic-ai/sandbox-runtime@0.0.81; "
                "or set security.srt_package to its absolute global installation. "
                "Windows provisioning must be performed explicitly by the user."
            )
        package_path = trusted(package)
        bridge = trusted(Path(__file__).with_name("srt_bridge.mjs"))
        python = trusted(sys.executable)
        shell_path = trusted(shutil.which(shell) or shell)
        self.shell = str(shell_path)
        # Installation candidates, not unconditional ACL grants: the bridge's
        # filesystem policy excludes Windows-managed paths from BOTH ACL lists.
        read_roots = [
            trusted(sys.prefix),
            trusted(sys.base_prefix),
            trusted(Path(__file__).parents[1]),
            trusted(shell_path.parent),
        ]
        return (str(node), str(bridge), str(package_path)), {
            "mode": self.policy.mode,
            "workspace": str(workspace.resolve()),
            "python": str(python),
            "writable_roots": list(self.policy.writable_roots),
            "read_roots": list(dict.fromkeys(str(p) for p in read_roots)),
            "allowed_domains": list(self.policy.allowed_domains),
            "denied_domains": list(self.policy.denied_domains),
        }

    async def execute(
        self,
        name: str,
        arguments: Json,
        context: ExecutionContext,
        emit: Emit,
    ) -> ToolResult:
        async with self.lock:
            if self.status in {"failed", "closed"}:
                return ToolResult(
                    context.call_id,
                    False,
                    {
                        "detail": self.detail,
                        "execution_state": "not_executed",
                    },
                    "backend_unavailable",
                )
            try:
                if self.client is None:
                    argv, params = self.installation(context.workspace, context.shell)
                    self.client = BridgeClient(argv, cwd=Path(argv[1]).parent)
                    await self.client.initialize(params)
                    self.status, self.detail = (
                        "ready",
                        "SRT initialized; Windows guarantees are experimental.",
                    )
                result = await self.client.execute(
                    name, arguments, replace(context, shell=self.shell or context.shell), emit
                )
                result = await self.finish_stop(result)
                if result.error_code == "cleanup_incomplete" or (
                    self.client and self.client.broken
                ):
                    self.status, self.detail = (
                        "failed",
                        "Previous execution cleanup is unconfirmed.",
                    )
                return result
            except ToolCancelled as exc:
                result = await self.finish_stop(exc.result)
                if exc.result.data.get("cleanup_incomplete") or (
                    self.client and self.client.broken
                ):
                    self.status, self.detail = (
                        "failed",
                        "Cancelled execution cleanup is unconfirmed.",
                    )
                raise ToolCancelled(result) from None
            except (OSError, ValueError, BridgeError, TimeoutError) as exc:
                self.status, self.detail = "failed", str(exc) or type(exc).__name__
                if self.client:
                    await self.client.close()
                return ToolResult(
                    context.call_id,
                    False,
                    {
                        "detail": self.detail,
                        "execution_state": "not_executed",
                    },
                    "backend_unavailable",
                )

    async def finish_stop(self, result: ToolResult) -> ToolResult:
        if result.error_code == "cleanup_incomplete" and self.client is not None:
            # An interrupted patch may lack a worker result. End the SRT holder
            # now even when no requires_reset frame arrived; preserve unknown facts.
            await self.client.close()
            self.status, self.detail = "failed", "Interrupted execution result is unconfirmed."
            return result
        if result.data.get("requires_reset") and self.client is not None:
            await self.client.close()
            if not self.client.cleanup_confirmed:
                self.status, self.detail = "failed", "SRT cancellation holder exit unconfirmed."
                return replace(
                    result,
                    ok=False,
                    error_code="cleanup_incomplete",
                    data={**result.data, "cleanup_incomplete": True},
                )
            self.client = None
            self.status, self.detail = (
                "not_initialized",
                "Cancelled SRT session closed; next call initializes a new session.",
            )
        return result

    async def close(self) -> None:
        if self.client:
            await self.client.close()
            if not self.client.cleanup_confirmed:
                self.status, self.detail = (
                    "failed",
                    "SRT reset/exit could not be confirmed. " + self.client.stderr,
                )
                raise OSError(self.detail)
        self.status = "closed"

    async def execute_patch_once(
        self,
        arguments: Json,
        context: ExecutionContext,
        emit: Emit,
    ) -> ToolResult:
        """A user-approved structured patch, never shell or persistent access."""
        async with self.lock:
            if self.policy.mode != "read-only" or self.status in {"failed", "closed"}:
                return ToolResult(context.call_id, False, {}, "dynamic_scope_unsupported")
            if self.client:
                await self.client.close()
                if not self.client.cleanup_confirmed:
                    self.status = "failed"
                    return ToolResult(
                        context.call_id, False, {"cleanup_incomplete": True}, "cleanup_incomplete"
                    )
                self.client = None
            one_shot = SRTBackend(replace(self.policy, mode="workspace-write", writable_roots=()))
            result: ToolResult | None = None
            try:
                result = await one_shot.execute("apply_patch", arguments, context, emit)
            finally:
                try:
                    await one_shot.close()
                except OSError:
                    self.status, self.detail = "failed", "One-shot reset is unconfirmed."
                    raise
                if one_shot.client and not one_shot.client.cleanup_confirmed:
                    self.status, self.detail = "failed", "One-shot reset is unconfirmed."
                else:
                    self.status, self.detail = (
                        "not_initialized",
                        "One-shot closed; read-only restored.",
                    )
            assert result is not None
            if self.status == "failed":
                return replace(
                    result,
                    ok=False,
                    error_code="cleanup_incomplete",
                    data={**result.data, "cleanup_incomplete": True},
                )
            return result

    async def request_write_access(
        self,
        paths: tuple[str, ...],
        context: ExecutionContext,
    ) -> ToolResult:
        """Arbitrary directory grants are unsupported; never elevate to Local."""
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
    """Production transport, also used with explicitly injected test adapters.

    The owner must close this object. There is no automatic retry after dispatch.
    Frame IDs are independent of model call IDs and never reused across runs.
    """

    def __init__(self, argv: tuple[str, ...], *, cwd: Path) -> None:
        self.argv, self.cwd = argv, cwd
        self.process: asyncio.subprocess.Process | None = None
        self.lock = asyncio.Lock()
        self.broken = False
        self.cleanup_confirmed = True
        self.stderr = ""
        self.stderr_task: asyncio.Task[None] | None = None

    async def drain_stderr(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        stream = self.process.stderr
        while chunk := await stream.read(4096):
            self.stderr = (self.stderr + chunk.decode("utf-8", errors="replace"))[-4000:]

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
                stderr=asyncio.subprocess.PIPE,
                limit=MAX_FRAME,
                **options,
            )
        )
        try:
            self.process = await asyncio.shield(spawn)
            self.stderr_task = asyncio.create_task(self.drain_stderr())
        except asyncio.CancelledError:
            self.process = await asyncio.shield(spawn)
            self.stderr_task = asyncio.create_task(self.drain_stderr())
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
                # SRT's first Windows persistent grant may walk installation trees;
                # its official grant API allows ten minutes. This is initialization,
                # not the model command's execution budget, and is never retried.
                async with asyncio.timeout(600):
                    await self.start()
                    ident = uuid4().hex
                    await self.send({"id": ident, "method": "initialize", "params": params})
                    value = await self.receive(ident)
                    if (
                        value.get("type") != "result"
                        or value.get("result", {}).get("initialized") is not True
                    ):
                        raise BridgeError(
                            f"backend_unavailable: {value.get('code')}: {value.get('detail', '')}"
                        )
            except TimeoutError:
                await self.close()
                raise BridgeError(
                    "SRT initialization exceeded 600 seconds; no tool dispatched."
                ) from None
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
                            or row.get("kind") not in {
                                "patch_diff", "patch_committed", "patch_manifest"}
                            or row["data"].get("call_id") != context.call_id
                        ):
                            raise BridgeError("invalid_worker_event")
                        await emit(row["kind"], row["data"])
                        await self.send({"id": ident, "method": "ack",
                                         "index": row["data"]["file_index"],
                                         "event_kind": row["kind"]})
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
                            {
                                "execution_state": row.get("state", "unknown"),
                                "detail": row.get("detail", ""),
                                **output.output(),
                            },
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
                            "audit_before_write": True,
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
        self.cleanup_confirmed = False
        if process.returncode is None:
            try:
                async with asyncio.timeout(120):
                    await self.send({"id": uuid4().hex, "method": "shutdown"})
                    await process.wait()
            except (OSError, TimeoutError):
                await _cleanup(process)
        self.cleanup_confirmed = process.returncode == 0
        if self.stderr_task is not None:
            try:
                async with asyncio.timeout(2):
                    await self.stderr_task
            except (OSError, TimeoutError):
                self.cleanup_confirmed = False
        self.process = None
