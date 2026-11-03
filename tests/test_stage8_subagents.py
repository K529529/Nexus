from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from nexus.app import bootstrap
from nexus.app.config import ConfigError, MultiAgentConfig, load_config
from nexus.app.session import list_sessions
from nexus.core.types import Emit, Message, ModelReply, ToolSpec
from tests.conftest import ScriptedModel, call, reply
from tests.test_conversation import config, ignore


async def test_disabled_has_no_delegation_tools_or_prompt(tmp_path: Path, monkeypatch: Any) -> None:
    class Model(ScriptedModel):
        def __init__(self, _: Any) -> None:
            super().__init__([reply(text="done")])

        async def close(self) -> None:
            pass

    monkeypatch.setattr(bootstrap, "ChatModel", Model)
    conversation = bootstrap.Conversation(
        tmp_path, replace(config(monkeypatch), memory_enabled=False), ignore, home=tmp_path,
    )
    try:
        assert conversation.coordinator is None
        assert "spawn_agent" not in conversation.registry
        assert (await conversation.turn("task")).outcome == "completed"
        assert "[Native collaboration]" not in conversation.session.messages[0].content
    finally:
        await conversation.close()


async def test_same_loop_fresh_concurrent_sessions_and_child_log(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    child_started, release = asyncio.Event(), asyncio.Event()
    clients: list[Any] = []

    class Model(ScriptedModel):
        def __init__(self, settings: Any) -> None:
            self.child = settings.reasoning_effort == "low"
            self.owner = asyncio.current_task()
            self.closed = False
            super().__init__([reply(text="child evidence")] if self.child else [
                reply(call("spawn_agent", {"role": "scout", "task": "inspect module",
                                           "scope": "read only"})),
                reply(call("wait_agents", {"timeout_seconds": 5}, "wait")),
                reply(text="integrated"),
            ])
            clients.append(self)

        async def complete(self, messages: list[Message], tools: list[ToolSpec],
                           emit: Emit) -> ModelReply:
            if self.child:
                child_started.set()
                await release.wait()
                assert all(m.content != "private parent task" for m in messages)
                assert "apply_patch" not in {t.name for t in tools}
                assert "spawn_agent" not in {t.name for t in tools}
            elif len(self.requests) == 1:
                await asyncio.wait_for(child_started.wait(), 5)
                release.set()  # Parent progresses while child has a pending model request.
            return await super().complete(messages, tools, emit)

        async def close(self) -> None:
            assert asyncio.current_task() is self.owner
            self.closed = True

    monkeypatch.setattr(bootstrap, "ChatModel", Model)
    cfg = replace(config(monkeypatch), memory_enabled=False,
                  multi_agent=MultiAgentConfig(enabled=True))
    conversation = bootstrap.Conversation(tmp_path, cfg, ignore, home=tmp_path / "home")
    try:
        result = await conversation.turn("private parent task")
        assert result.outcome == "completed" and result.final_text == "integrated"
        manager = conversation.coordinator
        assert manager is not None
        child = next(iter(manager.agents.values()))
        assert child.state == "completed", child.result
        assert child.conversation is not None and child.conversation.closed
        assert child.conversation.session is not conversation.session
        assert child.conversation.context is not conversation.context
        assert child.delivered
        assert conversation.writer is not None
        child_path = conversation.writer.path.with_suffix("") / "subagents" / f"{child.id}.jsonl"
        rows = [json.loads(line) for line in child_path.read_text(encoding="utf-8").splitlines()]
        assert any(r["kind"] == "run_finished" for r in rows)
        assert len(list_sessions(tmp_path, tmp_path / "home")) == 1
        assert len(clients) == 2 and clients[1].closed
    finally:
        await conversation.close()


async def test_capacity_wait_timeout_and_immediate_parent_finish(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    class Model(ScriptedModel):
        def __init__(self, _: Any) -> None:
            super().__init__([reply(text="unused")])

        async def complete(self, messages: list[Message], tools: list[ToolSpec],
                           emit: Emit) -> ModelReply:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        async def close(self) -> None:
            pass

    monkeypatch.setattr(bootstrap, "ChatModel", Model)
    cfg = replace(config(monkeypatch), memory_enabled=False,
                  multi_agent=MultiAgentConfig(enabled=True, max_agents=1))
    root = bootstrap.Conversation(tmp_path, cfg, ignore, home=tmp_path / "home")
    root.ensure_events("task")
    manager = root.coordinator
    assert manager is not None
    args = {"role": "worker", "task": "task", "scope": "a.py"}
    try:
        first = await manager.spawn("root", args)
        assert (await manager.spawn("root", args))["error"] == "capacity_full"
        await manager.wait("root", .01)
        child = manager.agents[first["agent_id"]]
        assert child.task is not None and not child.task.done()
        await manager.finish("root")
        assert child.task.done() and child.state == "aborted"
        second = await manager.spawn("root", args)
        await manager.finish("root")  # Cancel before child coroutine gets its first timeslice.
        assert manager.agents[second["agent_id"]].state == "aborted"
    finally:
        await root.close()


def test_config_defaults_and_role_model_overrides(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[model]\nname="test"\ncontext_window=32768\n')
    assert not load_config(path, require_key=False).multi_agent.enabled
    path.write_text(path.read_text() + '\n[multi_agent]\nenabled=true\nmax_agents=2\n'
                    'allowNestedSubagents=true\n[multi_agent.roles.scout]\n'
                    'model="default"\nreasoning_effort="medium"\nmcp_servers=["search"]\n')
    cfg = load_config(path, require_key=False).multi_agent
    assert cfg.max_agents == 2 and cfg.roles["scout"].mcp_servers == ("search",)
    path.write_text(path.read_text().replace('model="default"', 'model="missing"'))
    with pytest.raises(ConfigError, match="configured model alias"):
        load_config(path, require_key=False)
