"""Opt-in real Docker boundary checks; uses only owned disposable workspaces."""

from __future__ import annotations

import asyncio
import io
import json
import os
import tarfile
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from nexus.core.types import ExecutionContext, ToolCancelled
from nexus.evaluation.cases import CASE_IDS, SUITE, load_case
from nexus.evaluation.environment import Docker, Environment, blob_id
from nexus.evaluation.validation import validate
from nexus.tools.patch import apply_patch

pytestmark = pytest.mark.skipif(
    os.environ.get("NEXUS_TEST_DOCKER") != "1", reason="Opt-in Docker isolation acceptance"
)


@pytest.mark.parametrize("case_id", CASE_IDS)
async def test_real_target_contamination_scan(case_id: str) -> None:
    cache = Path.home() / ".nexus" / "evaluation" / "cache"
    case = load_case(case_id)
    docker = await Docker.connect(cache)
    environment = Environment(docker, case, cache)
    try:
        await environment.prepare()
        script = (SUITE / "environments/isolate_target.py").read_text(encoding="utf-8")
        scan = json.loads(
            await environment.checked(["python", "-c", script, "scan", case.key], 180)
        )
        assert not scan["errors"] and not scan["findings"], scan
        origin = await environment.checked(
            ["python", "-c", f"import {case.key} as target; print(target.__file__)"]
        )
        assert origin.decode().strip().startswith("/workspace/"), origin
    finally:
        await environment.close()


async def test_real_docker_snapshot_patch_timeout_cancel_and_isolation(tmp_path: Path) -> None:
    case = replace(load_case(CASE_IDS[-1]), id="rich__boundary-smoke")
    source = tmp_path / "sources" / case.id
    source.mkdir(parents=True)
    entries = []
    with tarfile.open(source / "base.tar", "w") as archive:
        for name, data, mode in [
            ("a.txt", b"original\n", "100644"),
            ("remove.txt", b"delete me\n", "100644"),
            ("run.sh", b"#!/bin/sh\necho hello\n", "100755"),
            ("link.txt", b"a.txt", "120000"),
        ]:
            entries.append(dict(path=name, type="blob", mode=mode, sha=blob_id(data)))
            member = tarfile.TarInfo(name)
            member.mode = int(mode, 8) & 0o777
            if mode == "120000":
                member.type, member.linkname = tarfile.SYMTYPE, data.decode()
                archive.addfile(member)
            else:
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
    (source / "tree.json").write_text(json.dumps(dict(tree=entries)), encoding="utf-8")
    docker = await Docker.connect(tmp_path)
    env = Environment(docker, case, tmp_path)
    events = []

    async def emit(kind: str, data: Any, *, protocol_data: Any = None) -> int:
        events.append((kind, data))
        return len(events)

    try:
        await env.prepare()
        context = ExecutionContext(env.workspace, "tool", "/bin/sh", output_limit_bytes=1000)
        assert (await env.checked(["git", "rev-list", "--count", "HEAD"])).strip() == b"1"
        assert not (await env.checked(["git", "remote", "-v"])).strip()
        assert not (await env.checked(["git", "status", "--porcelain"])).strip()
        assert (await env.checked(["readlink", "link.txt"])).strip() == b"a.txt"
        config = json.loads((await docker.checked(["inspect", env.name])).decode())[0]
        assert config["HostConfig"]["NetworkMode"] == "none"
        assert [m["Destination"] for m in config["Mounts"]] == ["/workspace"]
        assert not any(
            "DASHSCOPE" in v or "NEXUS_MODEL" in v or "PROXY=" in v.upper()
            for v in config["Config"]["Env"]
        )
        isolation = await env.execute(
            {
                "command": (
                    "test ! -e /validator && test ! -e /var/run/docker.sock "
                    "&& test ! -e /tmp/nexus-base.tar"
                )
            },
            context,
            emit,
        )
        assert isolation.ok
        network = await env.execute(
            {"command": "python -c 'import socket; socket.create_connection((\"1.1.1.1\",443),1)'"},
            context,
            emit,
        )
        assert not network.ok
        patched = await apply_patch(
            {"patch": "--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-original\n+updated\n"}, context, emit
        )
        assert patched.ok
        link = await apply_patch(
            {"patch": "--- a/link.txt\n+++ b/link.txt\n@@ -1 +1 @@\n-original\n+escape\n"},
            context,
            emit,
        )
        assert not link.ok
        committed = await env.execute(
            {
                "command": (
                    "git add a.txt && git commit -m candidate && rm remove.txt "
                    "&& printf 'new file\\n' > added.txt"
                )
            },
            context,
            emit,
        )
        assert committed.ok
        timeout = await env.execute(
            {"command": "sleep 30 & echo $! > /tmp/timed-child; wait", "timeout_ms": 1500},
            context,
            emit,
        )
        assert timeout.data["timed_out"] and not timeout.data["cleanup_incomplete"]
        assert (
            await env.checked(["sh", "-c", "! kill -0 $(cat /tmp/timed-child) 2>/dev/null"])
        ).strip() == b""
        task = asyncio.create_task(
            env.execute({"command": "sleep 30 & echo $! > /tmp/cancel-child; wait"}, context, emit)
        )
        for _ in range(30):
            code, _, _ = await docker.call(["exec", env.name, "test", "-s", "/tmp/cancel-child"])
            if code == 0:
                break
            await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(ToolCancelled) as exc:
            await task
        assert (
            exc.value.result.data["cancelled"] and not exc.value.result.data["cleanup_incomplete"]
        )
        await env.checked(["sh", "-c", "! kill -0 $(cat /tmp/cancel-child) 2>/dev/null"])
        bounded = await env.execute({"command": "python -c 'print(\"x\"*100000)'"}, context, emit)
        assert (
            bounded.ok and bounded.truncated and bounded.data["discarded_bytes"]["stdout"] > 98000
        )
        patch = tmp_path / "candidate.diff"
        await env.collect_patch(patch)
        contents = patch.read_text(encoding="utf-8")
        assert "+updated" in contents and "+new file" in contents and "-delete me" in contents
        assert "run.sh" not in contents and "link.txt" not in contents
    finally:
        await env.close()
    assert not env.workspace.exists()


async def test_real_fresh_validator_rejects_empty_patch(tmp_path: Path) -> None:
    # Fixed Rich base, verified source cache. No model request and no reference answer.
    cache = Path.home() / ".nexus" / "evaluation" / "cache"
    docker = await Docker.connect(cache)
    patch = tmp_path / "patch.diff"
    patch.write_bytes(b"")
    result = await validate(load_case(CASE_IDS[-1]), patch, tmp_path, docker, cache)
    assert result["status"] == "failed", result
    data = json.loads((tmp_path / "tests.json").read_bytes())
    assert sum(value == "failed" for value in data["tests"].values()) == 13
