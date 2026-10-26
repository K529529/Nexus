"""Real installed-package WSL/Linux acceptance; no model calls or mock isolation."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import shlex
import sys
import time
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from nexus.app.events import Events
from nexus.app.sandbox_setup import orphan_diagnostics
from nexus.app.session import SessionLog
from nexus.core.security import ApprovalDecision, SecurityGateway, SecurityPolicy
from nexus.core.types import ExecutionContext, Session, ToolCall, ToolCancelled
from nexus.tools.linux_docker import DockerCLI, LinuxDockerBackend
from nexus.tools.registry import native_tools, permission_tool


def python(code):
    return "python -c " + shlex.quote(code)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--parent", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    root = args.parent.resolve() / ("acceptance-" + uuid4().hex)
    root.mkdir()
    work = root / "work"
    work.mkdir()
    (work / "sub").mkdir()
    (work / "seed.txt").write_text("seed")
    (root / "outside.txt").write_text("host-only")
    (work / "test_temp.py").write_text(
        "from pathlib import Path\nfrom tempfile import TemporaryDirectory\n"
        "def test_temp(tmp_path,tmp_path_factory):\n"
        "    with TemporaryDirectory() as root:\n"
        "        p=Path(root)/'private'; p.mkdir(mode=0o700)\n"
        "        (p/'hello').write_text('hello'); assert (p/'hello').read_text()=='hello'\n"
        "    (tmp_path/'a').write_text('ok')\n"
        "    (tmp_path_factory.mktemp('factory')/'b').write_text('ok')\n"
    )
    report = {
        "root": str(root),
        "python": sys.executable,
        "platform": platform.platform(),
        "uid_gid": [os.getuid(), os.getgid()],
        "rows": [],
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)

    def save():
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    def check(name, passed, **details):
        report["rows"].append({"test": name, "pass": bool(passed), **details})
        print(name, "PASS" if passed else "FAIL", flush=True)
        save()

    async def ignore(*args, **kw):
        return 0

    session = Session(work, run_id="linux-acceptance")
    writer = SessionLog.create(session, "linux-acceptance", {}, root / "audit")
    events = Events(session, writer, ignore)

    async def invoke(
        backend, command=None, *, patch=None, emit=events, timeout_ms=120000, budget=32768, cwd="."
    ):
        ctx = ExecutionContext(work, uuid4().hex, "/bin/sh", budget)
        name = "apply_patch" if patch is not None else "exec_command"
        arguments = (
            {"patch": patch}
            if patch is not None
            else {"command": command, "timeout_ms": timeout_ms, "workdir": cwd}
        )
        start = time.monotonic()
        try:
            result = await backend.execute(name, arguments, ctx, emit)
        except ToolCancelled as exc:
            result = exc.result
        return result, {
            "result": asdict(result),
            "elapsed_ms": round((time.monotonic() - start) * 1000, 2),
        }

    os.environ["NEXUS_TEST_API_KEY"] = "fixture-secret-not-for-container"
    for mode in ("workspace-write", "read-only"):
        backend = LinuxDockerBackend(SecurityPolicy(mode))
        check(mode + "-lazy", backend.container_id is None)
        try:
            r, data = await invoke(backend, "printf 'shell-ok\\n'")
            check(mode + "-cold-shell", r.ok and "shell-ok" in r.data.get("stdout", ""), **data)
            if not r.ok:
                raise RuntimeError("Container startup failed; preserve report and inspect failure")
            ident = backend.container_id
            inspect = await backend.cli.inspect(ident)
            check(mode + "-inspect", True, inspection=inspect, timings=backend.timings)
            for n in range(3):
                r, data = await invoke(backend, "printf warm")
                check(mode + f"-warm-{n}", r.ok and backend.container_id == ident, **data)
            r, data = await invoke(backend, "exit 7")
            check(
                mode + "-confirmed-command-failure",
                r.data.get("exit_code") == 7 and backend.container_id == ident,
                **data,
            )
            r, data = await invoke(backend, "pwd", cwd="sub")
            check(mode + "-workdir", r.ok and r.data["stdout"].strip() == "/workspace/sub", **data)
            r, data = await invoke(
                backend,
                python(
                    "import os,pathlib,json; s=pathlib.Path('/proc/self/status').read_text(); "
                    "assert os.getuid()!=0; assert 'CapEff:\\t0000000000000000' in s; "
                    "assert 'NoNewPrivs:\\t1' in s; "
                    "assert not pathlib.Path('/var/run/docker.sock').exists(); "
                    "assert 'NEXUS_TEST_API_KEY' not in os.environ; print(os.getuid(),os.getgid())"
                ),
            )
            check(mode + "-identity-capabilities-env", r.ok, **data)
            r, data = await invoke(backend, "python -m pytest -q -p no:cacheprovider test_temp.py")
            check(mode + "-python0700-tempfile-pytest", r.ok, **data)
            for operation, command in (
                ("create", "printf new > created.txt"),
                ("update", "printf changed > seed.txt"),
                ("delete", "rm seed.txt"),
            ):
                r, data = await invoke(backend, command)
                check(mode + "-workspace-" + operation, r.ok == (mode == "workspace-write"), **data)
                if mode == "workspace-write" and operation == "delete":
                    (work / "seed.txt").write_text("seed")
            r, data = await invoke(
                backend,
                python(
                    f"from pathlib import Path; p=Path({str(root / 'outside.txt')!r}); "
                    "assert not p.exists(); "
                    "assert not Path('/home/archer/.nexus/config.toml').exists(); "
                    "Path('/tmp/ok').write_text('temp')"
                ),
            )
            check(
                mode + "-outside-host-not-mounted-temp-writable",
                r.ok and (root / "outside.txt").read_text() == "host-only",
                **data,
            )
            r, data = await invoke(
                backend,
                python("import sys; print('x'*40000); print('y'*40000,file=sys.stderr)"),
                budget=2048,
            )
            check(
                mode + "-bounded-output",
                r.ok and r.truncated and sum(r.data["discarded_bytes"].values()) > 70000,
                **data,
            )
            if mode == "workspace-write":
                patch = "*** Begin Patch\n*** Add File: edit.txt\n+old\n*** End Patch"
                seen = []

                async def audited(kind, value, seen=seen, **kw):
                    if kind == "patch_diff":
                        seen.append(not (work / "edit.txt").exists())
                    result = await events(kind, value, **kw)
                    if kind == "patch_diff":
                        assert '"phase":"planned"' in writer.path.read_text()
                    return result

                r, data = await invoke(backend, patch=patch, emit=audited)
                check(
                    "patch-create-audit-before-write",
                    r.ok and seen == [True] and (work / "edit.txt").read_text() == "old\n",
                    **data,
                )
                r, data = await invoke(
                    backend,
                    patch=(
                        "*** Begin Patch\n*** Update File: edit.txt\n@@\n-old\n+new\n*** End Patch"
                    ),
                )
                check(
                    "patch-update-host-ownership",
                    r.ok
                    and (work / "edit.txt").read_text() == "new\n"
                    and (work / "edit.txt").stat().st_uid == os.getuid()
                    and (work / "edit.txt").stat().st_gid == os.getgid(),
                    **data,
                )
                r, data = await invoke(
                    backend,
                    "git init -q && git add . && git -c user.name=Fixture "
                    "-c user.email=fixture@example.invalid commit -qm fixture",
                )
                check("git-init-add-commit", r.ok, **data)
            else:

                async def approve(request):
                    return ApprovalDecision(request.request_id, "allow_once")

                gateway = SecurityGateway(backend.policy, backend=backend, approver=approve)
                registry = {**native_tools(), "request_permissions": permission_tool()}

                async def gw(name, arguments, call_id, gateway=gateway, registry=registry):
                    return await gateway.execute(
                        ToolCall(call_id, name, json.dumps(arguments)),
                        registry,
                        session,
                        ExecutionContext(work, call_id, "/bin/sh"),
                        events,
                    )

                r = await gw(
                    "apply_patch",
                    {"patch": "*** Begin Patch\n*** Add File: once.txt\n+once\n*** End Patch"},
                    "denied",
                )
                check(
                    "hitl-ro-default-deny",
                    r.error_code == "policy_denied" and not (work / "once.txt").exists(),
                )
                r = await gw(
                    "request_permissions",
                    {"denied_call_id": "denied", "reason": "one exact patch"},
                    "approve",
                )
                check("hitl-once", r.ok and (work / "once.txt").exists(), result=asdict(r))
                r = await gw(
                    "request_permissions", {"denied_call_id": "denied", "reason": "reuse"}, "reuse"
                )
                check("hitl-no-reuse", not r.ok)
                r, data = await invoke(backend, "echo bad > after-once")
                check(
                    "hitl-original-container-stays-ro",
                    not r.ok
                    and backend.container_id == ident
                    and not (work / "after-once").exists(),
                    **data,
                )
        finally:
            ident = backend.container_id
            await backend.close()
            check(
                mode + "-normal-cleanup",
                ident is None or await backend.cli.inspect(ident) is None,
                timings=backend.timings,
            )

    # Network policy: test the exact same external request in bridge and none.
    for network in ("bridge", "none"):
        backend = LinuxDockerBackend(SecurityPolicy("read-only", docker_network=network))
        try:
            r, data = await invoke(
                backend,
                python(
                    "import urllib.request; print(urllib.request.urlopen('https://example.com',timeout=8).status)"
                ),
            )
            check("network-" + network, r.ok == (network == "bridge"), **data)
        finally:
            await backend.close()

    for stop in ("timeout", "cancel"):
        backend = LinuxDockerBackend(SecurityPolicy("workspace-write"))
        marker = work / (stop + ".late")
        ready = asyncio.Event()

        async def watch(kind, value, ready=ready, **kw):
            await events(kind, value, **kw)
            if kind == "tool_output_delta" and "child-ready" in value.get("text", ""):
                ready.set()

        child = (
            "import time,pathlib; time.sleep(5); "
            f"pathlib.Path('/workspace/{marker.name}').write_text('bad')"
        )
        parent = (
            "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',"
            + repr(child)
            + "]); print('child-ready',flush=True); time.sleep(30)"
        )
        task = asyncio.create_task(
            invoke(
                backend, python(parent), timeout_ms=1800 if stop == "timeout" else 30000, emit=watch
            )
        )
        await asyncio.wait_for(ready.wait(), 20)
        if stop == "cancel":
            task.cancel()
        r, data = await task
        await asyncio.sleep(5.2)
        check(
            stop + "-grandchildren-stopped",
            r.error_code == ("cancelled" if stop == "cancel" else "timeout")
            and not r.data["cleanup_incomplete"]
            and not marker.exists()
            and backend.container_id is None,
            **data,
        )
        r, data = await invoke(backend, "true")
        check(stop + "-no-container-reuse", not r.ok, **data)
        await backend.close()

    # Disk/audit failure at the first planned diff cannot mutate even its parent directory.
    backend = LinuxDockerBackend(SecurityPolicy("workspace-write"))

    async def sink_failure(kind, value, **kw):
        raise OSError("fixture audit write failure")

    r, data = await invoke(
        backend,
        patch="*** Begin Patch\n*** Add File: unacked/a\n+bad\n*** End Patch",
        emit=sink_failure,
    )
    check(
        "patch-no-ack-no-write",
        not r.ok and not (work / "unacked").exists() and backend.container_id is None,
        **data,
    )
    await backend.close()

    # Cancel after one committed file, while the second planned event awaits persistence.
    backend = LinuxDockerBackend(SecurityPolicy("workspace-write"))
    blocked = asyncio.Event()

    async def slow_sink(kind, value, **kw):
        if kind == "patch_diff" and value["file_index"] == 2:
            blocked.set()
            await asyncio.Event().wait()
        return await events(kind, value, **kw)

    task = asyncio.create_task(
        invoke(
            backend,
            patch=(
                "*** Begin Patch\n*** Add File: first\n+yes\n"
                "*** Add File: second\n+no\n*** End Patch"
            ),
            emit=slow_sink,
        )
    )
    await asyncio.wait_for(blocked.wait(), 20)
    task.cancel()
    r, data = await task
    check(
        "patch-cancel-completed-versus-unwritten",
        r.error_code == "cancelled"
        and r.data["changed_files"] == 1
        and (work / "first").exists()
        and not (work / "second").exists(),
        **data,
    )
    await backend.close()

    # Two live sessions must never adopt each other's container or temporary files.
    a = LinuxDockerBackend(SecurityPolicy("read-only"))
    b = LinuxDockerBackend(SecurityPolicy("read-only"))
    try:
        ra, _ = await invoke(a, "echo private > /tmp/session-a")
        rb, _ = await invoke(b, "test ! -e /tmp/session-a")
        check("sessions-independent", ra.ok and rb.ok and a.container_id != b.container_id)
        await a.close()
        rb, data = await invoke(b, "echo still-running")
        check("closing-other-session-safe", rb.ok, **data)
    finally:
        await a.close()
        await b.close()

    # Real patch timeout and one-shot cancellation, with an accelerated host timer.
    for kind in ("patch-timeout", "once-timeout", "once-cancel"):
        once = kind.startswith("once")
        backend = LinuxDockerBackend(SecurityPolicy("read-only" if once else "workspace-write"))
        await invoke(backend, "true")
        original_id = backend.container_id
        backend.patch_timeout_seconds = 0.7
        waiting = asyncio.Event()

        async def pause(kind, value, waiting=waiting, **kw):
            if kind == "patch_diff":
                waiting.set()
                await asyncio.Event().wait()
            return await events(kind, value, **kw)

        async def interrupted(backend=backend, once=once, kind=kind, pause=pause):
            arguments = {"patch": f"*** Begin Patch\n*** Add File: {kind}.txt\n+bad\n*** End Patch"}
            ctx = ExecutionContext(work, uuid4().hex, "/bin/sh")
            try:
                if once:
                    return await backend.execute_patch_once(arguments, ctx, pause)
                return await backend.execute("apply_patch", arguments, ctx, pause)
            except ToolCancelled as exc:
                return exc.result

        task = asyncio.create_task(interrupted())
        await asyncio.wait_for(waiting.wait(), 20)
        if kind == "once-cancel":
            task.cancel()
        r = await task
        check(
            kind + "-no-write-cleanup",
            not r.ok and not r.data["cleanup_incomplete"] and not (work / (kind + ".txt")).exists(),
            result=asdict(r),
        )
        if once:
            r, data = await invoke(backend, "echo bad > once-after-cancel")
            check(
                kind + "-ro-container-preserved",
                not r.ok
                and backend.container_id == original_id
                and not (work / "once-after-cancel").exists(),
                **data,
            )
        await backend.close()

    # Revalidate actual Docker state before reuse: resource mutation is detected.
    backend = LinuxDockerBackend(SecurityPolicy("read-only"))
    await invoke(backend, "true")
    await backend.cli.run(["update", "--pids-limit", "64", backend.container_id])
    r, data = await invoke(backend, "echo must-not-execute")
    check("container-tampering-fail-closed", not r.ok and backend.container_id is None, **data)
    await backend.close()

    backend = LinuxDockerBackend(SecurityPolicy("read-only"))
    backend.cli.env.pop("DOCKER_CONTEXT", None)
    backend.cli.env["DOCKER_HOST"] = "unix:///tmp/nexus-does-not-exist-" + uuid4().hex
    r, data = await invoke(backend, "true")
    check(
        "docker-unavailable-fail-closed",
        not r.ok and r.data["execution_state"] == "not_executed",
        **data,
    )
    await backend.close()

    for image_ref in ("nexus-missing-" + uuid4().hex, "alpine:3.20"):
        backend = LinuxDockerBackend(SecurityPolicy("read-only", docker_image=image_ref))
        r, data = await invoke(backend, "echo must-not-execute")
        check(
            "image-missing-or-incompatible-" + image_ref.split(":")[0],
            not r.ok
            and r.data["execution_state"] == "not_executed"
            and backend.container_id is None,
            **data,
        )
        await backend.close()

    backend = LinuxDockerBackend(SecurityPolicy("workspace-write"))
    task = asyncio.create_task(invoke(backend, "echo bad > init-cancel"))
    async with asyncio.timeout(20):
        while not backend.possibly_created:  # noqa: ASYNC110 - observe real create, no fake event
            await asyncio.sleep(0.01)
    task.cancel()
    r, data = await task
    check(
        "cancel-during-create-no-orphan",
        r.error_code == "cancelled"
        and not r.data["cleanup_incomplete"]
        and backend.container_id is None
        and not (work / "init-cancel").exists(),
        **data,
    )
    await backend.close()

    # Kill only this test's host Docker CLI: absence of a completion frame must kill its container.
    backend = LinuxDockerBackend(SecurityPolicy("workspace-write"))
    captured = []
    original_spawn = backend.cli.spawn

    async def capture(argv, **kw):
        process = await original_spawn(argv, **kw)
        if argv[0] == "exec":
            captured.append(process)
        return process

    backend.cli.spawn = capture
    ready = asyncio.Event()

    async def cli_ready(kind, value, **kw):
        if kind == "tool_output_delta" and "cli-ready" in value.get("text", ""):
            ready.set()
        return await events(kind, value, **kw)

    task = asyncio.create_task(
        invoke(
            backend,
            python(
                "import time,pathlib; print('cli-ready',flush=True); time.sleep(3); "
                "pathlib.Path('/workspace/cli-late').write_text('bad')"
            ),
            emit=cli_ready,
        )
    )
    await asyncio.wait_for(ready.wait(), 20)
    captured[0].kill()
    r, data = await task
    await asyncio.sleep(3.2)
    check(
        "lost-cli-completion-kills-container",
        not r.ok
        and not r.data["cleanup_incomplete"]
        and r.data["execution_state"] == "interrupted_unknown"
        and backend.container_id is None
        and not (work / "cli-late").exists(),
        **data,
    )
    await backend.close()

    # A crashed owner leaves a diagnosable container; never silently reattach it.
    code = """import asyncio,json,os,sys
from pathlib import Path
from nexus.tools.linux_docker import LinuxDockerBackend
from nexus.core.security import SecurityPolicy
async def main():
 b=LinuxDockerBackend(SecurityPolicy('read-only')); await b.ready(Path(sys.argv[1]))
 print(b.container_id,flush=True); os._exit(17)
asyncio.run(main())
"""
    owner = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-c",
        code,
        str(work),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await owner.communicate()
    orphan = out.decode().strip()
    rows = await orphan_diagnostics(DockerCLI())
    check(
        "crashed-owner-orphan-diagnosed",
        owner.returncode == 17
        and any(r["container_id"] == orphan and r["state"] == "orphan" for r in rows),
        diagnostics=rows,
        stderr=err.decode(),
    )
    # Explicit test-owned cleanup: ID came from this child; assert all identity labels first.
    cli = DockerCLI()
    info = await cli.inspect(orphan)
    assert info["Config"]["Labels"]["io.nexus.sandbox.workspace"] == str(work)
    assert info["Config"]["Labels"]["io.nexus.sandbox.pid"] == str(owner.pid)
    await cli.run(["rm", "-f", orphan])
    check("test-owned-orphan-cleaned", await cli.inspect(orphan) is None)
    writer.close()
    audit = writer.path.read_text()
    check(
        "durable-audit",
        all(k in audit for k in ("patch_diff", "patch_committed", "approval_resolved")),
        path=str(writer.path),
    )
    report["remaining_containers"] = await orphan_diagnostics(cli)
    save()
    if not all(row["pass"] for row in report["rows"]):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
