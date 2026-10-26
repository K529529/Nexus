"""Opt-in real Windows acceptance. Only creates/labels fresh dedicated fixtures.

Run from a trusted installation outside the generated workspace. No model, SRT,
shared TEMP ACL changes, administrator token, or old security PoC is used.
Fixtures and failure evidence are retained for manual review.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, replace
from pathlib import Path
from uuid import uuid4

from nexus.app.events import Events
from nexus.app.session import SessionLog
from nexus.core.security import ApprovalDecision, SecurityGateway, SecurityPolicy
from nexus.core.types import ExecutionContext, Session, ToolCall, ToolCancelled
from nexus.tools.registry import native_tools, permission_tool
from nexus.tools.windows_native import WindowsNativeBackend

PS = os.environ["SystemRoot"] + "/System32/WindowsPowerShell/v1.0/powershell.exe"


def ps(text: str) -> str:
    env = {k: v for k, v in os.environ.items() if k.upper() != "PSMODULEPATH"}
    return subprocess.check_output(
        [PS, "-NoProfile", "-NonInteractive", "-Command", text], encoding="utf-8", env=env
    ).strip()


def quote(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def python(code: str) -> str:
    # Windows PowerShell 5 strips embedded double quotes for native argv.
    encoded = base64.b64encode(code.encode()).decode()
    return f"& {quote(sys.executable)} -c \"exec(__import__('base64').b64decode('{encoded}'))\""


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--parent", type=Path, required=True)
    parser.add_argument("--koffi", required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    assert sys.platform == "win32"
    root = args.parent.resolve(strict=True) / ("nexus-native-acceptance-" + uuid4().hex)
    root.mkdir()  # Never select an existing directory for fixture ACL provisioning.
    sid = ps("[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value")
    await asyncio.to_thread(
        subprocess.run,
        ["icacls.exe", str(root), "/grant", f"*{sid}:(OI)(CI)F"],
        check=True,
        capture_output=True,
    )
    work, outside = root / "work", root / "outside"
    work.mkdir()
    outside.mkdir()
    (work / "hello.txt").write_text("hello", encoding="utf-8")
    (outside / "keep.txt").write_text("unchanged", encoding="utf-8")
    (work / "test_sample.py").write_text("def test_one(tmp_path):\n    assert tmp_path.is_dir()\n")
    report = {"root": str(root), "python": sys.executable, "shell": PS, "rows": []}
    args.report.parent.mkdir(parents=True, exist_ok=True)

    def save() -> None:
        args.report.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    def check(name: str, passed: bool, **data: object) -> None:
        report["rows"].append({"test": name, "pass": bool(passed), **data})
        save()
        print(name, "PASS" if passed else "FAIL", flush=True)

    # DACL/owner + mandatory-label readback. Read only, no system ACL writes.
    protected = [
        Path(os.environ["SystemRoot"]),
        Path(PS).parent,
        Path(os.environ["ProgramFiles"]),
        Path.home(),
        Path(os.environ["TEMP"]),
    ]

    def snapshots() -> dict:
        return {
            str(p): {
                "sddl": ps(f"(Get-Acl -LiteralPath {quote(p)}).Sddl"),
                "icacls_hex": subprocess.check_output(["icacls.exe", str(p)]).hex(),
            }
            for p in protected
        }

    before = snapshots()
    report["protected_before"] = before
    check(
        "ordinary_user",
        ps(
            "([Security.Principal.WindowsPrincipal] "
            "[Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole("
            "[Security.Principal.WindowsBuiltInRole]::Administrator)"
        )
        == "False",
    )
    policy = SecurityPolicy(
        "workspace-write", windows_koffi=args.koffi, windows_acl_roots=(str(work),)
    )
    session = Session(work, run_id="native-acceptance")
    writer = SessionLog.create(session, "native-acceptance", {}, root / "audit")

    async def consume(event):
        pass

    events = Events(session, writer, consume)
    temps = []

    async def invoke(backend, name, command=None, arguments=None, tool="exec_command", emit=events):
        ctx = ExecutionContext(work, uuid4().hex, PS)
        start = time.perf_counter()
        result = await backend.execute(
            tool, arguments or {"command": command, "timeout_ms": 15000}, ctx, emit
        )
        return result, {
            "elapsed_ms": round((time.perf_counter() - start) * 1000, 2),
            "result": asdict(result),
        }

    for mode in ("read-only", "workspace-write", "read-only"):
        label = mode + "-" + str(len(temps))
        backend = WindowsNativeBackend(replace(policy, mode=mode), secret_env=("CUSTOM_SECRET",))
        os.environ["CUSTOM_SECRET"] = "dummy-never-a-real-key"
        os.environ["OPENAI_API_KEY"] = "dummy-never-a-real-key"
        try:
            r, data = await invoke(
                backend,
                "startup",
                "$ExecutionContext.SessionState.LanguageMode; Write-Output '你好'; $env:TEMP",
            )
            lines = r.data.get("stdout", "").strip().splitlines()
            temp = Path(lines[-1]) if lines else root / "invalid-temp"
            temps.append(temp)
            check(
                label + "-powershell",
                r.ok
                and lines[:2] == ["FullLanguage", "你好"]
                and not r.data.get("stderr")
                and temp != Path(os.environ["TEMP"]),
                **data,
            )
            r, data = await invoke(
                backend,
                "python",
                python(
                    "import os,tempfile,pathlib; "
                    'p=pathlib.Path(tempfile.gettempdir())/"owned.txt"; p.write_text("ok"); '
                    'print("你好"); print(os.environ.get("OPENAI_API_KEY", "absent")); '
                    'print(os.environ.get("CUSTOM_SECRET", "absent"))'
                ),
            )
            check(
                label + "-python-env-temp",
                r.ok
                and "你好" in r.data.get("stdout", "")
                and r.data.get("stdout", "").count("absent") == 2,
                **data,
            )
            r, data = await invoke(backend, "read", "Get-Content hello.txt; whoami /groups")
            check(
                label + "-read-low-token",
                r.ok
                and "hello" in r.data.get("stdout", "")
                and "S-1-16-4096" in r.data.get("stdout", ""),
                **data,
            )
            r, data = await invoke(backend, "write", "Set-Content write.txt yes -ErrorAction Stop")
            check(label + "-workspace-write", r.ok == (mode == "workspace-write"), **data)
            for op, command in [
                ("create", f"Set-Content {quote(outside / 'new.txt')} x -ErrorAction Stop"),
                ("update", f"Set-Content {quote(outside / 'keep.txt')} x -ErrorAction Stop"),
                ("delete", f"Remove-Item {quote(outside / 'keep.txt')} -ErrorAction Stop"),
            ]:
                r, data = await invoke(backend, op, command)
                check(
                    label + "-outside-" + op,
                    not r.ok
                    and (outside / "keep.txt").read_text() == "unchanged"
                    and not (outside / "new.txt").exists(),
                    **data,
                )
            r, data = await invoke(
                backend,
                "pytest",
                f"& {quote(sys.executable)} -m pytest -q -p no:cacheprovider test_sample.py",
            )
            check(label + "-pytest", r.ok and "1 passed" in r.data.get("stdout", ""), **data)
            # Diagnose ordinary Python temp directory creation, no ACL repair.
            for bits in (0o777, 0o700):
                directory = temp / f"python-mode-{bits:o}"
                r, data = await invoke(
                    backend,
                    "python-mkdir",
                    python(
                        "import pathlib; p=pathlib.Path(" + repr(str(directory)) + "); "
                        f'p.mkdir(mode={bits}); print("mkdir-returned",flush=True); '
                        '(p/"data.txt").write_text("ok"); print("write-returned")'
                    ),
                )
                check(
                    label + f"-python-mkdir-{bits:o}",
                    r.ok,
                    sddl=ps(f"(Get-Acl -LiteralPath {quote(directory)}).Sddl"),
                    **data,
                )
            patch = {"patch": "*** Begin Patch\n*** Add File: patched.txt\n+patched\n*** End Patch"}
            if mode == "workspace-write":
                r, data = await invoke(backend, "patch", arguments=patch, tool="apply_patch")
                check(
                    label + "-patch",
                    r.ok and (work / "patched.txt").read_text().strip() == "patched",
                    **data,
                )
                r, data = await invoke(
                    backend,
                    "git",
                    "git init -q; git add hello.txt patched.txt; "
                    "git -c user.name=NativeTest -c user.email=native@example.invalid "
                    "commit -q -m acceptance; git status --porcelain; git log -1 --format=%s",
                )
                check(label + "-git", r.ok and "acceptance" in r.data.get("stdout", ""), **data)
                for index in range(3):
                    r, data = await invoke(backend, "warm", "Write-Output 'warm'")
                    check("warm-" + str(index), r.ok, **data)
                # Owned child process attempts a delayed write. No stale process may survive.
                for kind in ("timeout", "cancel"):
                    marker = work / (kind + ".marker")
                    pid_file = work / (kind + ".pid")
                    code = (
                        "import subprocess,sys,time,pathlib; p=subprocess.Popen([sys.executable,"
                        '"-c",'
                        + repr(
                            "import time,pathlib; time.sleep(10); pathlib.Path("
                            + repr(str(marker))
                            + ').write_text("bad")'
                        )
                        + "]); "
                        "pathlib.Path("
                        + repr(str(pid_file))
                        + ').write_text(str(p.pid)); print("ready",flush=True); time.sleep(30)'
                    )
                    ready = asyncio.Event()

                    async def watch(event, value, ready=ready, **kw):
                        await events(event, value, **kw)
                        if event == "tool_output_delta" and "ready" in value.get("text", ""):
                            ready.set()

                    task = asyncio.create_task(
                        invoke(
                            backend,
                            kind,
                            arguments={
                                "command": python(code),
                                "timeout_ms": 6000 if kind == "timeout" else 30000,
                            },
                            emit=watch,
                        )
                    )
                    if kind == "cancel":
                        await asyncio.wait_for(ready.wait(), 15)
                        task.cancel()
                    try:
                        r, data = await task
                    except ToolCancelled as cancelled:
                        r = cancelled.result
                        data = {"result": asdict(r)}
                    check(
                        kind + "-result",
                        r.error_code in ("command_timeout", "cancelled")
                        and not r.data.get("cleanup_incomplete"),
                        **data,
                    )
                    await asyncio.sleep(10.2)
                    check(
                        kind + "-descendant-gone",
                        not marker.exists()
                        and pid_file.exists()
                        and ps(
                            f"@(Get-Process -Id {pid_file.read_text()} "
                            "-ErrorAction SilentlyContinue).Count"
                        )
                        == "0",
                    )
            else:
                r, data = await invoke(backend, "patch-denied", arguments=patch, tool="apply_patch")
                check(label + "-patch-denied", r.error_code == "policy_denied", **data)
        finally:
            await backend.close()
            check(label + "-temp-cleanup", not temps[-1].exists())
            os.environ.pop("CUSTOM_SECRET", None)
            os.environ.pop("OPENAI_API_KEY", None)

    # Real one-call HITL patch; the original RO session remains RO after success.
    backend = WindowsNativeBackend(replace(policy, mode="read-only"))

    async def approve(request):
        return ApprovalDecision(request.request_id, "allow_once")

    gateway = SecurityGateway(backend.policy, backend=backend, approver=approve)
    registry = native_tools()
    registry["request_permissions"] = permission_tool()

    async def gw(name, arguments, ident):
        return await gateway.execute(
            ToolCall(ident, name, json.dumps(arguments)),
            registry,
            session,
            ExecutionContext(work, ident, PS),
            events,
        )

    try:
        r = await gw(
            "apply_patch",
            {"patch": "*** Begin Patch\n*** Add File: once.txt\n+once\n*** End Patch"},
            "denied",
        )
        check("hitl-initial-deny", r.error_code == "policy_denied")
        r = await gw(
            "request_permissions", {"denied_call_id": "denied", "reason": "acceptance"}, "once"
        )
        check("hitl-real-patch-once", r.ok and (work / "once.txt").exists(), result=asdict(r))
        r = await gw(
            "request_permissions", {"denied_call_id": "denied", "reason": "reuse"}, "reuse"
        )
        check("hitl-grant-not-reusable", r.error_code == "policy_denied")
        r, data = await invoke(backend, "ro-after-hitl", "Set-Content no.txt no -ErrorAction Stop")
        check("hitl-baseline-ro", not r.ok and not (work / "no.txt").exists(), **data)
    finally:
        await backend.close()
    # Junction cleanup must remove the link, not its target. Owned fixtures only.
    backend = WindowsNativeBackend(policy)
    r, _ = await invoke(backend, "junction-temp", "$env:TEMP")
    private = Path(r.data["stdout"].strip())
    await asyncio.to_thread(
        subprocess.run,
        [
            "node",
            "-e",
            "require('node:fs').symlinkSync(process.argv[1],process.argv[2],'junction')",
            str(outside),
            str(private / "owned-junction"),
        ],
        check=True,
    )
    await backend.close()
    check(
        "temp-child-junction-not-followed",
        not await asyncio.to_thread(private.exists)
        and (outside / "keep.txt").read_text() == "unchanged",
    )

    # Simulate a hard bridge crash. The guard must kill Pi's Job, fail closed,
    # and report that TEMP cleanup could not be confirmed. Do not repair ACLs.
    backend = WindowsNativeBackend(policy)
    r, _ = await invoke(backend, "crash-temp", "$env:TEMP")
    orphan = Path(r.data["stdout"].strip())
    marker = work / "crash.marker"
    ready = asyncio.Event()

    async def crash_watch(kind, data, **kw):
        await events(kind, data, **kw)
        if kind == "tool_output_delta" and "crash-ready" in data.get("text", ""):
            ready.set()

    command = python(
        'import time,pathlib; print("crash-ready",flush=True); '
        "time.sleep(4); pathlib.Path(" + repr(str(marker)) + ').write_text("bad")'
    )
    task = asyncio.create_task(invoke(backend, "crash", command, emit=crash_watch))
    await asyncio.wait_for(ready.wait(), 15)
    backend.client.process.kill()
    r, data = await task
    await asyncio.sleep(4.3)
    check("bridge-crash-job-stopped", not marker.exists() and backend.status == "failed", **data)
    r, data = await invoke(backend, "after-crash", "Set-Content after-crash.txt bad")
    check(
        "bridge-crash-fail-closed",
        r.error_code == "backend_unavailable" and not (work / "after-crash.txt").exists(),
        **data,
    )
    check(
        "crash-temp-residual-reported",
        await asyncio.to_thread(orphan.exists),
        retained_temp=str(orphan),
        cleanup="NOT CONFIRMED after forced process kill; no automatic reuse or ACL repair",
    )
    writer.close()
    logs = list((root / "audit").rglob("*.jsonl"))
    audit = "\n".join(p.read_text(encoding="utf-8") for p in logs)
    check(
        "durable-patch-approval-audit",
        "patch_diff" in audit and "approval_resolved" in audit,
        paths=[str(p) for p in logs],
    )
    report["protected_after"] = snapshots()
    check("protected-acls-unchanged", before == report["protected_after"])
    check("all-session-temps-removed", all(not p.exists() for p in temps))
    save()
    if not all(row["pass"] for row in report["rows"]):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
