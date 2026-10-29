"""Linux session containers: argv-only Docker transport, durable patch ACKs, fail closed.

Design reference: yandy/pi-packages pi-container-sandbox (MIT). No upstream code copied.
Only the owning live backend can remove its exact container; doctor only diagnoses orphans.
"""

from __future__ import annotations

import asyncio
import base64
import codecs
import json
import os
import shutil
import sys
import time
from dataclasses import replace
from pathlib import Path, PurePosixPath
from uuid import uuid4

from nexus.core.security import SecurityPolicy
from nexus.core.types import Emit, ExecutionContext, Json, ToolCancelled, ToolResult, json_text
from nexus.tools.docker_image import LABEL, PROTOCOL, default_image, fingerprint
from nexus.tools.execution import (
    COMMAND_ARGUMENTS_DETAIL,
    OutputBuffer,
    normalize_command_arguments,
)

MAX_FRAME = 4 * 1024 * 1024


class DockerError(OSError):
    pass


def linux_ids() -> tuple[int, int]:
    if sys.platform != "linux":
        raise DockerError("Linux user identity unavailable")
    return os.getuid(), os.getgid()


def validate_policy(policy: SecurityPolicy) -> None:
    if sys.platform != "linux":
        raise DockerError("Linux Docker requires Linux/WSL2 Ubuntu; no Local fallback")
    if linux_ids()[0] == 0 or linux_ids()[1] == 0:
        raise DockerError("Run Nexus as a non-root Linux user with Docker access")
    if policy.writable_roots:
        raise DockerError("Linux Docker V1 does not support extra writable_roots")
    if policy.domain_policy_configured or policy.allowed_domains or policy.denied_domains:
        raise DockerError("Linux Docker supports network bridge/none, not domain policies")
    if policy.node_path or policy.srt_package or policy.windows_koffi or policy.windows_acl_roots:
        raise DockerError("Remove Windows/SRT-only security fields from Linux user configuration")


def validate_workspace(workspace: Path) -> Path:
    absolute = workspace.absolute()
    if any(p.is_symlink() for p in (absolute, *absolute.parents)):
        raise DockerError("Workspace symlink/ancestor is not an eligible bind source")
    root = workspace.resolve(strict=True)
    protected = [
        Path(p)
        for p in (
            "/etc",
            "/proc",
            "/sys",
            "/dev",
            "/run",
            "/boot",
            "/root",
            "/usr",
            "/var/lib/docker",
        )
    ]
    home = Path.home().resolve()
    protected.extend(
        home / p
        for p in (".nexus", ".ssh", ".aws", ".azure", ".config", ".docker", ".kube", ".codex")
    )
    if (
        not root.is_dir()
        or root == Path("/")
        or home.is_relative_to(root)
        or root in {Path("/tmp"), Path("/var/tmp"), Path("/mnt")}
        or any(root.is_relative_to(p) or p.is_relative_to(root) for p in protected)
        or any(c in str(root) for c in (",", "\n", "\r"))
    ):
        raise DockerError(f"Unsafe workspace bind root: {root}")
    install = Path(__file__).resolve().parents[1]
    if install.is_relative_to(root):
        raise DockerError("Trusted Nexus installation must be outside the writable workspace")
    return root


def process_identity(pid: int) -> str:
    try:
        # comm can contain spaces or closing parentheses; starttime is field 22.
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return "unknown"


class DockerCLI:
    def __init__(self) -> None:
        self.executable = shutil.which("docker") or "docker"
        self.env = {
            k: v
            for k, v in os.environ.items()
            if k
            in {
                "PATH",
                "HOME",
                "LANG",
                "LC_ALL",
                "XDG_RUNTIME_DIR",
                "DOCKER_HOST",
                "DOCKER_CONTEXT",
                "DOCKER_CONFIG",
                "DOCKER_TLS_VERIFY",
                "DOCKER_CERT_PATH",
            }
        }

    async def spawn(self, args: list[str], *, stdin: bool = False) -> asyncio.subprocess.Process:
        return await asyncio.create_subprocess_exec(
            self.executable,
            *args,
            stdin=asyncio.subprocess.PIPE if stdin else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self.env,
            cwd="/",
            limit=MAX_FRAME,
        )

    async def run(self, args: list[str], *, timeout: float = 30) -> str:  # noqa: ASYNC109
        # Shield process creation: cancellation must not lose a just-spawned CLI handle.
        spawn = asyncio.create_task(self.spawn(args))
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.shield(spawn)
            async with asyncio.timeout(timeout):
                out, err = await proc.communicate()
            if proc.returncode:
                raise DockerError(
                    f"docker {args[0]} failed: " + err.decode(errors="replace")[-2000:]
                )
            return out.decode("utf-8")
        finally:
            if proc is None:
                proc = await spawn
            if proc.returncode is None:
                proc.kill()
                await proc.communicate()

    async def inspect(self, ident: str) -> Json | None:
        # Listing distinguishes absence from daemon errors. Never interpret all errors as absent.
        listing = await self.run(
            ["container", "ls", "-aq", "--no-trunc", "--filter", "id=" + ident]
        )
        if not listing.strip():
            return None
        values = json.loads(await self.run(["container", "inspect", ident]))
        return dict(values[0])

    async def environment(self) -> Json:
        endpoint = self.env.get("DOCKER_HOST") if not self.env.get("DOCKER_CONTEXT") else None
        if not endpoint:
            ctx = json.loads(await self.run(["context", "inspect"]))[0]
            endpoint = ctx["Endpoints"]["docker"]["Host"]
        if not endpoint.startswith("unix://"):
            raise DockerError("Only a local Unix Docker endpoint is supported")
        info: Json = json.loads(await self.run(["info", "--format", "{{json .}}"]))
        if info.get("OSType") != "linux":
            raise DockerError("A Linux Docker Engine is required")
        return {
            "endpoint": endpoint,
            "version": info.get("ServerVersion"),
            "os": info.get("OperatingSystem"),
        }


class LinuxDockerBackend:
    environment_display = ("Linux (Docker)", "/workspace")

    def __init__(self, policy: SecurityPolicy) -> None:
        self.policy = policy
        self.status = "not_initialized"
        self.detail = f"Linux Docker; container not initialized; Network: {policy.docker_network}"
        self.cli = DockerCLI()
        self.lock = asyncio.Lock()
        self.container_id: str | None = None
        self.name = "nexus-sbx-" + uuid4().hex
        self.labels: dict[str, str] = {}
        self.workspace: Path | None = None
        self.image_id = ""
        self.timings: dict[str, float] = {}
        self.possibly_created = False
        self.patch_timeout_seconds = 120.0

    async def image(self) -> str:
        ref = self.policy.docker_image or default_image()
        try:
            value = json.loads(await self.cli.run(["image", "inspect", ref]))[0]
        except DockerError as exc:
            raise DockerError(
                f"Sandbox image unavailable; run nexus sandbox setup. {exc}"
            ) from None
        labels = value.get("Config", {}).get("Labels") or {}
        if (
            labels.get(LABEL + ".code") != fingerprint()
            or labels.get(LABEL + ".protocol") != PROTOCOL
        ):
            raise DockerError("Sandbox image code/protocol mismatch; run nexus sandbox setup")
        if value["Config"].get("Volumes"):
            raise DockerError("Sandbox images may not declare implicit volumes")
        return str(value["Id"])

    def creation_args(self) -> list[str]:
        assert self.workspace is not None
        p = self.policy
        mount = f"type=bind,src={self.workspace},dst=/workspace,bind-recursive=disabled"
        if p.mode == "read-only":
            mount += ",readonly"
        args = [
            "create",
            "--pull=never",
            "--name",
            self.name,
            "--user",
            f"{linux_ids()[0]}:{linux_ids()[1]}",
            "--workdir",
            "/workspace",
            "--network",
            p.docker_network,
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--cpus",
            str(p.docker_cpus),
            "--memory",
            str(p.docker_memory_mb * 1024**2),
            "--memory-swap",
            str(p.docker_memory_mb * 1024**2),
            "--pids-limit",
            str(p.docker_pids),
            "--read-only",
            "--init",
            "--ipc",
            "private",
            "--cgroupns",
            "private",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=268435456,mode=1777",
            "--mount",
            mount,
            "--entrypoint",
            "/bin/sleep",
            "--no-healthcheck",
        ]
        for key, value in self.labels.items():
            args += ["--label", f"{key}={value}"]
        return [*args, self.image_id, "infinity"]

    def owns(self, value: Json) -> bool:
        return (
            value.get("Name") == "/" + self.name
            and bool(self.labels)
            and all(
                (value.get("Config", {}).get("Labels") or {}).get(k) == v
                for k, v in self.labels.items()
            )
            and (self.container_id is None or value.get("Id") == self.container_id)
        )

    def verify(self, value: Json) -> None:
        p = self.policy
        host, config = value["HostConfig"], value["Config"]
        binds = [m for m in value["Mounts"] if m["Type"] == "bind"]
        mount_options = host.get("Mounts") or []
        safe = (
            self.owns(value)
            and value["State"]["Running"]
            and value["Image"] == self.image_id
            and config["User"] == f"{linux_ids()[0]}:{linux_ids()[1]}"
            and host["Privileged"] is False
            and host["ReadonlyRootfs"] is True
            and host["CapDrop"] == ["ALL"]
            and not host.get("CapAdd")
            and "no-new-privileges" in host["SecurityOpt"]
            and host["NetworkMode"] == p.docker_network
            and host["PidMode"] == ""
            and host["IpcMode"] == "private"
            and host["CgroupnsMode"] == "private"
            and not host.get("Devices")
            and not host.get("Binds")
            and not host.get("VolumesFrom")
            and not host.get("PortBindings")
            and not host.get("ExtraHosts")
            and host["Memory"] == p.docker_memory_mb * 1024**2
            and host["MemorySwap"] == host["Memory"]
            and host["PidsLimit"] == p.docker_pids
            and host["NanoCpus"] == int(p.docker_cpus * 1_000_000_000)
            and len(binds) == 1
            and binds[0]["Source"] == str(self.workspace)
            and binds[0]["Destination"] == "/workspace"
            and binds[0]["RW"] == (p.mode == "workspace-write")
            and len(mount_options) == 1
            and mount_options[0].get("BindOptions", {}).get("NonRecursive") is True
            and all(
                m["Type"] == "bind" or (m["Type"] == "tmpfs" and m["Destination"] == "/tmp")
                for m in value["Mounts"]
            )
            and set(value["NetworkSettings"]["Networks"]) == {p.docker_network}
        )
        if not safe:
            raise DockerError("Container ownership/state/image/isolation verification failed")

    async def ready(self, workspace: Path) -> None:
        validate_policy(self.policy)
        root = await asyncio.to_thread(validate_workspace, workspace)
        if self.workspace is not None and root != self.workspace:
            raise DockerError("A session container cannot change its workspace")
        self.workspace = root
        if self.container_id:
            value = await self.cli.inspect(self.container_id)
            if value is None:
                raise DockerError("Session container disappeared; no automatic replay/recreation")
            self.verify(value)
            return
        executable = await asyncio.to_thread(Path(self.cli.executable).resolve)
        if executable.is_relative_to(root):
            raise DockerError("Docker CLI must be outside workspace")
        docker_config = self.cli.env.get("DOCKER_CONFIG")
        if docker_config and (await asyncio.to_thread(Path(docker_config).resolve)).is_relative_to(
            root
        ):
            raise DockerError("Trusted Docker configuration must be outside workspace")
        started = time.monotonic()
        await self.cli.environment()
        self.timings["engine_check_ms"] = (time.monotonic() - started) * 1000
        started = time.monotonic()
        self.image_id = await self.image()
        self.timings["image_check_ms"] = (time.monotonic() - started) * 1000
        self.labels = {
            LABEL: "v1",
            LABEL + ".session": self.name,
            LABEL + ".uid": str(linux_ids()[0]),
            LABEL + ".host": (await asyncio.to_thread(Path("/etc/machine-id").read_text)).strip(),
            LABEL + ".boot": (
                await asyncio.to_thread(Path("/proc/sys/kernel/random/boot_id").read_text)
            ).strip(),
            LABEL + ".pid": str(os.getpid()),
            LABEL + ".start": process_identity(os.getpid()),
            LABEL + ".workspace": str(root),
        }
        started = time.monotonic()
        self.possibly_created = True
        self.container_id = (await self.cli.run(self.creation_args(), timeout=60)).strip()
        await self.cli.run(["start", self.container_id], timeout=60)
        value = await self.cli.inspect(self.container_id)
        if value is None:
            raise DockerError("New session container is missing")
        self.verify(value)
        self.timings["container_start_ms"] = (time.monotonic() - started) * 1000
        self.status = "ready"
        self.detail = (
            f"Linux Docker; container {self.container_id[:12]}; "
            f"/workspace {self.policy.mode}; Network: {self.policy.docker_network}"
        )

    async def destroy(self) -> bool:
        started = time.monotonic()
        if not self.possibly_created:
            return True
        try:
            # A cancelled docker create may have succeeded before its ID was received.
            if not self.container_id:
                ids = await self.cli.run(
                    [
                        "container",
                        "ls",
                        "-aq",
                        "--no-trunc",
                        "--filter",
                        "name=^/" + self.name + "$",
                    ]
                )
                if not ids.strip():
                    self.possibly_created = False
                    return True
                self.container_id = ids.strip()
            value = await self.cli.inspect(self.container_id)
            if value is not None:
                if not self.owns(value):
                    raise DockerError("Refusing to delete a container with unconfirmed ownership")
                await self.cli.run(["rm", "--force", self.container_id])
            if await self.cli.inspect(self.container_id) is not None:
                raise DockerError("Container removal is unconfirmed")
            self.possibly_created = False
            self.container_id = None
            return True
        except (OSError, ValueError, TimeoutError) as exc:
            self.status, self.detail = "failed", f"cleanup_incomplete: {exc}; container {self.name}"
            return False
        finally:
            self.timings["cleanup_ms"] = (time.monotonic() - started) * 1000

    async def close(self) -> None:
        async with self.lock:
            if not await self.destroy():
                raise DockerError(self.detail)
            self.status = "closed"

    def exec_args(self, name: str, arguments: Json, shell: str) -> list[str]:
        assert self.container_id is not None
        args = [
            "exec",
            "-i",
            "--workdir",
            arguments.get("workdir", "/workspace"),
            self.container_id,
            "/usr/bin/env",
            "-i",
            "PATH=/usr/local/bin:/usr/bin:/bin",
            "HOME=/tmp",
            "TMPDIR=/tmp",
            "LANG=C.UTF-8",
            "PYTHONDONTWRITEBYTECODE=1",
            "PYTHONUNBUFFERED=1",
        ]
        if name == "apply_patch":
            return [*args, "/usr/local/bin/python", "-I", "/opt/nexus/tools/patch_worker.py"]
        return [*args, "/usr/local/bin/python", "-I", "/opt/nexus/tools/docker_shell.py"]

    async def execute(
        self, name: str, arguments: Json, context: ExecutionContext, emit: Emit
    ) -> ToolResult:
        async with self.lock:
            if self.status in {"failed", "closed"}:
                return ToolResult(
                    context.call_id,
                    False,
                    {"execution_state": "not_executed", "detail": self.detail},
                    "backend_unavailable",
                )
            if name not in {"exec_command", "apply_patch"}:
                return ToolResult(context.call_id, False, {}, "policy_denied")
            if name == "exec_command":
                if context.shell not in {"/bin/sh", "/bin/bash", "/usr/bin/bash", "/usr/bin/sh"}:
                    return ToolResult(
                        context.call_id,
                        False,
                        {"detail": "Linux Docker V1 supports sh/bash installed in the image"},
                        "invalid_arguments",
                    )
                arguments, _ = normalize_command_arguments(arguments)
                if (
                    arguments.keys() - {"command", "workdir", "timeout_ms"}
                    or not isinstance(arguments.get("command"), str)
                    or not arguments["command"].strip()
                    or not isinstance(arguments.get("workdir", "."), str)
                    or not arguments.get("workdir", ".")
                    or type(arguments.get("timeout_ms", 120000)) is not int
                    or not 1 <= arguments.get("timeout_ms", 120000) <= 600000
                ):
                    return ToolResult(
                        context.call_id,
                        False,
                        {"detail": COMMAND_ARGUMENTS_DETAIL},
                        "invalid_arguments",
                    )
                work = PurePosixPath(arguments.get("workdir", "."))
                if ".." in work.parts:
                    return ToolResult(
                        context.call_id,
                        False,
                        {"detail": "workdir cannot contain .."},
                        "invalid_arguments",
                    )
                arguments = {
                    **arguments,
                    "workdir": str(
                        work if work.is_absolute() else PurePosixPath("/workspace") / work
                    ),
                }
            return await self._execute(name, arguments, context, emit)

    async def _execute(
        self, name: str, arguments: Json, context: ExecutionContext, emit: Emit
    ) -> ToolResult:
        started = time.monotonic()
        ident = uuid4().hex
        process: asyncio.subprocess.Process | None = None
        spawn: asyncio.Task[asyncio.subprocess.Process] | None = None
        tasks: list[asyncio.Task[object]] = []
        output = OutputBuffer(context.output_limit_bytes)
        streamed = 0
        planned: list[Json] = []
        completed: list[Json] = []
        dispatched = False
        decoders = {
            channel: codecs.getincrementaldecoder("utf-8")(errors="replace")
            for channel in ("stdout", "stderr")
        }

        async def display(channel: str, chunk: bytes) -> None:
            nonlocal streamed
            output.add(channel, chunk)
            visible = chunk[: max(0, context.output_limit_bytes - streamed)]
            streamed += len(visible)
            text = decoders[channel].decode(visible)
            if text:
                await emit(
                    "tool_output_delta",
                    {
                        "call_id": context.call_id,
                        "stream": channel,
                        "text": text,
                    },
                )

        async def shell_result() -> ToolResult:
            assert process is not None and process.stdin is not None and process.stdout is not None
            request = {"id": ident, "shell": context.shell, "command": arguments["command"]}
            raw = (json_text(request) + "\n").encode()
            if len(raw) > MAX_FRAME:
                raise DockerError("Shell request too large")
            process.stdin.write(raw)
            await process.stdin.drain()
            while line := await process.stdout.readline():
                row = json.loads(line)
                if row.get("id") != ident:
                    raise DockerError("Shell frame belongs to a different call")
                if row.get("type") == "output" and row.get("stream") in decoders:
                    await display(row["stream"], base64.b64decode(row["data"], validate=True))
                elif row.get("type") == "result" and type(row.get("exit_code")) is int:
                    code = row["exit_code"]
                    return ToolResult(
                        context.call_id,
                        code == 0,
                        {
                            "exit_code": code,
                            "timed_out": False,
                            "cancelled": False,
                            "cleanup_incomplete": False,
                        },
                        None if code == 0 else "command_exit_nonzero",
                    )
                else:
                    raise DockerError("Invalid shell worker frame")
            raise DockerError("Shell exited without a confirmed completion frame")

        async def drain(stream: asyncio.StreamReader, channel: str) -> None:
            while chunk := await stream.read(4096):
                await display(channel, chunk)

        async def patch() -> ToolResult:
            assert process is not None and process.stdin is not None and process.stdout is not None
            request = {
                "id": ident,
                "call_id": context.call_id,
                "workspace": "/workspace",
                "arguments": arguments,
                "output_limit_bytes": context.output_limit_bytes,
                "audit_before_write": True,
            }
            data = (json_text(request) + "\n").encode()
            if len(data) > MAX_FRAME:
                raise DockerError("Patch request too large")
            process.stdin.write(data)
            await process.stdin.drain()
            while line := await process.stdout.readline():
                row = json.loads(line)
                if row.get("id") != ident:
                    raise DockerError("Patch frame belongs to a different call")
                if row.get("type") == "event":
                    kind, event = row.get("kind"), row["data"]
                    index = event.get("file_index")
                    if event.get("call_id") != context.call_id:
                        raise DockerError("Patch event call_id mismatch")
                    if kind == "patch_manifest":
                        if index != 0 or planned or completed:
                            raise DockerError("Invalid patch manifest sequence")
                    elif kind == "patch_diff":
                        if (
                            index != len(planned) + 1
                            or len(planned) != len(completed)
                            or event.get("phase") != "planned"
                        ):
                            raise DockerError("Invalid planned patch sequence")
                    elif kind == "patch_committed":
                        if (
                            index != len(completed) + 1
                            or index != len(planned)
                            or any(
                                event.get(k) != planned[-1].get(k)
                                for k in ("path", "status", "before_hash", "after_hash")
                            )
                        ):
                            raise DockerError("Invalid committed patch sequence")
                    else:
                        raise DockerError("Invalid patch event kind")
                    await emit(kind, event)  # Durable Events.append completes before ACK.
                    if kind != "patch_manifest":
                        (planned if kind == "patch_diff" else completed).append(event)
                    process.stdin.write(
                        (
                            json_text(
                                {"id": ident, "method": "ack", "index": index, "event_kind": kind}
                            )
                            + "\n"
                        ).encode()
                    )
                    await process.stdin.drain()
                elif row.get("type") == "result":
                    result = ToolResult(**row["result"])
                    if (
                        result.call_id != context.call_id
                        or type(result.ok) is not bool
                        or result.data.get("changed_files") != len(completed)
                    ):
                        raise DockerError("Invalid patch result")
                    return result
                else:
                    raise DockerError("Invalid patch worker frame")
            raise DockerError("Patch worker exited without a confirmed result")

        try:
            initialization = asyncio.create_task(self.ready(context.workspace))
            try:
                await asyncio.shield(initialization)
            except asyncio.CancelledError:
                # Let create/start finish (bounded Docker calls), then remove that exact ID.
                # Killing the CLI mid-create could leave an untracked delayed container.
                await asyncio.gather(initialization, return_exceptions=True)
                raise
            seconds = (
                self.patch_timeout_seconds
                if name == "apply_patch"
                else arguments.get("timeout_ms", 120000) / 1000
            )
            async with asyncio.timeout(seconds):
                dispatched = True
                spawn = asyncio.create_task(
                    self.cli.spawn(self.exec_args(name, arguments, context.shell), stdin=True)
                )
                process = await asyncio.shield(spawn)
                assert process.stdout is not None and process.stderr is not None
                stderr_task = asyncio.create_task(drain(process.stderr, "stderr"))
                tasks.append(stderr_task)
                if name == "exec_command":
                    result = await shell_result()
                else:
                    result = await patch()
                await stderr_task
                if await process.wait() != 0:
                    raise DockerError("Worker transport exited abnormally after result")
                for channel, decoder in decoders.items():
                    tail = decoder.decode(b"", final=True)
                    if tail:
                        await emit(
                            "tool_output_delta",
                            {
                                "call_id": context.call_id,
                                "stream": channel,
                                "text": tail,
                            },
                        )
                if name == "exec_command":
                    result.data.update(output.output())
                result.data.update(
                    {
                        "execution_backend": "linux-docker",
                        "container_id": self.container_id,
                        "container_workspace": "/workspace",
                        "image_id": self.image_id,
                        "network": self.policy.docker_network,
                    }
                )
                result.duration_ms = int((time.monotonic() - started) * 1000)
                result.truncated = bool(result.data.get("truncated", result.truncated))
                return result
        except (Exception, asyncio.CancelledError) as exc:
            cancelled = isinstance(exc, asyncio.CancelledError)
            timed_out = isinstance(exc, TimeoutError)
            # Remove the container, not just docker exec. No old process can survive confirmed rm.
            clean = await asyncio.shield(self.destroy())
            self.status = "failed"
            self.detail = (
                f"Linux Docker stopped after {type(exc).__name__}; open a new conversation. {exc}"
            )
            error = (
                "cleanup_incomplete"
                if not clean
                else "cancelled"
                if cancelled
                else "timeout"
                if timed_out
                else "backend_unavailable"
            )
            data = {
                "execution_backend": "linux-docker",
                "container_name": self.name,
                "image_id": self.image_id,
                "network": self.policy.docker_network,
                "execution_state": "interrupted_unknown" if dispatched else "not_executed",
                "cleanup_incomplete": not clean,
                "timed_out": timed_out,
                "cancelled": cancelled,
                "detail": self.detail,
                "files": completed,
                "changed_files": len(completed),
                "unknown_files": [p["path"] for p in planned[len(completed) :]],
                **output.output(),
            }
            result = ToolResult(
                context.call_id, False, data, error, int((time.monotonic() - started) * 1000)
            )
            if cancelled:
                raise ToolCancelled(result) from None
            return result
        finally:
            if process is None and spawn is not None:
                process = await spawn
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def execute_patch_once(
        self, arguments: Json, context: ExecutionContext, emit: Emit
    ) -> ToolResult:
        async with self.lock:
            if self.policy.mode != "read-only" or self.status in {"failed", "closed"}:
                return ToolResult(context.call_id, False, {}, "dynamic_scope_unsupported")
            one = LinuxDockerBackend(replace(self.policy, mode="workspace-write"))
            one.patch_timeout_seconds = self.patch_timeout_seconds
            try:
                return await one.execute("apply_patch", arguments, context, emit)
            finally:
                if not await asyncio.shield(one.destroy()):
                    self.status, self.detail = "failed", one.detail
                    raise DockerError(one.detail)
