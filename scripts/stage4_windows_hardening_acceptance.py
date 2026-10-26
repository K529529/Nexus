"""Bounded real Native regression; no model, command rewriting, or ACL repair.

The exact requested Python program is written verbatim to an owned test file.
P0 failures remain failures. UI tests press real TUI keys in a controlled fixture,
and do not stand in for the user's later manual acceptance.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, replace
from pathlib import Path
from uuid import uuid4

from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from nexus.app.acl_consent import ensure_workspace_consent
from nexus.app.config import ConfigError, load_config
from nexus.app.tui import confirm_workspace_acl
from nexus.core.security import SecurityPolicy
from nexus.core.types import ExecutionContext
from nexus.tools.windows_native import WindowsNativeBackend

PS = os.environ["SystemRoot"] + "/System32/WindowsPowerShell/v1.0/powershell.exe"
PROGRAM = """from pathlib import Path
from tempfile import TemporaryDirectory

with TemporaryDirectory() as root:
    target = Path(root) / "private"
    target.mkdir(mode=0o700)

    file = target / "hello.txt"
    file.write_text("hello", encoding="utf-8")

    assert file.read_text(encoding="utf-8") == "hello"
"""


def quote(path):
    return "'" + str(path).replace("'", "''") + "'"


def ps(code):
    env = {k: v for k, v in os.environ.items() if k.upper() != "PSMODULEPATH"}
    return subprocess.check_output(
        [PS, "-NoProfile", "-NonInteractive", "-Command", code], env=env, encoding="utf-8"
    ).strip()


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--parent", type=Path, required=True)
    parser.add_argument("--koffi", required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    root = args.parent.resolve(strict=True) / ("nexus-hardening-" + uuid4().hex)
    root.mkdir()
    sid = ps("[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value")
    await asyncio.to_thread(
        subprocess.run,
        ["icacls.exe", str(root), "/grant", f"*{sid}:(OI)(CI)F"],
        check=True,
        capture_output=True,
    )
    work = root / "work"
    work.mkdir()
    authority = root / "trusted-config.toml"
    authority.write_text('[model]\nname="unused"\n[memory]\nenabled=false\n')
    (work / "requested.py").write_text(PROGRAM, encoding="utf-8")
    (work / "test_private.py").write_text(
        "def test_temp(tmp_path, tmp_path_factory):\n"
        '    (tmp_path/"one").write_text("ok")\n'
        '    (tmp_path_factory.mktemp("factory")/"two").write_text("ok")\n'
    )
    report = {
        "root": str(root),
        "python": sys.executable,
        "python_version": sys.version,
        "rows": [],
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)

    def record(name, passed, **data):
        report["rows"].append(dict(test=name, passed=bool(passed), **data))
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(name, "PASS" if passed else "FAIL", flush=True)

    async def emit(*a, **kw):
        pass

    async def run(backend, name, command):
        start = time.perf_counter()
        r = await backend.execute(
            "exec_command",
            {"command": command, "timeout_ms": 20000},
            ExecutionContext(work, uuid4().hex, PS),
            emit,
        )
        return r, dict(result=asdict(r), elapsed_ms=round((time.perf_counter() - start) * 1000, 2))

    async def answer(key, expected):
        async def confirm(path):
            assert path == work
            with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
                task = asyncio.create_task(confirm_workspace_acl(path))
                await asyncio.sleep(0.1)
                pipe.send_text(key)
                result = await asyncio.wait_for(task, 5)
                assert result is expected
                return result

        return confirm

    initial = SecurityPolicy("workspace-write", windows_koffi=args.koffi)
    before = ps(f"(Get-Acl -LiteralPath {quote(work)}).Sddl")
    original = authority.read_bytes()
    for key in ["d", "\x1b"]:
        value = await ensure_workspace_consent(
            work, initial, path=authority, confirm=await answer(key, False)
        )
        record(
            "deny-or-cancel-" + repr(key),
            value is None
            and authority.read_bytes() == original
            and ps(f"(Get-Acl -LiteralPath {quote(work)}).Sddl") == before,
        )
    try:
        await ensure_workspace_consent(work, initial, path=authority)
        record("noninteractive-denied", False)
    except ConfigError as e:
        record("noninteractive-denied", "interactive Nexus" in str(e), detail=str(e))
    approved = await ensure_workspace_consent(
        work, initial, path=authority, confirm=await answer("a", True)
    )
    record(
        "allow-persists-no-acl-yet",
        approved is not None and ps(f"(Get-Acl -LiteralPath {quote(work)}).Sddl") == before,
    )
    assert approved
    reloaded = load_config(authority, require_key=False)
    policy = replace(initial, windows_acl_roots=reloaded.security.windows_acl_roots)

    async def forbidden(path):
        raise AssertionError("Repeated prompt")

    again = await ensure_workspace_consent(work, policy, path=authority, confirm=forbidden)
    record("restart-reuses-exact-consent", again == policy)
    for mode in ["read-only", "workspace-write"]:
        first = WindowsNativeBackend(replace(policy, mode=mode))
        second = WindowsNativeBackend(replace(policy, mode=mode))
        temp_a = temp_b = None
        try:
            r, data = await run(first, "temp-a", "$env:TEMP")
            record(mode + "-session-a-start", r.ok, **data)
            temp_a = Path(r.data["stdout"].strip())
            r, data = await run(second, "temp-b", "$env:TEMP")
            record(mode + "-session-b-start", r.ok, **data)
            temp_b = Path(r.data["stdout"].strip())
            record(mode + "-unique-temp", temp_a != temp_b)
            r, data = await run(
                second,
                "cross-session",
                f"Set-Content {quote(temp_a / 'foreign.txt')} no -ErrorAction Stop",
            )
            record(
                mode + "-cross-session-write-denied",
                not r.ok and not (temp_a / "foreign.txt").exists(),
                **data,
            )
            r, data = await run(first, "exact-python", f"& {quote(sys.executable)} requested.py")
            record(mode + "-exact-requested-python", r.ok, **data)
            r, data = await run(
                first,
                "pytest",
                f"& {quote(sys.executable)} -m pytest -q -p no:cacheprovider test_private.py",
            )
            record(mode + "-pytest-tmp-path-and-factory", r.ok, **data)
            r, data = await run(
                first, "after-error", "Set-Content persistent.txt ok -ErrorAction Stop"
            )
            record(
                mode + "-boundary-after-python-error", r.ok == (mode == "workspace-write"), **data
            )
            await first.close()
            r, data = await run(
                second,
                "still-active",
                "Set-Content (Join-Path $env:TEMP 'alive.txt') yes -ErrorAction Stop",
            )
            record(mode + "-second-survives-first-close", r.ok and not temp_a.exists(), **data)
        finally:
            await first.close()
            await second.close()
            record(
                mode + "-temps-removed", all(p is None or not p.exists() for p in [temp_a, temp_b])
            )
    record(
        "persistent-consent-still-exact",
        load_config(authority, require_key=False).security.windows_acl_roots == (str(work),),
    )
    if not all(row["passed"] for row in report["rows"]):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
