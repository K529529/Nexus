"""Sequential stdio MCP lifecycle implemented behind Nexus-owned values."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, cast

from mcp import Client, StdioServerParameters
from mcp.shared.exceptions import MCPError as SDKMCPError
from mcp_types import REQUEST_TIMEOUT

from nexus.config.models import MCPServerConfig
from nexus.domain.mcp import MCPConnectionInfo, MCPToolDescriptor
from nexus.domain.tooling import JsonObject
from nexus.errors import MCPError

_MAX_DESCRIPTION_CHARS = 2_000
_MAX_SCHEMA_BYTES = 64_000
_MAX_TEXT_CHARS = 16_000
_MAX_BINARY_CHARS = 4_096


class _SessionLike(Protocol):
    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        read_timeout_seconds: float | None = None,
        progress_callback: object | None = None,
        *,
        input_responses: object | None = None,
        request_state: str | None = None,
        meta: object | None = None,
        allow_input_required: bool = False,
        allow_claimed: bool = False,
    ) -> object: ...


class _ClientLike(Protocol):
    server_info: object | None
    protocol_version: str | None
    session: _SessionLike

    async def __aenter__(self) -> _ClientLike: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None: ...

    async def list_tools(
        self,
        *,
        cursor: str | None = None,
        meta: object | None = None,
        cache_mode: str = "use",
    ) -> object: ...


ClientFactory = Callable[[MCPServerConfig], _ClientLike]


@dataclass(slots=True)
class _Connection:
    config: MCPServerConfig
    client: _ClientLike
    remote_names: frozenset[str] = frozenset()


class SDKMCPManager:
    def __init__(
        self,
        servers: tuple[MCPServerConfig, ...],
        *,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self._servers = tuple(server for server in servers if server.enabled)
        self._client_factory = client_factory or _sdk_client
        self._connections: list[_Connection] = []
        self._descriptors: tuple[MCPToolDescriptor, ...] | None = None
        self._connection_info: tuple[MCPConnectionInfo, ...] | None = None

    async def connect(self) -> tuple[MCPConnectionInfo, ...]:
        if self._connection_info is not None:
            return self._connection_info
        try:
            for config in self._servers:
                self._connections.append(await self._connect_one(config))
        except asyncio.CancelledError:
            await self.close()
            raise
        except Exception:
            await self.close()
            raise
        self._connection_info = tuple(
            MCPConnectionInfo(
                connection.config.server_id,
                True,
                _optional_text(connection.client.server_info, "name"),
                _optional_text(connection.client.server_info, "version"),
                connection.client.protocol_version,
            )
            for connection in self._connections
        )
        return self._connection_info

    async def _connect_one(self, config: MCPServerConfig) -> _Connection:
        for attempt in range(1, config.max_connect_attempts + 1):
            client: _ClientLike | None = None
            try:
                client = self._client_factory(config)
                async with asyncio.timeout(config.connect_timeout_seconds):
                    await client.__aenter__()
                return _Connection(config, client)
            except asyncio.CancelledError:
                if client is not None:
                    await _close_client(client)
                raise
            except Exception as exc:
                if client is not None:
                    await _close_client(client)
                retryable = _is_retryable_connect_failure(exc)
                if not retryable or attempt == config.max_connect_attempts:
                    raise MCPError(
                        f"MCP server {config.server_id!r} failed during connect.",
                        code="MCP_CONNECT_FAILED",
                        retryable=False,
                    ) from exc
                await asyncio.sleep(0)
        raise AssertionError("MCP connect attempt loop did not terminate.")

    async def list_tools(self) -> tuple[MCPToolDescriptor, ...]:
        if self._connection_info is None:
            raise MCPError(
                "MCP discovery requires a successful connection.",
                code="MCP_DISCOVERY_FAILED",
            )
        if self._descriptors is not None:
            return self._descriptors
        descriptors: list[MCPToolDescriptor] = []
        try:
            for connection in self._connections:
                names: set[str] = set()
                cursor: str | None = None
                while True:
                    async with asyncio.timeout(
                        connection.config.connect_timeout_seconds
                    ):
                        page = await connection.client.list_tools(cursor=cursor)
                    tools = getattr(page, "tools", None)
                    if not isinstance(tools, list):
                        raise MCPError(
                            f"MCP server {connection.config.server_id!r} returned "
                            "an invalid discovery result.",
                            code="MCP_DISCOVERY_FAILED",
                        )
                    for tool in tools:
                        descriptor = _descriptor(connection.config.server_id, tool)
                        if descriptor.remote_name in names:
                            raise MCPError(
                                f"MCP server {connection.config.server_id!r} advertised "
                                "a duplicate Tool name.",
                                code="MCP_DISCOVERY_FAILED",
                            )
                        names.add(descriptor.remote_name)
                        descriptors.append(descriptor)
                    next_cursor = getattr(page, "next_cursor", None)
                    if next_cursor is None:
                        break
                    if not isinstance(next_cursor, str) or not next_cursor:
                        raise MCPError(
                            f"MCP server {connection.config.server_id!r} returned "
                            "an invalid discovery cursor.",
                            code="MCP_DISCOVERY_FAILED",
                        )
                    cursor = next_cursor
                connection.remote_names = frozenset(names)
        except asyncio.CancelledError:
            raise
        except MCPError:
            raise
        except Exception as exc:
            raise MCPError(
                "MCP Tool discovery failed.",
                code="MCP_DISCOVERY_FAILED",
                retryable=False,
            ) from exc
        registry_names = [item.registry_name for item in descriptors]
        if len(registry_names) != len(set(registry_names)):
            raise MCPError(
                "MCP Tool discovery produced duplicate registry names.",
                code="MCP_DISCOVERY_FAILED",
            )
        self._descriptors = tuple(descriptors)
        return self._descriptors

    async def call_tool(
        self,
        *,
        server_id: str,
        remote_name: str,
        arguments: JsonObject,
        timeout_seconds: float,
    ) -> JsonObject:
        connection = next(
            (item for item in self._connections if item.config.server_id == server_id),
            None,
        )
        if connection is None or remote_name not in connection.remote_names:
            raise MCPError(
                f"MCP Tool {server_id!r}/{remote_name!r} is not connected and discovered.",
                code="MCP_CALL_FAILED",
            )
        timeout = min(timeout_seconds, connection.config.tool_timeout_seconds)
        try:
            async with asyncio.timeout(timeout):
                result = await connection.client.session.call_tool(
                    remote_name,
                    dict(arguments),
                    read_timeout_seconds=timeout,
                    allow_input_required=True,
                )
            return _normalize_result(result)
        except asyncio.CancelledError:
            raise
        except TimeoutError as exc:
            raise MCPError(
                f"MCP Tool {server_id!r}/{remote_name!r} timed out.",
                code="MCP_TOOL_TIMEOUT",
                retryable=True,
            ) from exc
        except SDKMCPError as exc:
            if exc.code == REQUEST_TIMEOUT:
                raise MCPError(
                    f"MCP Tool {server_id!r}/{remote_name!r} timed out.",
                    code="MCP_TOOL_TIMEOUT",
                    retryable=True,
                ) from exc
            raise MCPError(
                f"MCP Tool {server_id!r}/{remote_name!r} failed during call.",
                code="MCP_CALL_FAILED",
                retryable=False,
            ) from exc
        except MCPError:
            raise
        except Exception as exc:
            raise MCPError(
                f"MCP Tool {server_id!r}/{remote_name!r} failed during call.",
                code="MCP_CALL_FAILED",
                retryable=False,
            ) from exc

    async def close(self) -> None:
        connections = tuple(reversed(self._connections))
        self._connections.clear()
        self._connection_info = None
        self._descriptors = None
        for connection in connections:
            await _close_client(connection.client)


def _sdk_client(config: MCPServerConfig) -> _ClientLike:
    parameters = StdioServerParameters(
        command=config.command,
        args=list(config.args),
        env=dict(os.environ) if config.inherit_environment else {},
    )
    return cast(_ClientLike, Client(parameters, input_required_max_rounds=0))


def _is_retryable_connect_failure(exc: Exception) -> bool:
    """Return whether a connect failure is an explicit transient transport signal."""

    if isinstance(exc, ExceptionGroup):
        return bool(exc.exceptions) and all(
            _is_retryable_connect_failure(item) for item in exc.exceptions
        )
    return isinstance(exc, (TimeoutError, ConnectionError))


async def _close_client(client: _ClientLike) -> None:
    try:
        await client.__aexit__(None, None, None)
    except asyncio.CancelledError:
        raise
    except Exception:
        return


def _descriptor(server_id: str, tool: object) -> MCPToolDescriptor:
    remote_name = getattr(tool, "name", None)
    description = getattr(tool, "description", None)
    schema = getattr(tool, "input_schema", None)
    if not isinstance(remote_name, str) or not remote_name:
        raise MCPError(
            f"MCP server {server_id!r} advertised an invalid Tool name.",
            code="MCP_INVALID_SCHEMA",
        )
    if description is not None and not isinstance(description, str):
        raise MCPError(
            f"MCP Tool {server_id!r}/{remote_name!r} has an invalid description.",
            code="MCP_INVALID_SCHEMA",
        )
    if not isinstance(schema, dict) or any(not isinstance(key, str) for key in schema):
        raise MCPError(
            f"MCP Tool {server_id!r}/{remote_name!r} has an invalid input schema.",
            code="MCP_INVALID_SCHEMA",
        )
    try:
        encoded = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise MCPError(
            f"MCP Tool {server_id!r}/{remote_name!r} has a non-JSON input schema.",
            code="MCP_INVALID_SCHEMA",
        ) from exc
    if len(encoded.encode("utf-8")) > _MAX_SCHEMA_BYTES:
        raise MCPError(
            f"MCP Tool {server_id!r}/{remote_name!r} input schema is too large.",
            code="MCP_INVALID_SCHEMA",
        )
    return MCPToolDescriptor(
        server_id,
        remote_name,
        f"mcp.{server_id}.{remote_name}",
        None if description is None else description[:_MAX_DESCRIPTION_CHARS],
        cast(JsonObject, schema),
    )


def _normalize_result(result: object) -> JsonObject:
    content = getattr(result, "content", None)
    structured = getattr(result, "structured_content", None)
    is_error = getattr(result, "is_error", None)
    if not isinstance(content, list) or not isinstance(is_error, bool):
        raise MCPError("MCP returned an invalid Tool result.", code="MCP_INVALID_RESULT")
    if structured is not None and not isinstance(structured, dict):
        raise MCPError("MCP returned invalid structured content.", code="MCP_INVALID_RESULT")
    return {
        "content": [_safe_content(block) for block in content],
        "structured_content": None if structured is None else _safe_json(structured),
        "is_error": is_error,
    }


def _safe_content(block: object) -> object:
    dump = getattr(block, "model_dump", None)
    if callable(dump):
        value = dump(mode="json", by_alias=False)
    else:
        value = {"type": type(block).__name__, "text": str(block)[:_MAX_TEXT_CHARS]}
    return _safe_json(value)


def _safe_json(value: object, *, key: str | None = None) -> object:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        limit = _MAX_BINARY_CHARS if key in {"data", "blob"} else _MAX_TEXT_CHARS
        if len(value) <= limit:
            return value
        return value[:limit] + "...[truncated]"
    if isinstance(value, list):
        return [_safe_json(item) for item in value[:100]]
    if isinstance(value, dict):
        return {
            str(item_key): _safe_json(item, key=str(item_key))
            for item_key, item in list(value.items())[:100]
        }
    raise MCPError("MCP returned non-JSON content.", code="MCP_INVALID_RESULT")


def _optional_text(value: object | None, attribute: str) -> str | None:
    result = None if value is None else getattr(value, attribute, None)
    return result if isinstance(result, str) else None
