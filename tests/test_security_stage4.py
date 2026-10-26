from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from pathlib import Path

import pytest

from nexus.app.bootstrap import Conversation
from nexus.app.config import Config, ConfigError, ModelConfig, load_config
from nexus.app.events import Events
from nexus.app.memory import MemoryStore
from nexus.app.session import SessionLog, resume_session
from nexus.core.agent import run_turn
from nexus.core.security import (
    ApprovalDecision,
    ApprovalRequest,
    Mode,
    SecurityGateway,
    SecurityPolicy,
)
from nexus.core.types import (
    Emit,
    ExecutionContext,
    Json,
    Limits,
    Message,
    RuntimeEvent,
    Session,
    Tool,
    ToolCancelled,
    ToolResult,
    ToolSpec,
)
from nexus.tools.memory import create_memory_tools
from nexus.tools.registry import default_tools, native_tools
from nexus.tools.sandbox import SRTBackend
from tests.conftest import Recorder, ScriptedModel, call, reply


class Backend:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def execute(
        self,
        name: str,
        arguments: Json,
        context: ExecutionContext,
        emit: Emit,
    ) -> ToolResult:
        self.calls.append(name)
        return ToolResult(context.call_id, True, {"mock_backend": True})


@pytest.mark.parametrize("mode", ["local", "read-only", "workspace-write"])
async def test_loop_routes_and_denies_unknown(tmp_path: Path, mode: Mode) -> None:
    session = Session(tmp_path)
    backend = Backend()
    registry = default_tools(session)
    model = ScriptedModel(
        [
            reply(
                call(
                    "apply_patch", {"patch": "*** Begin Patch\n*** Add File: a\n+x\n*** End Patch"}
                ),
                call("update_plan", {"plan": [{"step": "inspect", "status": "in_progress"}]}, "p"),
                call("unknown", {}, "u"),
            ),
            reply(text="done"),
        ]
    )
    result = await run_turn(
        session,
        "test",
        model,
        registry,
        Recorder(),
        Limits(),
        gateway=SecurityGateway(SecurityPolicy(mode), backend=backend),
    )
    assert result.outcome == "completed"
    values = [json.loads(m.content) for m in session.messages if m.role == "tool"]
    assert values[-1]["error_code"] == "unknown_tool"
    assert values[1]["ok"]
    assert (tmp_path / "a").exists() == (mode == "local")
    assert backend.calls == (["apply_patch"] if mode == "workspace-write" else [])
    assert values[0]["ok"] == (mode != "read-only")


@pytest.mark.parametrize("mode", ["read-only", "workspace-write"])
async def test_production_backend_missing_installation_fails_closed(
    tmp_path: Path, mode: Mode
) -> None:
    gateway = SecurityGateway(
        SecurityPolicy(mode),
        backend=SRTBackend(SecurityPolicy(mode, node_path=str(tmp_path / "missing-node.exe"))),
    )
    value = await gateway.execute(
        call("exec_command", {"command": "must not run"}),
        native_tools(),
        Session(tmp_path),
        ExecutionContext(tmp_path, "c1", "missing-shell"),
        Recorder(),
    )
    assert value.error_code == "backend_unavailable"
    assert value.data["execution_state"] == "not_executed"
    denied = await SRTBackend().request_write_access(
        (str(tmp_path),), ExecutionContext(tmp_path, "c1", "")
    )
    assert denied.error_code == "dynamic_scope_unsupported"


async def test_forged_tool_name_does_not_confer_trust(tmp_path: Path) -> None:
    backend = Backend()

    async def forbidden(args: Json, ctx: ExecutionContext, emit: Emit) -> ToolResult:
        pytest.fail("Untrusted implementation invoked")

    registry = {"update_plan": Tool(ToolSpec("update_plan", "trusted", {}), forbidden)}
    value = await SecurityGateway(SecurityPolicy("workspace-write"), backend=backend).execute(
        call("update_plan", {}),
        registry,
        Session(tmp_path),
        ExecutionContext(tmp_path, "c1", ""),
        Recorder(),
    )
    assert value.error_code == "policy_denied"


@pytest.mark.parametrize("choice", ["allow_once", "deny", "cancel", "wrong_id", "timeout"])
async def test_approval_scope_lifetime_and_durable_order(tmp_path: Path, choice: str) -> None:
    session = Session(tmp_path, run_id="run")
    recorder = Recorder()
    backend = Backend()

    async def approve(request: ApprovalRequest) -> ApprovalDecision:
        assert (request.session_id, request.run_id, request.call_id) == (
            session.session_id,
            "run",
            "c1",
        )
        assert len(request.arguments_digest) == 64
        assert json.loads(request.capability_scope)["backend"] == "restricted"
        assert recorder.events[-1][0] == "approval_requested"
        assert "arguments_preview" not in recorder.events[-1][1]
        if choice == "timeout":
            await asyncio.sleep(1)
        return ApprovalDecision(
            "wrong" if choice == "wrong_id" else request.request_id,
            "allow_once"
            if choice in {"allow_once", "wrong_id"}
            else "cancel"
            if choice == "cancel"
            else "deny",
        )

    gateway = SecurityGateway(
        SecurityPolicy("workspace-write", ask_tools=("exec_command",)),
        backend=backend,
        approver=approve,
        approval_timeout=0.03,
    )

    async def run() -> ToolResult:
        return await gateway.execute(
            call("exec_command", {"command": "x"}),
            native_tools(),
            session,
            ExecutionContext(tmp_path, "c1", ""),
            recorder,
        )

    if choice == "cancel":
        with pytest.raises(ToolCancelled):
            await run()
    else:
        value = await run()
        assert value.ok == (choice == "allow_once")
    assert backend.calls == (["exec_command"] if choice == "allow_once" else [])
    kinds = [k for k, _ in recorder.events]
    if backend.calls:
        assert kinds.index("approval_resolved") < kinds.index("tool_execution")


async def test_mutated_arguments_and_reused_decision_rejected(tmp_path: Path) -> None:
    session = Session(tmp_path, run_id="run")
    target = call("exec_command", {"command": "first"})
    backend = Backend()
    answers: list[ApprovalDecision] = []

    async def approve(request: ApprovalRequest) -> ApprovalDecision:
        if not answers:
            answers.append(ApprovalDecision(request.request_id, "allow_once"))
            target.arguments_json = '{"command":"changed"}'
        return answers[0]

    gateway = SecurityGateway(
        SecurityPolicy("workspace-write", ask_tools=("exec_command",)),
        backend=backend,
        approver=approve,
    )
    for _ in range(2):
        value = await gateway.execute(
            target, native_tools(), session, ExecutionContext(tmp_path, "c1", ""), Recorder()
        )
        assert value.error_code == "approval_invalid"
    assert not backend.calls


async def test_audit_failure_prevents_dispatch(tmp_path: Path) -> None:
    backend = Backend()

    async def approve(request: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(request.request_id, "allow_once")

    async def broken(kind: str, data: Json, **_: object) -> int:
        if kind == "approval_resolved":
            raise OSError("disk full")
        return 1

    gateway = SecurityGateway(
        SecurityPolicy("workspace-write", ask_tools=("exec_command",)),
        backend=backend,
        approver=approve,
    )
    with pytest.raises(OSError):
        await gateway.execute(
            call("exec_command", {"command": "x"}),
            native_tools(),
            Session(tmp_path, run_id="r"),
            ExecutionContext(tmp_path, "c1", ""),
            broken,
        )
    assert not backend.calls


async def test_external_cancel_during_approval_is_not_executed(tmp_path: Path) -> None:
    ready = asyncio.Event()
    backend = Backend()
    recorder = Recorder()

    async def approve(request: ApprovalRequest) -> ApprovalDecision:
        ready.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    gateway = SecurityGateway(
        SecurityPolicy("workspace-write", ask_tools=("exec_command",)),
        backend=backend,
        approver=approve,
    )
    task = asyncio.create_task(
        gateway.execute(
            call("exec_command", {"command": "x"}),
            native_tools(),
            Session(tmp_path, run_id="r"),
            ExecutionContext(tmp_path, "c1", ""),
            recorder,
        )
    )
    await asyncio.wait_for(ready.wait(), 2)
    task.cancel()
    with pytest.raises(ToolCancelled) as caught:
        await task
    assert caught.value.result.data["execution_state"] == "not_executed"
    assert recorder.events[-1][1]["choice"] == "cancel"
    assert not backend.calls


async def test_memory_allow_once_uses_host_without_starting_index(tmp_path: Path) -> None:
    root = tmp_path / "memory"
    registry = create_memory_tools(MemoryStore(root))

    async def approve(request: ApprovalRequest) -> ApprovalDecision:
        assert json.loads(request.capability_scope)["backend"] == "host-memory"
        return ApprovalDecision(request.request_id, "allow_once")

    gateway = SecurityGateway(
        SecurityPolicy("workspace-write", allow_host_memory=True), approver=approve
    )
    value = await gateway.execute(
        call("memory_write", {"content": "fixed test fact"}),
        registry,
        Session(tmp_path, run_id="r"),
        ExecutionContext(tmp_path, "c1", "", 4096),
        Recorder(),
    )
    assert value.ok
    assert "fixed test fact" in (root / "MEMORY.md").read_text(encoding="utf-8")
    assert not (root / ".index.json").exists()


async def test_restricted_turn_does_not_connect_mcp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nexus.app import bootstrap

    monkeypatch.setenv("STAGE4_TEST_KEY", "test-only")

    class Model(ScriptedModel):
        async def close(self) -> None:
            pass

    async def forbidden(*args: object) -> list[str]:
        pytest.fail("Restricted mode must not connect MCP")

    model = Model([reply(text="done")])
    monkeypatch.setattr(bootstrap, "ChatModel", lambda _: model)
    monkeypatch.setattr(bootstrap, "connect_servers", forbidden)
    cfg = Config(
        ModelConfig("fake", 32768, api_key_env="STAGE4_TEST_KEY"),
        Limits(),
        mcp_servers={"test-server": {"command": "must-not-run"}},
        security=SecurityPolicy("workspace-write"),
    )

    async def consume(event: RuntimeEvent) -> None:
        pass

    conversation = Conversation(tmp_path, cfg, consume, home=tmp_path / "home")
    try:
        assert (await conversation.turn("test")).outcome == "completed"
        assert conversation.unavailable == ["test-server"]
        assert not (tmp_path / "home/memory").exists()
        assert any("MCP" in m.content and "disabled" in m.content for m in model.requests[0])
    finally:
        await conversation.close()


@pytest.mark.parametrize(
    "mode,allow,tool,args,expected",
    [
        ("workspace-write", False, "memory_read", {}, "policy_denied"),
        ("read-only", True, "memory_write", {"content": "x"}, "policy_denied"),
        ("workspace-write", True, "memory_search", {"query": "x"}, "policy_denied"),
        ("workspace-write", True, "memory_write", {"content": "x"}, "approval_unavailable"),
        ("read-only", True, "scratchpad", {"action": "list"}, "approval_unavailable"),
        ("read-only", True, "scratchpad", {"action": "add", "content": "x"}, "policy_denied"),
    ],
)
async def test_memory_policy(
    tmp_path: Path, mode: Mode, allow: bool, tool: str, args: Json, expected: str
) -> None:
    root = tmp_path / "memory"
    registry = create_memory_tools(MemoryStore(root))
    gateway = SecurityGateway(SecurityPolicy(mode, allow_host_memory=allow))
    value = await gateway.execute(
        call(tool, args),
        registry,
        Session(tmp_path),
        ExecutionContext(tmp_path, "c1", ""),
        Recorder(),
    )
    assert value.error_code == expected
    assert not root.exists()


async def test_restricted_bootstrap_no_background_side_effects(tmp_path: Path) -> None:
    async def consume(event: RuntimeEvent) -> None:
        pass

    config = Config(
        ModelConfig("fake", 32768),
        Limits(),
        mcp_servers={"bad": {}},
        security=SecurityPolicy("read-only"),
    )
    conversation = Conversation(tmp_path, config, consume, home=tmp_path / "home")
    assert conversation.memory_index is None
    assert conversation.context.before_compact is None
    assert await conversation.memory_prefix() == ""
    await conversation.start_memory()
    await conversation.summarize_exit("slash-exit")
    assert not (tmp_path / "home").exists()
    assert conversation.gateway.policy.mode == "read-only"
    await conversation.close()


@pytest.mark.parametrize("dispatched", [False, True])
async def test_resume_never_restores_approval(tmp_path: Path, dispatched: bool) -> None:
    session = Session(tmp_path, run_id="run")
    writer = SessionLog.create(session, "test", {}, tmp_path / "home")

    async def consume(event: RuntimeEvent) -> None:
        pass

    events = Events(session, writer, consume, ("secret-value",))
    await events("run_started", {"execution_mode": "workspace-write"})
    await events("message", Message("assistant", tool_calls=[call("exec_command", {})]).public())
    request = ApprovalRequest(
        "request",
        session.session_id,
        "run",
        "c1",
        "exec_command",
        "digest",
        "restricted",
        123,
        "secret-value",
    )
    await events("approval_requested", asdict(request))
    await events("approval_resolved", {**asdict(request), "choice": "allow_once"})
    if dispatched:
        await events("tool_execution", {"call_id": "c1", "state": "dispatching"})
    path = writer.path
    writer.close()
    assert "secret-value" not in path.read_text(encoding="utf-8")
    restored, reopened, _ = resume_session(path, tmp_path, tmp_path / "home")
    reopened.close()
    value = json.loads(restored.messages[-1].content)
    assert value["error_code"] == ("interrupted_unknown" if dispatched else "not_executed")
    assert SecurityGateway().approver is None


def test_security_config_validation(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    base = '[model]\nname="fake"\n[security]\n'
    path.write_text(base + 'mode="workspace-write"\nask_tools=["exec_command"]\n')
    assert load_config(path, require_key=False).security.ask_tools == ("exec_command",)
    for invalid in [
        'mode="unsafe"',
        "mode=[]",
        'writable_roots=["relative"]',
        'allow_host_memory="yes"',
        "accept_unverified_backend=true",
    ]:
        path.write_text(base + invalid)
        with pytest.raises(ConfigError):
            load_config(path, require_key=False)
