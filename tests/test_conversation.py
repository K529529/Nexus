from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from nexus.app import bootstrap, cli
from nexus.app.config import Config, ModelConfig, default_shell
from nexus.core.context import SYSTEM
from nexus.core.types import Limits, ModelReply, RunResult, RuntimeEvent
from nexus.tools.mcp import connect_servers, public_name
from nexus.tools.registry import native_tools
from tests.conftest import ScriptedModel, call, python_command, reply


async def ignore(event: RuntimeEvent) -> None:
    pass


def config(monkeypatch: Any) -> Config:
    monkeypatch.setenv("TEST_CONVERSATION_KEY", "test-only")
    return Config(
        ModelConfig("test", 32768, api_key_env="TEST_CONVERSATION_KEY"),
        Limits(shell=default_shell()),
    )


async def test_reuses_resources_until_close_and_reopens_on_resume(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    cfg = config(monkeypatch)
    clients: list[Any] = []
    connections: list[str] = []

    class Model(ScriptedModel):
        def __init__(self, _: Any) -> None:
            super().__init__([reply(text="first"), reply(text="second")])
            self.closed = 0
            clients.append(self)

        async def close(self) -> None:
            self.closed += 1

    @asynccontextmanager
    async def connection() -> Any:
        owner = asyncio.current_task()
        connections.append("open")
        try:
            yield
        finally:
            assert asyncio.current_task() is owner
            connections.append("close")

    async def connect(configured: Any, registry: Any, stack: Any, events: Any) -> list[str]:
        await stack.enter_async_context(connection())
        return []

    monkeypatch.setattr(bootstrap, "ChatModel", Model)
    monkeypatch.setattr(bootstrap, "connect_servers", connect)
    convo = bootstrap.Conversation(tmp_path, cfg, ignore, home=tmp_path)
    assert not clients and not connections and convo.writer is None
    registry = convo.registry
    try:
        assert (await cli.drive(convo, "one")).outcome == "completed"
        assert (await cli.drive(convo, "two")).outcome == "completed"
        assert len(clients) == 1 and clients[0].closed == 0
        assert connections == ["open"] and convo.registry is registry
        assert not any(m.content in {"one", "first"} for m in clients[0].requests[-1])
        assert convo.writer is not None
        path = convo.writer.path
    finally:
        await convo.close()
    await convo.close()
    assert clients[0].closed == 1 and connections == ["open", "close"]
    with pytest.raises(RuntimeError, match="closed"):
        await convo.turn("after close")
    resumed = bootstrap.Conversation(tmp_path, cfg, ignore, home=tmp_path, resume=path)
    try:
        await cli.drive(resumed, "continue")
        assert len(clients) == 2 and connections.count("open") == 2
        assert not any(
            m.content in {"one", "two", "first", "second"} for m in clients[1].requests[0]
        )
    finally:
        await resumed.close()
    assert clients[1].closed == 1 and connections.count("close") == 2


async def test_resume_unfinished_conversation_and_refresh_current_environment(
    tmp_path: Path, monkeypatch: Any
) -> None:
    cfg = config(monkeypatch)
    root = tmp_path / "AGENTS.md"
    root.write_text("initial root rules", encoding="utf-8")
    clients: list[Any] = []

    class Model(ScriptedModel):
        def __init__(self, _: Any) -> None:
            super().__init__([reply(text="Recovered"), reply(text="New task answer")])
            clients.append(self)

        async def complete(self, messages: Any, tools: Any, emit: Any) -> ModelReply:
            if len(clients) == 1:
                raise asyncio.CancelledError
            return await super().complete(messages, tools, emit)

        async def close(self) -> None:
            pass

    async def connect(configured: Any, registry: Any, stack: Any, events: Any) -> list[str]:
        return ["offline-server"]

    monkeypatch.setattr(bootstrap, "ChatModel", Model)
    monkeypatch.setattr(bootstrap, "connect_servers", connect)
    convo = bootstrap.Conversation(tmp_path, cfg, ignore, home=tmp_path)
    try:
        assert (await convo.turn("Interrupted task")).outcome == "aborted"
        run_id = convo.session.run_id
        assert convo.writer is not None
        path = convo.writer.path
    finally:
        await convo.close()

    root.write_text("updated root rules", encoding="utf-8")
    resumed = bootstrap.Conversation(tmp_path, cfg, ignore, home=tmp_path, resume=path)
    try:
        assert (await resumed.turn("Continue")).outcome == "completed"
        assert resumed.session.run_id == run_id
        active = clients[1].requests[0]
        assert "updated root rules" in active[0].content
        assert "initial root rules" not in active[0].content
        assert any(m.content == "Interrupted task" for m in active)
        assert active[-2].content == "Continue"
        assert (await resumed.turn("Unrelated question")).outcome == "completed"
        active = clients[1].requests[1]
        assert len(active) == 4
        assert "offline-server" in active[1].content
        assert active[1].run_id == resumed.session.run_id != run_id
        assert active[2].content == "Unrelated question"
    finally:
        await resumed.close()


async def test_real_mcp_two_turns_discover_once_and_release(
    tmp_path: Path, monkeypatch: Any
) -> None:
    cfg = config(monkeypatch)
    cfg.mcp_servers = {
        "local": {
            "command": sys.executable,
            "args": [str(Path(__file__).parent / "fixtures/mcp_server.py")],
        }
    }
    name = public_name("local", "add")
    connections = 0

    class Model(ScriptedModel):
        closed = False

        async def close(self) -> None:
            self.closed = True

    model = Model(
        [
            reply(call(name, {"a": 1, "b": 2}, "first")),
            reply(text="3"),
            reply(call(name, {"a": 4, "b": 5}, "second")),
            reply(text="9"),
        ]
    )

    async def connect(*args: Any) -> list[str]:
        nonlocal connections
        connections += 1
        return await connect_servers(*args)

    monkeypatch.setattr(bootstrap, "ChatModel", lambda _: model)
    monkeypatch.setattr(bootstrap, "connect_servers", connect)
    convo = bootstrap.Conversation(tmp_path, cfg, ignore, home=tmp_path)
    try:
        assert (await cli.drive(convo, "sum")).final_text == "3"
        tool = convo.registry[name]
        assert (await cli.drive(convo, "sum again")).final_text == "9"
        assert connections == 1 and convo.registry[name] is tool
        assert not model.closed
    finally:
        await convo.close()
    assert model.closed


@pytest.mark.parametrize("failure", [TimeoutError, asyncio.CancelledError])
async def test_failed_mcp_stays_disabled_across_turns(
    tmp_path: Path,
    monkeypatch: Any,
    failure: type[BaseException],
) -> None:
    cfg = config(monkeypatch)
    cfg.mcp_servers = {"remote": {"command": "fixture"}}
    name = public_name("remote", "do_work")
    opened = discovered = closed = 0

    class Client:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> Client:
            nonlocal opened
            opened += 1
            return self

        async def __aexit__(self, *args: Any) -> None:
            nonlocal closed
            closed += 1

        async def list_tools(self, **kwargs: Any) -> Any:
            from types import SimpleNamespace

            nonlocal discovered
            discovered += 1
            return SimpleNamespace(
                result_type="complete",
                next_cursor=None,
                tools=[
                    SimpleNamespace(name="do_work", description="", input_schema={"type": "object"})
                ],
            )

        async def call_tool(self, *args: Any, **kwargs: Any) -> Any:
            raise failure

    class Model(ScriptedModel):
        async def close(self) -> None:
            pass

    responses = [reply(call(name, {}))]
    if failure is TimeoutError:
        responses.append(reply(text="server failed"))
    responses += [
        reply(call("exec_command", {"command": python_command("print('working')")}, "c2")),
        reply(text="native still works"),
    ]
    model = Model(responses)

    async def connect(*args: Any) -> list[str]:
        return await connect_servers(*args, client_factory=Client)

    monkeypatch.setattr(bootstrap, "connect_servers", connect)
    monkeypatch.setattr(bootstrap, "ChatModel", lambda _: model)
    convo = bootstrap.Conversation(tmp_path, cfg, ignore, home=tmp_path)
    try:
        await cli.drive(convo, "use remote")
        assert name not in convo.registry
        assert (await cli.drive(convo, "continue")).final_text == "native still works"
        assert opened == discovered == 1 and closed == 0
        assert name not in convo.registry
    finally:
        await convo.close()
    assert closed == 1
    monkeypatch.setattr(bootstrap, "ChatModel", lambda _: Model([reply(text="new")]))
    fresh = bootstrap.Conversation(tmp_path, cfg, ignore, home=tmp_path)
    try:
        await fresh.turn("new session")
        assert name in fresh.registry and opened == discovered == 2
    finally:
        await fresh.close()


async def test_cli_new_and_resume_close_old_lifetimes(tmp_path: Path, monkeypatch: Any) -> None:
    instances: list[Any] = []
    answers = iter(["/new", "/resume", "/exit"])

    class Conversation:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            from types import SimpleNamespace

            assert not instances or instances[-1].closed
            self.closed = False
            self.session = SimpleNamespace(messages=[])
            self.resume = kwargs.get("resume")
            instances.append(self)

        async def close(self) -> None:
            self.closed = True

    class Prompt:
        async def prompt_async(self, text: str) -> str:
            return next(answers)

    async def select(items: Any) -> Path:
        return tmp_path / "history.jsonl"

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(cli, "read_toml", lambda _: {"model": {"name": "test"}})
    monkeypatch.setattr(cli, "load_config", lambda: object())
    monkeypatch.setattr(cli, "PromptSession", Prompt)
    monkeypatch.setattr(cli, "Conversation", Conversation)
    monkeypatch.setattr(cli, "list_sessions", lambda _: [])
    monkeypatch.setattr(cli, "select_session", select)
    assert await cli.application(argparse.Namespace(command=None)) == 0
    assert len(instances) == 3 and all(c.closed for c in instances)
    assert instances[-1].resume == tmp_path / "history.jsonl"


async def test_real_mcp_sigint_then_another_turn_and_close(
    tmp_path: Path, monkeypatch: Any
) -> None:
    cfg = config(monkeypatch)
    cfg.mcp_servers = {
        "local": {
            "command": sys.executable,
            "args": [str(Path(__file__).parent / "fixtures/mcp_server.py")],
        }
    }
    name = public_name("local", "wait")

    class Model(ScriptedModel):
        async def complete(self, *args: Any, **kwargs: Any) -> ModelReply:
            response = await super().complete(*args, **kwargs)
            if response.message.tool_calls and response.message.tool_calls[0].name == name:
                handler = signal.getsignal(signal.SIGINT)
                assert callable(handler)
                asyncio.get_running_loop().call_later(0.2, handler, signal.SIGINT, None)
            return response

        async def close(self) -> None:
            pass

    model = Model(
        [
            reply(call(name, {"seconds": 30})),
            reply(
                call("exec_command", {"command": python_command("print('still works')")}, "native")
            ),
            reply(text="continued"),
        ]
    )
    monkeypatch.setattr(bootstrap, "ChatModel", lambda _: model)
    convo = bootstrap.Conversation(tmp_path, cfg, ignore, home=tmp_path)
    try:
        assert (await cli.drive(convo, "wait")).outcome == "aborted"
        assert public_name("local", "add") not in convo.registry
        assert (await cli.drive(convo, "continue")).final_text == "continued"
    finally:
        await convo.close()
    current = asyncio.current_task()
    assert current is not None and current.cancelling() == 0


def test_prompt_describes_two_native_tools() -> None:
    text = " ".join(SYSTEM.split())
    assert "Use exec_command to inspect and run checks" in text
    assert "and apply_patch to edit" in text
    assert set(native_tools()) == {"exec_command", "apply_patch"}


async def test_late_sigint_does_not_cancel_next_prompt() -> None:
    class QuickConversation:
        async def turn(self, text: str) -> RunResult:
            handler = signal.getsignal(signal.SIGINT)
            assert callable(handler)
            handler(signal.SIGINT, None)
            return RunResult("completed")

    conversation: Any = QuickConversation()
    assert (await cli.drive(conversation, "finish")).outcome == "completed"
    await asyncio.sleep(0)
    current = asyncio.current_task()
    assert current is not None and current.cancelling() == 0
