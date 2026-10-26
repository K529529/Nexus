"""Explicit Linux image setup and read-only diagnostics; no model calls."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

from nexus.app.config import config_path, read_toml, security_policy
from nexus.core.types import Json
from nexus.tools.docker_image import LABEL, build_context, default_image
from nexus.tools.linux_docker import (
    DockerCLI,
    DockerError,
    LinuxDockerBackend,
    linux_ids,
    process_identity,
    validate_policy,
    validate_workspace,
)


async def orphan_diagnostics(cli: DockerCLI) -> list[Json]:
    ids = (await cli.run(["ps", "-aq", "--no-trunc", "--filter", "label=" + LABEL + "=v1"])).split()
    rows = []
    host = (await asyncio.to_thread(Path("/etc/machine-id").read_text)).strip()
    boot = (await asyncio.to_thread(Path("/proc/sys/kernel/random/boot_id").read_text)).strip()
    for ident in ids:
        value = await cli.inspect(ident)
        if value is None:
            continue
        labels = value["Config"].get("Labels") or {}
        state = "ownership_unknown"
        if labels.get(LABEL + ".host") == host and labels.get(LABEL + ".uid") == str(
            linux_ids()[0]
        ):
            pid = labels.get(LABEL + ".pid", "")
            if pid.isdigit() and labels.get(LABEL + ".start") not in {None, "unknown"}:
                state = (
                    "owner_alive"
                    if (
                        labels.get(LABEL + ".boot") == boot
                        and process_identity(int(pid)) == labels[LABEL + ".start"]
                    )
                    else "orphan"
                )
        rows.append(
            {
                "container_id": ident,
                "name": value["Name"],
                "state": state,
                "running": value["State"]["Running"],
                "workspace": labels.get(LABEL + ".workspace"),
                "action": "diagnostic only; never auto-reuse/delete",
            }
        )
    return rows


async def sandbox_command(action: str) -> int:
    if sys.platform != "linux":
        raise DockerError("Docker sandbox setup/doctor must run inside Linux/WSL2 Ubuntu")
    policy = security_policy(read_toml(config_path()))
    validate_policy(policy)
    cli = DockerCLI()
    report: Json = {
        "engine": await cli.environment(),
        "uid_gid": linux_ids(),
        "network": policy.docker_network,
    }
    if action == "setup":
        print(
            "Explicit setup: building trusted Python/Git/pytest image; may download dependencies.",
            flush=True,
        )
        with build_context() as directory:
            process = await cli.spawn(
                [
                    "build",
                    "--progress=plain",
                    "-t",
                    policy.docker_image or default_image(),
                    directory,
                ]
            )
            assert process.stdout is not None and process.stderr is not None

            async def output(stream: asyncio.StreamReader) -> None:
                while chunk := await stream.read(4096):
                    print(chunk.decode(errors="replace"), end="", flush=True)

            try:
                async with asyncio.timeout(900):
                    await asyncio.gather(output(process.stdout), output(process.stderr))
                    if await process.wait() != 0:
                        raise DockerError(
                            "Image build failed; retained output above; no tool executed"
                        )
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
    backend = LinuxDockerBackend(policy)
    try:
        report["image_id"] = await backend.image()
    except DockerError as exc:
        report["image_error"] = str(exc)
    try:
        report["workspace"] = str(await asyncio.to_thread(validate_workspace, Path.cwd()))
    except DockerError as exc:
        report["workspace_error"] = str(exc)
    report["mount_probe"] = (
        "NOT RUN (doctor is read-only; real mount checked on lazy container start)"
    )
    report["containers"] = await orphan_diagnostics(cli)
    report["checked_at"] = time.time()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if "image_error" in report or "workspace_error" in report else 0
