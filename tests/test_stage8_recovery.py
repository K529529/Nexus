from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from nexus.app import bootstrap
from nexus.app.config import MultiAgentConfig
from nexus.core.inbox import Inbox
from tests.conftest import ScriptedModel, reply
from tests.test_conversation import config, ignore

CRASH = r'''
import asyncio, json, os, sys
from pathlib import Path
from dataclasses import replace
from nexus.app import bootstrap
from nexus.app.config import Config, ModelConfig, MultiAgentConfig, default_shell
from nexus.app.events import Events
from nexus.core.types import Limits
from tests.conftest import ScriptedModel, call, python_command, reply

workspace, window = Path(sys.argv[1]), sys.argv[2]
os.environ['STAGE8_TEST_KEY'] = 'test'
cfg = Config(ModelConfig('test',32768,api_key_env='STAGE8_TEST_KEY'),
             Limits(shell=default_shell()), memory_enabled=False,
             multi_agent=MultiAgentConfig(enabled=True))
original = Events.__call__
async def event(self,kind,data,**kwargs):
    is_child = self.session.session_id != root.session.session_id
    if window == 'effect' and is_child and kind == 'tool_checkpoint' and 'result' in data:
        os._exit(77)
    seq = await original(self,kind,data,**kwargs)
    if window == 'created' and kind == 'agent_created': os._exit(77)
    if window == 'completed' and is_child and kind == 'run_finished': os._exit(77)
    if window == 'guidance' and is_child and kind == 'run_finished':
        accepted = await root.coordinator.message('root', self.session.session_id, 'late guidance')
        assert accepted['received']
        os._exit(77)
    if window == 'delivered' and not is_child and kind == 'message':
        delivered = str(data.get('input_id','')).startswith('agent-result:')
        if data.get('role') == 'tool':
            result = json.loads(data['content'])
            delivered |= any(a.get('result') is not None
                             for a in result.get('data',{}).get('agents',[]))
        if delivered: os._exit(77)
    return seq
Events.__call__ = event
class Model(ScriptedModel):
    def __init__(self, settings):
        self.child = settings.reasoning_effort == 'high'
        self.count = 0
        code = ("import os; f=open('counter','ab'); f.write(b'x'); "
                "f.flush(); os.fsync(f.fileno()); f.close()")
        super().__init__([reply(call('exec_command',{'command':python_command(code)})),
                          reply(text='child completed')])
    async def complete(self,messages,tools,emit):
        if self.child: return await super().complete(messages,tools,emit)
        self.count += 1
        if self.count == 1:
            value = reply(call('spawn_agent',{
                'role':'worker','task':'write once','scope':'counter'}))
        else:
            value = reply(call('wait_agents',{'timeout_seconds':5},f'w{self.count}'))
        await emit('model_started',{'attempt':1})
        from dataclasses import asdict
        await emit('model_finished',{'usage':asdict(value.usage)})
        return value
    async def close(self): pass
bootstrap.ChatModel = Model
async def ignore(event): pass
root = bootstrap.Conversation(workspace,cfg,ignore,home=workspace/'home')
asyncio.run(root.turn('original root task'))
raise RuntimeError('Crash boundary not reached')
'''


def crash(workspace: Path, window: str) -> Path:
    repository = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, "-c", CRASH, str(workspace), window],
        capture_output=True, timeout=30, check=False,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(
            [str(repository / "src"), str(repository)])})
    assert result.returncode == 77, result.stderr.decode(errors="replace")
    return next((workspace / "home" / "sessions").glob("*/*.jsonl"))


@pytest.mark.parametrize("window", ["created", "effect", "completed", "delivered"])
async def test_abrupt_crash_restores_tree_without_replay_or_duplicate_result(
    tmp_path: Path, monkeypatch: Any, window: str,
) -> None:
    path = await asyncio.to_thread(crash, tmp_path, window)
    monkeypatch.setenv("STAGE8_TEST_KEY", "test")
    cfg = replace(config(monkeypatch), memory_enabled=False,
                  multi_agent=MultiAgentConfig(enabled=True))
    clients: list[Any] = []

    class Model(ScriptedModel):
        def __init__(self, _: Any) -> None:
            super().__init__([reply(text="resumed root"), reply(text="resumed root")])
            clients.append(self)

        async def close(self) -> None:
            pass

    monkeypatch.setattr(bootstrap, "ChatModel", Model)
    root = bootstrap.Conversation(tmp_path, cfg, ignore, home=tmp_path / "home", resume=path)
    manager = root.coordinator
    assert manager is not None
    try:
        assert len(manager.agents) == 1 and not clients
        agent = next(iter(manager.agents.values()))
        assert agent.task is None
        if window in {"created", "effect"}:
            assert agent.state == "interrupted"
        else:
            assert agent.state == "completed"
        if window == "effect":
            assert root.session.unresolved_effects
            assert (await manager.spawn("root", {
                "role": "worker", "task": "retry", "scope": "counter",
            }))["error"] == "unresolved_workspace_effects"
        result = await root.turn("continue using saved state")
        assert result.outcome == "completed", result
        assert len(manager.agents) == 1 and len(clients) == 1
        counter = tmp_path / "counter"
        assert counter.read_bytes() == b"x" if counter.exists() else window == "created"
        injected = [m for m in root.session.messages if m.input_id and
                    m.input_id.startswith("agent-result:")]
        assert len(injected) == (1 if window == "completed" else 0)
    finally:
        await root.close()


async def test_explicit_resume_keeps_child_history_and_new_delivery_identity(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    path = await asyncio.to_thread(crash, tmp_path, "effect")
    monkeypatch.setenv("STAGE8_TEST_KEY", "test")
    cfg = replace(config(monkeypatch), memory_enabled=False,
                  multi_agent=MultiAgentConfig(enabled=True))
    clients: list[Any] = []

    class Model(ScriptedModel):
        def __init__(self, _: Any) -> None:
            super().__init__([reply(text="checked existing counter; no retry")])
            clients.append(self)

        async def close(self) -> None:
            pass

    monkeypatch.setattr(bootstrap, "ChatModel", Model)
    root = bootstrap.Conversation(tmp_path, cfg, ignore, home=tmp_path / "home", resume=path)
    root.ensure_events("resume")
    manager = root.coordinator
    assert manager is not None
    try:
        agent = next(iter(manager.agents.values()))
        prior = agent.execution_id
        root.session.run_id = "resume-task"
        assert (await manager.resume("root", agent.id, "inspect only; do not replay"))["agent_id"]
        assert agent.task_run_id == "resume-task"
        assert agent.task is not None
        await agent.task
        assert agent.state == "completed" and agent.execution_id != prior
        assert (tmp_path / "counter").read_bytes() == b"x"
        assert any(m.role == "tool" and "interrupted_unknown" in m.content
                   for m in clients[0].requests[0])
        assert any("write once" in m.content for m in clients[0].requests[0])
        assert root.session.unresolved_effects  # Textual success does not clear unknown effects.
        await manager.deliver_results("root")
        root.session.run_id = "next-task"
        assert agent.id in manager.current_agents()
        assert "unresolved" in manager.panel()
    finally:
        await root.close()


def test_inbox_recovery_uses_committed_messages_not_early_receipts() -> None:
    inbox = Inbox()
    inbox.restore([
        {"kind": "input_received", "data": {"id": "a", "mode": "guide", "text": "old"}},
        {"kind": "input_updated", "data": {"id": "a", "text": "new"}},
        {"kind": "input_delivered", "data": {"id": "a"}},
        {"kind": "input_received", "data": {"id": "b", "mode": "queue", "text": "next"}},
        {"kind": "message", "data": {"role": "user", "input_id": "b", "content": "next"}},
    ])
    assert [(m.id, m.text) for m in inbox.pending] == [("a", "new")]


async def test_crash_with_late_guidance_recovers_interrupted_not_stale_completed(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    path = await asyncio.to_thread(crash, tmp_path, "guidance")
    monkeypatch.setenv("STAGE8_TEST_KEY", "test")
    cfg = replace(config(monkeypatch), memory_enabled=False,
                  multi_agent=MultiAgentConfig(enabled=True))
    clients: list[ScriptedModel] = []

    class Model(ScriptedModel):
        def __init__(self, _: Any) -> None:
            super().__init__([reply(text="handled saved guidance")])
            clients.append(self)

        async def close(self) -> None:
            pass

    monkeypatch.setattr(bootstrap, "ChatModel", Model)
    root = bootstrap.Conversation(tmp_path, cfg, ignore, home=tmp_path / "home", resume=path)
    root.ensure_events("resume")
    root.ensure_events("resume")
    manager = root.coordinator
    assert manager is not None
    try:
        agent = next(iter(manager.agents.values()))
        assert agent.state == "interrupted" and agent.result is None and agent.task is None
        assert not await manager.deliver_results("root")
        assert not clients
        assert (await manager.resume("root", agent.id, "continue"))["agent_id"] == agent.id
        assert agent.task is not None
        await agent.task
        assert agent.state == "completed" and agent.result is not None
        assert agent.result.final_text == "handled saved guidance"
        assert any("late guidance" in m.content for m in clients[0].requests[0])
        assert (tmp_path / "counter").read_bytes() == b"x"
        assert await manager.deliver_results("root")
        assert not await manager.deliver_results("root")
    finally:
        await root.close()
