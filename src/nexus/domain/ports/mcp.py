"""Async MCP lifecycle port without SDK-specific types."""

from typing import Protocol

from nexus.domain.mcp import MCPConnectionInfo, MCPToolDescriptor
from nexus.domain.tooling import JsonObject


class MCPManager(Protocol):
    async def connect(self) -> tuple[MCPConnectionInfo, ...]: ...

    async def list_tools(self) -> tuple[MCPToolDescriptor, ...]: ...

    async def call_tool(
        self,
        *,
        server_id: str,
        remote_name: str,
        arguments: JsonObject,
        timeout_seconds: float,
    ) -> JsonObject: ...

    async def close(self) -> None: ...
