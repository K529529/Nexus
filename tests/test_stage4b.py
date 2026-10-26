from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nexus.core.security import ApprovalDecision, ApprovalRequest, SecurityGateway, SecurityPolicy
from nexus.core.types import Emit, ExecutionContext, Json, Session, ToolResult
from nexus.tools.registry import native_tools, permission_tool
from tests.conftest import Recorder, call


async def test_failed_srt_close_still_releases_conversation_resources() -> None:
    from nexus.app.bootstrap import Conversation

    closed: list[str] = []

    async def backend_close() -> None:
        raise OSError("SRT reset failed")

    async def memory_close() -> None:
        closed.append("memory")

    async def resources_close() -> None:
        closed.append("resources")

    conversation = SimpleNamespace(
        closed=False,
        gateway=SimpleNamespace(backend=SimpleNamespace(close=backend_close)),
        memory_index=SimpleNamespace(close=memory_close),
        resources=SimpleNamespace(aclose=resources_close),
        writer=SimpleNamespace(close=lambda: closed.append("writer")),
    )
    with pytest.raises(OSError, match="SRT reset failed"):
        await Conversation.close(conversation)  # type: ignore[arg-type]
    assert closed == ["memory", "resources", "writer"]


async def test_unknown_patch_ends_holder_immediately(tmp_path: Path) -> None:
    from nexus.tools.sandbox import SRTBackend

    class Client:
        closed = False

        async def close(self) -> None:
            self.closed = True

    backend = SRTBackend(SecurityPolicy(mode="workspace-write"))
    client = Client()
    backend.client = client  # type: ignore[assignment]
    result = ToolResult(
        "c", False, {"execution_state": "interrupted_unknown"}, "cleanup_incomplete"
    )
    assert await backend.finish_stop(result) is result
    assert client.closed and backend.status == "failed"


@pytest.mark.parametrize("confirmed", [True, False])
async def test_cancel_requires_holder_exit_before_new_session(
    tmp_path: Path, confirmed: bool
) -> None:
    from nexus.tools.sandbox import SRTBackend

    class Client:
        cleanup_confirmed = False
        closed = False

        async def close(self) -> None:
            self.closed = True
            self.cleanup_confirmed = confirmed

    backend = SRTBackend(SecurityPolicy(mode="workspace-write"))
    client = Client()
    backend.client = client  # type: ignore[assignment]
    result = await backend.finish_stop(
        ToolResult("c", False, {"requires_reset": True}, "cancelled")
    )
    assert client.closed
    if confirmed:
        assert backend.client is None and backend.status == "not_initialized"
        assert result.error_code == "cancelled"
    else:
        assert backend.status == "failed" and result.error_code == "cleanup_incomplete"


async def test_forged_patch_request_is_denied_without_approval(tmp_path: Path) -> None:
    backend = ScopedBackend()
    gateway = SecurityGateway(SecurityPolicy(mode="read-only"), backend=backend)
    session = Session(tmp_path)
    session.run_id = "run"
    registry = {**native_tools(), "request_permissions": permission_tool()}
    context = ExecutionContext(tmp_path, "c1", "")
    await gateway.execute(
        call("apply_patch", {"patch": "original"}), registry, session, context, Recorder()
    )
    value = await gateway.execute(
        call(
            "request_permissions",
            {"denied_call_id": "c1", "reason": "change scope", "patch": "forged"},
        ),
        registry,
        session,
        context,
        Recorder(),
    )
    assert value.error_code == "policy_denied" and not backend.calls


@pytest.mark.parametrize(
    "arguments",
    [
        ["--permission-mode", "read-only"],
        ["--permission-mode", "read-only", "exec", "task"],
        ["exec", "task", "--permission-mode", "read-only"],
        ["resume", "--permission-mode", "read-only"],
    ],
)
def test_cli_mode_positions(monkeypatch: Any, arguments: list[str]) -> None:
    from nexus.app import cli

    async def application(args: argparse.Namespace) -> int:
        assert args.permission_mode == "read-only"
        return 0

    monkeypatch.setattr(cli, "application", application)
    monkeypatch.setattr(sys, "argv", ["nexus", *arguments])
    with pytest.raises(SystemExit) as result:
        cli.main()
    assert result.value.code == 0


@pytest.mark.parametrize("key,expected", [("1", "local"), ("3", "workspace-write"), ("\x1b", None)])
async def test_permission_selector_keys(key: str, expected: str | None) -> None:
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from nexus.app.tui import select_permissions

    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        task = asyncio.create_task(select_permissions("read-only", "not_initialized", "lazy"))
        await asyncio.sleep(0.05)
        pipe.send_text(key)
        assert await asyncio.wait_for(task, 3) == expected


class ScopedBackend:
    def __init__(self) -> None:
        self.calls: list[Json] = []

    async def execute(self, name: str, args: Json, ctx: ExecutionContext, emit: Emit) -> ToolResult:
        return ToolResult(ctx.call_id, False, {}, "command_exit_nonzero")

    async def execute_patch_once(self, args: Json, ctx: ExecutionContext, emit: Emit) -> ToolResult:
        self.calls.append(args)
        return ToolResult(ctx.call_id, True, {"applied": True})


@pytest.mark.parametrize(
    "domain", ["https://example.com", "*", "example.com:443", "x/y", "a..b", "x\n"]
)
def test_domains_fail_closed(domain: str) -> None:
    with pytest.raises(ValueError):
        SecurityPolicy(allowed_domains=(domain,))


@pytest.mark.parametrize("choice", ["allow_once", "deny", "cancel"])
async def test_scoped_patch_binds_original_and_consumes_denial(tmp_path: Path, choice: str) -> None:
    from nexus.core.types import ToolCancelled

    backend = ScopedBackend()
    requests: list[ApprovalRequest] = []

    async def approve(request: ApprovalRequest) -> ApprovalDecision:
        requests.append(request)
        return ApprovalDecision(request.request_id, choice)  # type: ignore[arg-type]

    gateway = SecurityGateway(SecurityPolicy(mode="read-only"), backend=backend, approver=approve)
    session = Session(tmp_path)
    session.run_id = "run"
    registry = {**native_tools(), "request_permissions": permission_tool()}
    original = {"patch": "*** Begin Patch\n*** Add File: one\n+hello\n*** End Patch"}
    denied = await gateway.execute(
        call("apply_patch", original, "denied"),
        registry,
        session,
        ExecutionContext(tmp_path, "denied", ""),
        Recorder(),
    )
    assert denied.error_code == "policy_denied" and not backend.calls
    request = call(
        "request_permissions", {"denied_call_id": "denied", "reason": "Create file once"}
    )
    try:
        value = await gateway.execute(
            request, registry, session, ExecutionContext(tmp_path, "c1", ""), Recorder()
        )
        assert value.ok == (choice == "allow_once")
    except ToolCancelled:
        assert choice == "cancel"
    assert backend.calls == ([original] if choice == "allow_once" else [])
    assert len(requests) == 1
    scope = json.loads(requests[0].capability_scope)
    assert scope["tool"] == "apply_patch" and "operation" not in scope
    assert json.loads(requests[0].arguments_preview)["operation"] == original
    assert scope["denial_source"] == "gateway_read_only_policy"
    again = await gateway.execute(
        request, registry, session, ExecutionContext(tmp_path, "c1", ""), Recorder()
    )
    assert again.error_code == "policy_denied" and len(requests) == 1


async def test_nonzero_shell_and_old_run_cannot_authorize(tmp_path: Path) -> None:
    backend = ScopedBackend()
    gateway = SecurityGateway(SecurityPolicy(mode="read-only"), backend=backend)
    registry = {**native_tools(), "request_permissions": permission_tool()}
    session = Session(tmp_path)
    session.run_id = "one"
    context = ExecutionContext(tmp_path, "c1", "")
    await gateway.execute(
        call("exec_command", {"command": "exit 1"}), registry, session, context, Recorder()
    )
    request = call("request_permissions", {"denied_call_id": "c1", "reason": "Please"})
    value = await gateway.execute(request, registry, session, context, Recorder())
    assert value.error_code == "policy_denied"
    await gateway.execute(
        call("apply_patch", {"patch": "anything"}), registry, session, context, Recorder()
    )
    session.run_id = "two"
    value = await gateway.execute(request, registry, session, context, Recorder())
    assert value.error_code == "policy_denied" and not backend.calls
