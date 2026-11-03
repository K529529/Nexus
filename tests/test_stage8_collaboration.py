from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from mcp.types import CallToolResult, TextContent

from nexus.app import bootstrap
from nexus.app.config import AgentRoleConfig, MultiAgentConfig
from nexus.core.types import (
    Emit,
    ExecutionContext,
    Json,
    Message,
    ModelReply,
    Tool,
    ToolResult,
    ToolSpec,
)
from nexus.tools.execution import execute
from nexus.tools.mcp import adapt_tool
from tests.conftest import Recorder, ScriptedModel, call, python_command, reply
from tests.test_cli_ux import until
from tests.test_conversation import config, ignore


class WaitingModel(ScriptedModel):
    def __init__(self, _: Any) -> None:
        super().__init__([reply(text="unused")])

    async def complete(self, messages: list[Message], tools: list[ToolSpec],
                       emit: Emit) -> ModelReply:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def close(self) -> None:
        pass


def root_conversation(tmp_path: Path, monkeypatch: Any) -> bootstrap.Conversation:
    monkeypatch.setattr(bootstrap, "ChatModel", WaitingModel)
    cfg = replace(config(monkeypatch), memory_enabled=False,
                  multi_agent=MultiAgentConfig(enabled=True, allowNestedSubagents=True))
    root = bootstrap.Conversation(tmp_path, cfg, ignore, home=tmp_path / "home")
    root.ensure_events("task")
    return root


@pytest.mark.parametrize("role,allowed,denied", [
    ("worker", "reviewer", None), ("scout", "scout", "worker"),
    ("reviewer", "reviewer", "worker"),
])
async def test_nested_roles_depth_and_tree_capacity(
    tmp_path: Path, monkeypatch: Any, role: str, allowed: str, denied: str | None,
) -> None:
    root = root_conversation(tmp_path, monkeypatch)
    manager = root.coordinator
    assert manager is not None
    try:
        parent = (await manager.spawn("root", {"role": role, "task": "parent", "scope": "a"}))[
            "agent_id"]
        await until(lambda: manager.agents[parent].conversation is not None)
        if denied:
            assert (await manager.spawn(parent, {"role": denied, "task": "x", "scope": "a"}))[
                "error"] == "role_delegation_denied"
        grand = (await manager.spawn(parent, {"role": allowed, "task": "child", "scope": "a"}))[
            "agent_id"]
        await until(lambda: manager.agents[grand].conversation is not None)
        assert manager.agents[grand].depth == 2
        assert "spawn_agent" not in manager.tools(grand)
        assert (await manager.spawn(grand, {"role": "scout", "task": "x", "scope": "a"}))[
            "error"] == "delegation_disabled"
        await manager.spawn("root", {"role": "worker", "task": "sibling", "scope": "b"})
        assert (await manager.spawn("root", {"role": "worker", "task": "full", "scope": "c"}))[
            "error"] == "capacity_full"
        await manager.cancel("root", parent)
        assert manager.agents[parent].state == manager.agents[grand].state == "aborted"
    finally:
        await root.close()


async def test_decision_and_user_guidance_wake_parent_without_deadlock(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    root = root_conversation(tmp_path, monkeypatch)
    manager = root.coordinator
    assert manager is not None and root.events is not None
    decision: asyncio.Task[Any] | None = None
    try:
        child = (await manager.spawn("root", {"role": "worker", "task": "work", "scope": "a"}))[
            "agent_id"]
        await until(lambda: manager.agents[child].conversation is not None)
        waiter = asyncio.create_task(manager.wait("root", 30))
        await asyncio.sleep(.01)
        decision = asyncio.create_task(manager.report(child, "which behavior?", True))
        status = await asyncio.wait_for(waiter, 2)
        assert status["guidance_pending"] and not decision.done()
        assert manager.agents[child].state == "waiting_decision"
        assert (await manager.message("unrelated", child, "no"))["error"] == "not_direct_child"
        assert (await manager.message("root", child, "choose A"))["received"]
        assert (await decision)["answer"] == "choose A"
        root.inbox.pending.clear()
        waiter = asyncio.create_task(manager.wait("root", 30))
        await asyncio.sleep(.01)
        await root.inbox.receive("user guidance", "guide", root.events)
        assert (await asyncio.wait_for(waiter, 2))["guidance_pending"]
    finally:
        if decision is not None and not decision.done():
            decision.cancel()
            await asyncio.gather(decision, return_exceptions=True)
        await root.close()


async def test_mcp_permission_intersection_independent_skills_and_failure_visibility(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    root = root_conversation(tmp_path, monkeypatch)
    manager = root.coordinator
    assert manager is not None
    root.config.mcp_servers = {"a": {}, "b": {}}
    root.config.multi_agent.roles["worker"] = AgentRoleConfig(mcp_servers=("a",))
    root.config.multi_agent.roles["scout"] = AgentRoleConfig(mcp_servers=("a", "b"))
    async def unused(args: Json, context: ExecutionContext, emit: Emit) -> ToolResult:
        return ToolResult(context.call_id, True, {})
    for name in ("a", "b"):
        root.registry[name] = Tool(ToolSpec(name, "test", {}), unused, server=name)
    try:
        child_id = (await manager.spawn("root", {"role": "worker", "task": "work", "scope": "a"}))[
            "agent_id"]
        await until(lambda: manager.agents[child_id].conversation is not None)
        child = manager.owner(child_id)
        assert "a" in child.registry and "b" not in child.registry
        assert child.registry["a"] is not root.registry["a"]
        assert child.catalog is not root.catalog
        assert child.session.loaded_skills == ()
        grand_id = (await manager.spawn(child_id, {"role": "scout", "task": "read", "scope": "a"}))[
            "agent_id"]
        await until(lambda: manager.agents[grand_id].conversation is not None)
        grand = manager.owner(grand_id)
        assert "a" in grand.registry and "b" not in grand.registry
        root.registry.pop("a")
        await manager.prepare(child_id)
        await manager.prepare(grand_id)
        assert "a" not in child.registry and "a" not in grand.registry
        assert any("MCP servers unavailable" in m.text for m in grand.inbox.guides())
    finally:
        await root.close()


async def test_mcp_serializes_same_server_without_blocking_another(tmp_path: Path) -> None:
    started: list[str] = []
    release = asyncio.Event()

    class Client:
        async def call_tool(self, name: str, *_: Any, **__: Any) -> CallToolResult:
            started.append(name)
            await release.wait()
            return CallToolResult(content=[TextContent(text=name)])

    client, lock = Client(), asyncio.Lock()
    first = adapt_tool(client, "one", ToolSpec("one", "", {}), "a", set(), {}, [], lock)
    second = adapt_tool(client, "two", ToolSpec("two", "", {}), "a", set(), {}, [], lock)
    other = adapt_tool(client, "other", ToolSpec("other", "", {}), "b", set(), {}, [],
                       asyncio.Lock())
    context = ExecutionContext(tmp_path, "call", "")
    tasks = [asyncio.ensure_future(t.execute({}, context, Recorder()))
             for t in (first, second, other)]
    try:
        await until(lambda: len(started) == 2)
        assert started == ["one", "other"]
        release.set()
        assert all(r.ok for r in await asyncio.gather(*tasks))
        assert started == ["one", "other", "two"]
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_cancel_agent_reaps_real_process_tree(tmp_path: Path, monkeypatch: Any) -> None:
    root = root_conversation(tmp_path, monkeypatch)
    ready = asyncio.Event()

    async def consume(event: Any) -> None:
        if event.kind == "subagent_event" and event.data["kind"] == "tool_output_delta":
            if "ready" in event.data["data"]["text"]:
                ready.set()

    root.consumer = consume

    class ShellModel(ScriptedModel):
        def __init__(self, _: Any) -> None:
            super().__init__([reply(call("exec_command", {"command": python_command(
                "import subprocess,sys,time,pathlib; "
                "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
                "pathlib.Path('child.pid').write_text(str(p.pid)); "
                "print('ready',flush=True); time.sleep(30)"
            )}))])

        async def close(self) -> None:
            pass

    monkeypatch.setattr(bootstrap, "ChatModel", ShellModel)
    manager = root.coordinator
    assert manager is not None
    try:
        ident = (await manager.spawn("root", {"role": "worker", "task": "test", "scope": "pid"}))[
            "agent_id"]
        await asyncio.wait_for(ready.wait(), 10)
        # Parent can still perform independent work while the child's shell sleeps.
        (tmp_path / "parent.txt").write_text("parent progressed", encoding="utf-8")
        result = await manager.cancel("root", ident)
        assert result["state"] == "aborted", result
        pid = int((tmp_path / "child.pid").read_text())
        check = (
            f"import ctypes; h=ctypes.windll.kernel32.OpenProcess(0x1000,False,{pid}); "
            "code=ctypes.c_ulong(); "
            "ctypes.windll.kernel32.GetExitCodeProcess(h,ctypes.byref(code)); "
            "assert not h or code.value != 259"
        ) if os.name == "nt" else (
            f"import pathlib; p=pathlib.Path('/proc/{pid}/stat'); "
            "assert not p.exists() or p.read_text().split()[2]=='Z'"
        )
        checked = await execute({"command": python_command(check)},
            ExecutionContext(tmp_path, "check", root.config.limits.shell), Recorder())
        assert checked.ok
    finally:
        await root.close()


async def test_cleanup_failure_retains_capacity(tmp_path: Path, monkeypatch: Any) -> None:
    root = root_conversation(tmp_path, monkeypatch)
    root.config.multi_agent.max_agents = 1
    manager = root.coordinator
    assert manager is not None
    args = {"role": "worker", "task": "test", "scope": "a"}
    ident = (await manager.spawn("root", args))["agent_id"]
    await until(lambda: manager.agents[ident].conversation is not None)
    child = manager.owner(ident)
    close = child.close

    async def failed_close() -> None:
        await close()
        raise OSError("simulated cleanup failure")

    monkeypatch.setattr(child, "close", failed_close)
    assert (await manager.cancel("root", ident))["state"] == "cleanup_failed"
    assert (await manager.spawn("root", args))["error"] == "capacity_full"
    with pytest.raises(RuntimeError, match="cleanup_incomplete"):
        await root.close()
