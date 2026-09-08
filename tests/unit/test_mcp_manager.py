from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, cast

import pytest
from mcp.shared.exceptions import MCPError as SDKMCPError
from mcp_types import REQUEST_TIMEOUT

from nexus.config.models import MCPServerConfig
from nexus.errors import MCPError
from nexus.infrastructure.mcp.manager import ClientFactory, SDKMCPManager, _sdk_client


@dataclass
class FakeTool:
    name: str
    description: str = "fixture tool"
    input_schema: dict[str, object] | None = None

    def __post_init__(self) -> None:
        if self.input_schema is None:
            self.input_schema = {"type": "object", "properties": {}}


@dataclass
class FakePage:
    tools: list[object]
    next_cursor: str | None = None


class FakeBlock:
    def __init__(self, text: str) -> None:
        self.text = text

    def model_dump(self, *, mode: str, by_alias: bool) -> dict[str, object]:
        assert mode == "json"
        assert by_alias is False
        return {"type": "text", "text": self.text}


class FakeResult:
    def __init__(
        self,
        *,
        is_error: bool = False,
        content: list[object] | None = None,
        structured_content: dict[str, object] | None = None,
    ) -> None:
        self.is_error = is_error
        self.content = content if content is not None else [FakeBlock("ok")]
        self.structured_content = structured_content


class FakeSession:
    def __init__(self, result: object | None = None, *, delay: float = 0) -> None:
        self.result = result if result is not None else FakeResult()
        self.delay = delay
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, object] | None = None,
        read_timeout_seconds: float | None = None,
        progress_callback: object | None = None,
        *,
        input_responses: object | None = None,
        request_state: str | None = None,
        meta: object | None = None,
        allow_input_required: bool = False,
        allow_claimed: bool = False,
    ) -> object:
        del (
            read_timeout_seconds,
            progress_callback,
            input_responses,
            request_state,
            meta,
            allow_claimed,
        )
        assert allow_input_required is True
        self.calls.append((name, dict(arguments or {})))
        if self.delay:
            await asyncio.sleep(self.delay)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class FakeClient:
    def __init__(
        self,
        server_id: str,
        *,
        enter_error: Exception | None = None,
        close_error: Exception | None = None,
        pages: list[FakePage] | None = None,
        session: FakeSession | None = None,
        close_order: list[str] | None = None,
    ) -> None:
        self.server_info = SimpleNamespace(name=server_id, version="1.0")
        self.protocol_version = "2026-07-28"
        self.session = session or FakeSession()
        self.enter_error = enter_error
        self.close_error = close_error
        self.pages = pages or [FakePage([FakeTool("echo")])]
        self.close_order = close_order
        self.enter_count = 0
        self.close_count = 0

    async def __aenter__(self) -> FakeClient:
        self.enter_count += 1
        if self.enter_error is not None:
            raise self.enter_error
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        del exc_type, exc_val, exc_tb
        self.close_count += 1
        if self.close_order is not None:
            self.close_order.append(self.server_info.name)
        if self.close_error is not None:
            raise self.close_error

    async def list_tools(
        self,
        *,
        cursor: str | None = None,
        meta: object | None = None,
        cache_mode: str = "use",
    ) -> object:
        del meta, cache_mode
        index = 0 if cursor is None else int(cursor)
        return self.pages[index]


def _config(
    server_id: str,
    *,
    attempts: int = 1,
    timeout: float = 1,
) -> MCPServerConfig:
    return MCPServerConfig(
        server_id=server_id,
        command="fixture",
        max_connect_attempts=attempts,
        connect_timeout_seconds=timeout,
        tool_timeout_seconds=timeout,
    )


def _factory(clients: list[FakeClient], order: list[str] | None = None) -> ClientFactory:
    def create(config: MCPServerConfig) -> FakeClient:
        if order is not None:
            order.append(config.server_id)
        return clients.pop(0)

    return cast(ClientFactory, create)


def test_sdk_client_environment_inheritance_is_explicit_and_not_in_config_repr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NEXUS_TEST_MCP_SECRET", "fixture-secret")
    inherited = cast(Any, _sdk_client(_config("inherited")))
    isolated = cast(
        Any,
        _sdk_client(
            MCPServerConfig(
                server_id="isolated",
                command="fixture",
                inherit_environment=False,
            )
        ),
    )

    assert inherited.server.env["NEXUS_TEST_MCP_SECRET"] == "fixture-secret"
    assert "NEXUS_TEST_MCP_SECRET" not in isolated.server.env
    assert "fixture-secret" not in repr(_config("inherited"))


@pytest.mark.asyncio
async def test_disabled_servers_create_no_clients() -> None:
    calls: list[str] = []

    def create(config: MCPServerConfig) -> FakeClient:
        calls.append(config.server_id)
        return FakeClient(config.server_id)

    manager = SDKMCPManager(
        (MCPServerConfig(server_id="off", enabled=False, command="fixture"),),
        client_factory=cast(ClientFactory, create),
    )

    assert await manager.connect() == ()
    assert calls == []
    await manager.close()


@pytest.mark.asyncio
async def test_connect_is_idempotent_and_preserves_configuration_order() -> None:
    creation_order: list[str] = []
    clients = [FakeClient("one"), FakeClient("two")]
    manager = SDKMCPManager(
        (_config("one"), _config("two")),
        client_factory=_factory(clients, creation_order),
    )

    first = await manager.connect()
    second = await manager.connect()

    assert first == second
    assert creation_order == ["one", "two"]
    assert tuple(item.server_id for item in first) == ("one", "two")
    await manager.close()


@pytest.mark.asyncio
async def test_retryable_connect_failure_uses_configured_total_attempts() -> None:
    failed = FakeClient("retry", enter_error=RuntimeError("private transport detail"))
    successful = FakeClient("retry")
    clients = [failed, successful]
    manager = SDKMCPManager(
        (_config("retry", attempts=2),), client_factory=_factory(clients)
    )

    info = await manager.connect()

    assert info[0].connected is True
    assert failed.close_count == 1
    assert successful.enter_count == 1
    await manager.close()


@pytest.mark.asyncio
async def test_exhausted_connect_closes_partial_connections() -> None:
    first = FakeClient("first")
    second = FakeClient("second", enter_error=RuntimeError("boom"))
    manager = SDKMCPManager(
        (_config("first"), _config("second")),
        client_factory=_factory([first, second]),
    )

    with pytest.raises(MCPError) as raised:
        await manager.connect()

    assert raised.value.code == "MCP_CONNECT_FAILED"
    assert "boom" not in str(raised.value)
    assert first.close_count == 1
    assert second.close_count == 1


@pytest.mark.asyncio
async def test_close_is_reverse_idempotent_and_attempts_all_resources() -> None:
    close_order: list[str] = []
    first = FakeClient("first", close_order=close_order)
    second = FakeClient(
        "second", close_error=RuntimeError("close failed"), close_order=close_order
    )
    manager = SDKMCPManager(
        (_config("first"), _config("second")),
        client_factory=_factory([first, second]),
    )
    await manager.connect()

    await manager.close()
    await manager.close()

    assert close_order == ["second", "first"]
    assert first.close_count == second.close_count == 1


@pytest.mark.asyncio
async def test_discovery_is_paginated_ordered_and_rejects_duplicates() -> None:
    pages = [
        FakePage([FakeTool("first")], next_cursor="1"),
        FakePage([FakeTool("second")]),
    ]
    manager = SDKMCPManager(
        (_config("server"),),
        client_factory=_factory([FakeClient("server", pages=pages)]),
    )
    await manager.connect()

    descriptors = await manager.list_tools()

    assert tuple(item.registry_name for item in descriptors) == (
        "mcp.server.first",
        "mcp.server.second",
    )
    await manager.close()

    duplicate_manager = SDKMCPManager(
        (_config("server"),),
        client_factory=_factory(
            [FakeClient("server", pages=[FakePage([FakeTool("same"), FakeTool("same")])])]
        ),
    )
    await duplicate_manager.connect()
    with pytest.raises(MCPError) as raised:
        await duplicate_manager.list_tools()
    assert raised.value.code == "MCP_DISCOVERY_FAILED"
    await duplicate_manager.close()


@pytest.mark.asyncio
async def test_discovery_rejects_invalid_schema_and_duplicate_registry_names() -> None:
    invalid_manager = SDKMCPManager(
        (_config("server"),),
        client_factory=_factory(
            [
                FakeClient(
                    "server",
                    pages=[
                        FakePage(
                            [SimpleNamespace(name="bad", description="bad", input_schema=[])]
                        )
                    ],
                )
            ]
        ),
    )
    await invalid_manager.connect()
    with pytest.raises(MCPError) as invalid:
        await invalid_manager.list_tools()
    assert invalid.value.code == "MCP_INVALID_SCHEMA"
    await invalid_manager.close()

    duplicate_registry_manager = SDKMCPManager(
        (_config("same"), _config("same")),
        client_factory=_factory([FakeClient("same"), FakeClient("same")]),
    )
    await duplicate_registry_manager.connect()
    with pytest.raises(MCPError) as duplicate:
        await duplicate_registry_manager.list_tools()
    assert duplicate.value.code == "MCP_DISCOVERY_FAILED"
    await duplicate_registry_manager.close()


@pytest.mark.asyncio
async def test_tool_call_normalizes_result_and_never_replays_timeout() -> None:
    session = FakeSession(
        FakeResult(structured_content={"echo": "hello"}),
        delay=0.05,
    )
    manager = SDKMCPManager(
        (_config("server", timeout=0.01),),
        client_factory=_factory([FakeClient("server", session=session)]),
    )
    await manager.connect()
    await manager.list_tools()

    with pytest.raises(MCPError) as raised:
        await manager.call_tool(
            server_id="server",
            remote_name="echo",
            arguments={"message": "hello"},
            timeout_seconds=0.01,
        )

    assert raised.value.code == "MCP_TOOL_TIMEOUT"
    assert raised.value.retryable is True
    assert len(session.calls) == 1
    await manager.close()


@pytest.mark.asyncio
async def test_tool_call_success_and_cancellation_boundary() -> None:
    session = FakeSession(FakeResult(structured_content={"echo": "hello"}))
    manager = SDKMCPManager(
        (_config("server"),),
        client_factory=_factory([FakeClient("server", session=session)]),
    )
    await manager.connect()
    await manager.list_tools()

    result = await manager.call_tool(
        server_id="server",
        remote_name="echo",
        arguments={"message": "hello"},
        timeout_seconds=1,
    )

    assert result == {
        "content": [{"type": "text", "text": "ok"}],
        "structured_content": {"echo": "hello"},
        "is_error": False,
    }
    await manager.close()

    slow_session = FakeSession(delay=10)
    cancel_manager = SDKMCPManager(
        (_config("server"),),
        client_factory=_factory([FakeClient("server", session=slow_session)]),
    )
    await cancel_manager.connect()
    await cancel_manager.list_tools()
    task = asyncio.create_task(
        cancel_manager.call_tool(
            server_id="server",
            remote_name="echo",
            arguments={},
            timeout_seconds=1,
        )
    )
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await cancel_manager.close()


@pytest.mark.asyncio
async def test_sdk_request_timeout_maps_to_stable_tool_timeout() -> None:
    session = FakeSession(SDKMCPError(REQUEST_TIMEOUT, "private SDK timeout"))
    manager = SDKMCPManager(
        (_config("server"),),
        client_factory=_factory([FakeClient("server", session=session)]),
    )
    await manager.connect()
    await manager.list_tools()

    with pytest.raises(MCPError) as raised:
        await manager.call_tool(
            server_id="server",
            remote_name="echo",
            arguments={},
            timeout_seconds=1,
        )

    assert raised.value.code == "MCP_TOOL_TIMEOUT"
    assert raised.value.retryable is True
    assert "private SDK timeout" not in str(raised.value)
    assert len(session.calls) == 1
    await manager.close()
