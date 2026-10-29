"""Policy/transport regressions. Real Docker acceptance is a separate opt-in script."""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from nexus.app.config import ConfigError, security_policy
from nexus.core.security import SecurityPolicy
from nexus.core.types import ExecutionContext, ToolCancelled
from nexus.tools import linux_docker as docker
from nexus.tools.docker_image import default_image, fingerprint
from nexus.tools.patch import apply_patch
from nexus.tools.platform_backend import platform_backend


@pytest.mark.parametrize(
    "key,value",
    [
        ("docker_network", "host"),
        ("docker_network", "bad"),
        ("docker_cpus", 0),
        ("docker_cpus", True),
        ("docker_cpus", float("nan")),
        ("docker_memory_mb", "1024"),
        ("docker_memory_mb", 0),
        ("docker_pids", -1),
        ("docker_pids", True),
        ("docker_image", "--privileged"),
        ("docker_image", "image; touch /tmp/bad"),
    ],
)
def test_invalid_settings(key: str, value: Any) -> None:
    with pytest.raises(ValueError):
        security_policy({"security": {key: value}})


@pytest.mark.parametrize("key", ["allowed_domains", "denied_domains"])
def test_explicit_empty_domain_policy_rejected(monkeypatch: pytest.MonkeyPatch, key: str) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(ConfigError, match="unsupported"):
        security_policy({"security": {"mode": "read-only", key: []}})


def test_selector_local_and_platforms(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    backend = platform_backend(SecurityPolicy("read-only"))
    assert isinstance(backend, docker.LinuxDockerBackend) and backend.container_id is None
    with pytest.raises(ValueError, match="original executor"):
        platform_backend(SecurityPolicy())
    monkeypatch.setattr(sys, "platform", "darwin")
    assert type(platform_backend(SecurityPolicy("read-only"))).__name__ == "UnavailableBackend"


@pytest.mark.parametrize(
    "policy",
    [
        SecurityPolicy("read-only", writable_roots=("/tmp/extra",)),
        SecurityPolicy("read-only", domain_policy_configured=True),
        SecurityPolicy("read-only", windows_koffi="/opt/koffi"),
    ],
)
def test_unsupported_fields_fail_closed(
    monkeypatch: pytest.MonkeyPatch, policy: SecurityPolicy
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(docker, "linux_ids", lambda: (1001, 1002))
    with pytest.raises(docker.DockerError):
        docker.validate_policy(policy)


def test_creation_resources_no_implicit_mounts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(docker, "linux_ids", lambda: (1001, 1002))
    backend = docker.LinuxDockerBackend(SecurityPolicy("read-only", docker_network="none"))
    backend.workspace, backend.image_id = tmp_path, "sha256:fixture"
    args = backend.creation_args()
    assert args[args.index("--user") + 1] == "1001:1002"
    assert args[args.index("--network") + 1] == "none"
    assert args.count("--mount") == 1 and ",readonly" in args[args.index("--mount") + 1]
    assert "bind-recursive=disabled" in args[args.index("--mount") + 1]
    assert "--privileged" not in args and "--pid" not in args and "--env" not in args
    assert "--cap-drop" in args and "--read-only" in args
    assert backend.name != docker.LinuxDockerBackend(backend.policy).name
    assert fingerprint()[:20] in default_image()


@pytest.mark.parametrize("cancel", [False, True])
async def test_prewrite_barrier_leaves_no_side_effects(tmp_path: Path, cancel: bool) -> None:
    ctx = ExecutionContext(tmp_path, "one", "/bin/sh")
    seen = []

    async def reject(kind: str, data: dict[str, Any], **kw: Any) -> int:
        seen.append((kind, data))
        assert not (tmp_path / "parent").exists()
        if cancel:
            raise asyncio.CancelledError
        raise OSError("durable audit failed")

    patch = "*** Begin Patch\n*** Add File: parent/one.txt\n+one\n*** End Patch"
    with pytest.raises(ToolCancelled if cancel else OSError):
        await apply_patch({"patch": patch}, ctx, reject, audit_before_write=True)
    assert seen[0][0] == "patch_manifest" and not (tmp_path / "parent").exists()


async def test_prewrite_ack_rechecks_conflicts_and_records_completion(tmp_path: Path) -> None:
    ctx = ExecutionContext(tmp_path, "one", "/bin/sh")
    target = tmp_path / "one.txt"
    events = []

    async def emit(kind: str, data: dict[str, Any], **kw: Any) -> int:
        events.append((kind, data))
        if kind in {"patch_manifest", "patch_diff"}:
            assert not target.exists()
        else:
            assert target.read_text() == "one\n"
        return 0

    args = {"patch": "*** Begin Patch\n*** Add File: one.txt\n+one\n*** End Patch"}
    result = await apply_patch(args, ctx, emit, audit_before_write=True)
    assert result.ok and [e[0] for e in events] == [
        "patch_manifest", "patch_diff", "patch_committed"]
    target.unlink()

    async def race(kind: str, data: dict[str, Any], **kw: Any) -> int:
        target.write_text("host concurrent edit")
        return 0

    result = await apply_patch(args, ctx, race, audit_before_write=True)
    assert result.error_code == "concurrent_change" and target.read_text() == "host concurrent edit"


async def test_worker_wrong_ack_cannot_write(tmp_path: Path) -> None:
    from nexus.tools import patch_worker

    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        patch_worker.__file__,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdin and proc.stdout
    request = {
        "id": "request",
        "call_id": "call",
        "workspace": str(tmp_path),
        "output_limit_bytes": 32768,
        "audit_before_write": True,
        "arguments": {"patch": "*** Begin Patch\n*** Add File: one\n+one\n*** End Patch"},
    }
    proc.stdin.write((json.dumps(request) + "\n").encode())
    await proc.stdin.drain()
    async with asyncio.timeout(10):
        event = json.loads(await proc.stdout.readline())
        assert event["kind"] == "patch_manifest" and not (tmp_path / "one").exists()
        proc.stdin.write(b'{"id":"request","method":"ack","index":2,"event_kind":"patch_diff"}\n')
        await proc.stdin.drain()
        result = json.loads(await proc.stdout.readline())
        await proc.wait()
    assert result["cancelled"] and not (tmp_path / "one").exists()


async def test_failed_cleanup_prevents_reuse(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    backend = docker.LinuxDockerBackend(SecurityPolicy("workspace-write"))

    async def fail(workspace: Path) -> None:
        raise docker.DockerError("fixture daemon unavailable")

    async def incomplete() -> bool:
        return False

    async def emit(*args: Any, **kw: Any) -> int:
        return 0

    monkeypatch.setattr(backend, "ready", fail)
    monkeypatch.setattr(backend, "destroy", incomplete)
    ctx = ExecutionContext(tmp_path, "one", "/bin/sh")
    result = await backend.execute("exec_command", {"command": "true"}, ctx, emit)
    assert result.error_code == "cleanup_incomplete"
    result = await backend.execute(
        "exec_command", {"command": "true"}, replace(ctx, call_id="two"), emit
    )
    assert result.error_code == "backend_unavailable"


async def test_conversation_displays_container_path_without_starting_docker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from nexus.app.bootstrap import Conversation
    from nexus.app.config import Config, ModelConfig
    from nexus.core.types import Limits, RuntimeEvent

    monkeypatch.setattr(sys, "platform", "linux")

    async def consume(event: RuntimeEvent) -> None:
        pass

    config = Config(
        ModelConfig("fake", 32768),
        Limits(),
        memory_enabled=False,
        security=SecurityPolicy("read-only"),
    )
    conversation = Conversation(tmp_path, config, consume)
    assert conversation.environment_display == ("Linux (Docker)", "/workspace")
    assert isinstance(conversation.gateway.backend, docker.LinuxDockerBackend)
    assert conversation.gateway.backend.container_id is None
    await conversation.close()


@pytest.mark.parametrize("startup_mode", ["local", "read-only"])
def test_windows_never_silently_ignores_docker_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, startup_mode: str
) -> None:
    from nexus.tools.sandbox import BridgeError
    from nexus.tools.windows_native import WindowsNativeBackend

    monkeypatch.setattr(sys, "platform", "win32")
    data = {"security": {"mode": startup_mode, "docker_network": "none"}}
    if startup_mode == "read-only":
        with pytest.raises(ConfigError, match="Docker settings apply only"):
            security_policy(data)
    else:
        policy = security_policy(data)
        backend = WindowsNativeBackend(replace(policy, mode="read-only"))
        with pytest.raises(BridgeError, match="Docker settings apply only"):
            backend.installation(tmp_path, "powershell.exe")
