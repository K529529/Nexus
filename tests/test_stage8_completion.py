from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest
from prompt_toolkit import PromptSession
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from nexus.app import bootstrap, cli
from nexus.app.agent_history import read_tree, tree_facts
from nexus.app.config import MultiAgentConfig
from nexus.app.input import TaskInput
from nexus.app.tracing import project_tree
from nexus.core.context import Context
from nexus.core.types import Emit, Message, ModelReply, RunResult, RuntimeEvent, Session, ToolSpec
from tests.conftest import ScriptedModel, call, python_command, reply
from tests.test_cli_ux import until
from tests.test_conversation import config
from tests.test_stage8_collaboration import root_conversation


@dataclass
class Finishing:
    root: bootstrap.Conversation
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    maintenance_left: asyncio.Event = field(default_factory=asyncio.Event)
    root_ready: asyncio.Event = field(default_factory=asyncio.Event)
    root_release: asyncio.Event = field(default_factory=asyncio.Event)
    receipt: asyncio.Event = field(default_factory=asyncio.Event)
    receipt_release: asyncio.Event = field(default_factory=asyncio.Event)
    closing: asyncio.Event = field(default_factory=asyncio.Event)
    close_release: asyncio.Event = field(default_factory=asyncio.Event)
    block_receipt: bool = False
    block_close: bool = False
    models: list[ScriptedModel] = field(default_factory=list)


@pytest.fixture
async def finishing(tmp_path: Path, monkeypatch: Any) -> AsyncIterator[Finishing]:
    async def consume(event: RuntimeEvent) -> None:
        if (state.block_receipt and event.kind == "subagent_event"
                and event.data["kind"] == "input_received"):
            state.receipt.set()
            await state.receipt_release.wait()

    class Model(ScriptedModel):
        def __init__(self, settings: Any) -> None:
            self.child = settings.reasoning_effort == "high"
            super().__init__([
                reply(call("exec_command", {"command": python_command(
                    "from pathlib import Path; p=Path('counter'); "
                    "p.write_text((p.read_text() if p.exists() else '')+'x')"
                )}, "once")), reply(text="original result"), reply(text="updated result"),
            ] if self.child else [reply(text="parent done") for _ in range(4)])
            if self.child:
                state.models.append(self)

        async def complete(self, messages: list[Message], tools: list[ToolSpec],
                           emit: Emit) -> ModelReply:
            if not self.child and not self.requests:
                state.root_ready.set()
                await state.root_release.wait()
            return await super().complete(messages, tools, emit)

        async def close(self) -> None:
            pass

    monkeypatch.setattr(bootstrap, "ChatModel", Model)
    cfg = replace(config(monkeypatch), memory_enabled=False,
                  multi_agent=MultiAgentConfig(enabled=True, max_agents=1))
    root = bootstrap.Conversation(tmp_path, cfg, consume, home=tmp_path / "home")
    root.ensure_events("parent task")
    state = Finishing(root)
    prepare, close = Context.prepare, bootstrap.Conversation.close
    seen: set[str] = set()

    async def maintenance(self: Context, session: Session, *args: Any, **kwargs: Any) -> bool:
        blocked = (kwargs.get("reason") == "between_runs"
                   and session.session_id != root.session.session_id
                   and session.session_id not in seen)
        if blocked:
            seen.add(session.session_id)
            state.ready.set()
            # Inside real Conversation.compact, including its cancellation handling.
            await state.release.wait()
        result = await prepare(self, session, *args, **kwargs)
        if blocked:
            state.maintenance_left.set()
        return result

    async def closing(self: bootstrap.Conversation) -> None:
        if self is not root and state.block_close:
            state.closing.set()
            await state.close_release.wait()
        await close(self)

    monkeypatch.setattr(Context, "prepare", maintenance)
    monkeypatch.setattr(bootstrap.Conversation, "close", closing)
    try:
        yield state
    finally:
        state.release.set()
        state.receipt_release.set()
        state.close_release.set()
        state.root_release.set()
        await root.close()


async def test_parent_guidance_committing_during_compaction_is_drained_once(
    finishing: Finishing, tmp_path: Path,
) -> None:
    f, root = finishing, finishing.root
    manager = root.coordinator
    assert manager is not None and root.events is not None and root.writer is not None
    root.session.run_id, root.session.execution_id = "root-run", "root-exec"
    await root.events("run_started", {"execution_id": "root-exec"})
    ident = (await manager.spawn("root", {
        "role": "worker", "task": "original", "scope": "counter",
    }))["agent_id"]
    await asyncio.wait_for(f.ready.wait(), 5)
    agent, child = manager.agents[ident], manager.owner(ident)
    first_execution = agent.execution_id
    f.block_receipt = True
    sending = asyncio.create_task(manager.message("root", ident, "accepted late guidance"))
    await asyncio.wait_for(f.receipt.wait(), 3)
    f.release.set()
    # The receipt transaction still owns the completion boundary.
    await asyncio.wait_for(f.maintenance_left.wait(), 3)
    assert manager.occupied() == 1 and not child.closed and agent.result is None
    f.receipt_release.set()
    assert (await sending)["received"]
    assert agent.task is not None
    await asyncio.wait_for(agent.task, 5)
    assert agent.state == "completed" and child.closed and not child.inbox.guides()
    assert agent.result is not None and agent.result.final_text == "updated result"
    assert agent.execution_id != first_execution
    model = f.models[0]
    assert len(model.requests) == 3 and (tmp_path / "counter").read_text() == "x"
    request = model.requests[-1]
    assert sum("accepted late guidance" in m.content for m in request) == 1
    assert sum(m.role == "user" and m.content.startswith("original\n") for m in request) == 1
    assert [m.tool_call_id for m in request if m.role == "tool"] == ["once"]
    assert await manager.deliver_results("root")
    assert not await manager.deliver_results("root")
    results = [m for m in root.session.messages
               if m.input_id and m.input_id.startswith("agent-result:")]
    assert len(results) == 1 and "updated result" in results[0].content
    await root.events("run_finished", {"outcome": "completed"})
    logs, _, _ = read_tree(root.writer.path)
    starts = [r for r in logs[ident] if r["kind"] == "run_started"]
    assert len(starts) == 2 and len({r["run_id"] for r in starts}) == 1
    assert len({r["data"]["execution_id"] for r in starts}) == 2
    spans, _ = project_tree(logs, "root-run")
    lifetimes = [s for s in spans if s["name"] == "nexus.agent"]
    assert len(lifetimes) == 2 and len({s["span_id"] for s in spans}) == len(spans)
    assert len({s["trace_id"] for s in spans}) == 1


async def test_selected_child_enter_during_compaction_reaches_updated_parent_result(
    finishing: Finishing,
) -> None:
    f, root = finishing, finishing.root
    manager = root.coordinator
    assert manager is not None
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        prompt: PromptSession[str] = PromptSession(output=DummyOutput())
        editor = TaskInput(prompt)
        running = asyncio.create_task(cli.drive(root, "parent task", editor=editor))
        try:
            await asyncio.wait_for(f.root_ready.wait(), 3)
            ident = (await manager.spawn("root", {
                "role": "worker", "task": "original", "scope": "counter",
            }))["agent_id"]
            await asyncio.wait_for(f.ready.wait(), 5)
            assert await cli.agent_command(root, editor, f"/agent {ident}")
            await until(lambda: prompt.app.is_running and not prompt.app.is_done)
            pipe.send_text("user late guidance")
            await until(lambda: prompt.default_buffer.text == "user late guidance")
            pipe.send_text("\r")
            await until(lambda: bool(manager.owner(ident).inbox.guides()))
            f.release.set()
            task = manager.agents[ident].task
            assert task is not None
            await asyncio.wait_for(task, 5)
            f.root_release.set()
            assert (await asyncio.wait_for(running, 5)).outcome == "completed"
            assert any("user late guidance" in m.content for m in f.models[0].requests[-1])
            assert any("updated result" in m.content and m.input_id
                       and m.input_id.startswith("agent-result:") for m in root.session.messages)
        finally:
            f.root_release.set()
            if not running.done():
                running.cancel()
                await running


async def test_completed_acceptance_boundary_rejects_before_close(finishing: Finishing) -> None:
    f, root = finishing, finishing.root
    manager = root.coordinator
    assert manager is not None
    f.block_close = True
    ident = (await manager.spawn("root", {
        "role": "worker", "task": "original", "scope": "counter",
    }))["agent_id"]
    await asyncio.wait_for(f.ready.wait(), 5)
    f.release.set()
    await asyncio.wait_for(f.closing.wait(), 5)
    assert not manager.owner(ident).closed and manager.occupied() == 1
    assert (await manager.message("root", ident, "too late"))["error"] == "agent_not_accepting"
    assert (await manager.user_message(ident, "also too late"))["error"] == "agent_not_accepting"
    f.close_release.set()
    task = manager.agents[ident].task
    assert task is not None
    await asyncio.wait_for(task, 3)
    assert not manager.owner(ident).inbox.guides() and manager.occupied() == 0


@pytest.mark.parametrize("in_flight", [False, True])
async def test_cancelled_maintenance_does_not_revive_for_accepted_guidance(
    finishing: Finishing, tmp_path: Path, in_flight: bool,
) -> None:
    f, root = finishing, finishing.root
    manager = root.coordinator
    assert manager is not None and root.writer is not None
    ident = (await manager.spawn("root", {
        "role": "worker", "task": "original", "scope": "counter",
    }))["agent_id"]
    await asyncio.wait_for(f.ready.wait(), 5)
    if in_flight:
        f.block_receipt = True
        sending = asyncio.create_task(manager.message("root", ident, "accepted before cancel"))
        await asyncio.wait_for(f.receipt.wait(), 3)
        cancelling = asyncio.create_task(manager.cancel("root", ident))
        await until(lambda: manager.agents[ident].state == "cancelling")
        f.receipt_release.set()
        assert (await asyncio.wait_for(sending, 3))["received"]
        cancelled = await asyncio.wait_for(cancelling, 5)
    else:
        assert (await manager.message("root", ident, "accepted before cancel"))["received"]
        cancelled = await asyncio.wait_for(manager.cancel("root", ident), 5)
    assert cancelled["state"] == "aborted"
    assert len(f.models[0].requests) == 2 and (tmp_path / "counter").read_text() == "x"
    assert (await manager.user_message(ident, "after cancel"))["error"] == "agent_not_accepting"
    assert manager.owner(ident).inbox.guides()  # Retained for explicit resume, never auto-started.
    facts = tree_facts(root.writer.path)
    assert next(a for a in facts["agents"] if a["agent_id"] == ident)["state"] == "aborted"
    path, cfg = root.writer.path, root.config
    await root.close()
    restored = bootstrap.Conversation(tmp_path, cfg, root.consumer,
                                      home=tmp_path / "home", resume=path)
    try:
        assert restored.coordinator is not None
        saved = restored.coordinator.agents[ident]
        assert saved.state == "aborted" and saved.task is None
    finally:
        await restored.close()


async def test_cancel_keeps_cleanup_failure_and_capacity(tmp_path: Path, monkeypatch: Any) -> None:
    root = root_conversation(tmp_path, monkeypatch)
    manager = root.coordinator
    assert manager is not None

    async def failed_turn(self: bootstrap.Conversation, text: str) -> RunResult:
        manager.agents[self.agent_id].state = "cancelling"
        return RunResult("failed", reason="cleanup_incomplete")

    monkeypatch.setattr(bootstrap.Conversation, "turn", failed_turn)
    ident = (await manager.spawn("root", {
        "role": "worker", "task": "original", "scope": "counter",
    }))["agent_id"]
    task = manager.agents[ident].task
    assert task is not None
    await task
    assert manager.agents[ident].state == "cleanup_failed" and manager.occupied() == 1
    with pytest.raises(RuntimeError, match="cleanup_incomplete"):
        await root.close()
