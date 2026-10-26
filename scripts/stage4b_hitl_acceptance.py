"""Opt-in real SRT one-shot patch with typed TUI input, no model calls."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from prompt_toolkit import PromptSession
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from nexus.app.config import default_shell
from nexus.app.input import TaskInput
from nexus.core.security import ApprovalDecision, ApprovalRequest, SecurityGateway, SecurityPolicy
from nexus.core.types import ExecutionContext, Json, Session, ToolCall
from nexus.tools.registry import native_tools, permission_tool
from nexus.tools.sandbox import SRTBackend


async def run(output: Path, temp_root: Path) -> None:
    # mkdtemp's Windows owner-only ACL does not survive a change of file owner
    # when a second OS account creates files. Use ordinary inherited permissions.
    root = temp_root / ("nexus-stage4b-hitl-" + uuid4().hex)
    root.mkdir()
    workspace = root / "work"
    workspace.mkdir()
    session = Session(workspace)
    session.run_id = "hitl-real-srt"
    rows: list[Json] = []
    policy = SecurityPolicy(mode="read-only")
    backend = SRTBackend(policy)
    registry = {**native_tools(), "request_permissions": permission_tool()}

    async def emit(kind: str, data: Json) -> None:
        rows.append({"event": kind, "data": data})

    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        editor = TaskInput(PromptSession(output=DummyOutput()))
        typed_choice = "deny"

        async def approve(request: ApprovalRequest) -> ApprovalDecision:
            # Exercise the actual terminal input handler; input is scripted, not
            # a claim that a human manually clicked or typed during acceptance.
            with editor.busy(lambda: None):
                pending = asyncio.create_task(editor.approve())
                await asyncio.sleep(0.05)
                pipe.send_text(typed_choice + "\r")
                choice = await asyncio.wait_for(pending, 5)
            return ApprovalDecision(
                request.request_id, "allow_once" if choice == "allow" else "deny"
            )

        gateway = SecurityGateway(policy, backend=backend, approver=approve)

        async def invoke(ident: str, name: str, args: Json) -> None:
            started = time.perf_counter()
            result = await gateway.execute(
                ToolCall(ident, name, json.dumps(args)),
                registry,
                session,
                ExecutionContext(workspace, ident, default_shell()),
                emit,
            )
            row = {
                "test": ident,
                "elapsed_ms": (time.perf_counter() - started) * 1000,
                "result": asdict(result),
            }
            rows.append(row)
            print(json.dumps(row), flush=True)

        patch = {"patch": "*** Begin Patch\n*** Add File: one.txt\n+approved once\n*** End Patch"}
        try:
            await invoke("initial-read-only", "exec_command", {"command": "whoami"})
            await invoke("denied-first", "apply_patch", patch)
            await invoke(
                "user-deny",
                "request_permissions",
                {"denied_call_id": "denied-first", "reason": "Create one file"},
            )
            rows.append({"test": "deny-no-file", "passed": not (workspace / "one.txt").exists()})
            await invoke("denied-again", "apply_patch", patch)
            typed_choice = "allow"
            await invoke(
                "user-allow-once",
                "request_permissions",
                {"denied_call_id": "denied-again", "reason": "Create one file once"},
            )
            await invoke(
                "cannot-reuse",
                "request_permissions",
                {"denied_call_id": "denied-again", "reason": "Reuse forbidden"},
            )
            await invoke(
                "baseline-read", "exec_command", {"command": "Get-Content one.txt; whoami"}
            )
            await invoke(
                "baseline-deny-write",
                "exec_command",
                {"command": "Set-Content forbidden.txt no -ErrorAction Stop"},
            )
            import sys

            await invoke(
                "default-network-denied",
                "exec_command",
                {
                    "command": f"& '{sys.executable}' -c \"import urllib.request; "
                    "print(urllib.request.urlopen('https://example.com', timeout=10).status)\""
                },
            )
            rows.append(
                {
                    "test": "host-files",
                    "approved_file": (workspace / "one.txt").read_text()
                    if (workspace / "one.txt").exists()
                    else None,
                    "baseline_write_denied": not (workspace / "forbidden.txt").exists(),
                }
            )
        finally:
            try:
                await backend.close()
                rows.append({"test": "close", "confirmed": True})
            except OSError as exc:
                rows.append({"test": "close", "confirmed": False, "detail": str(exc)})
            await asyncio.to_thread(
                output.write_text,
                json.dumps({"root": str(root), "rows": rows}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--temp-root", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(run(args.output, args.temp_root))
