"""Adapt SDK-neutral MCP descriptors to the existing Nexus Tool contract."""

from __future__ import annotations

import asyncio
from typing import cast

from nexus.domain.mcp import MCPToolDescriptor
from nexus.domain.ports.mcp import MCPManager
from nexus.domain.tooling import (
    JsonObject,
    PolicyDecision,
    RiskLevel,
    ToolError,
    ToolInvocation,
    ToolResult,
)
from nexus.errors import MCPError


class MCPToolAdapter:
    def __init__(
        self,
        descriptor: MCPToolDescriptor,
        manager: MCPManager,
        *,
        risk_level: RiskLevel,
        timeout_seconds: float,
    ) -> None:
        self.descriptor = descriptor
        self._manager = manager
        self.risk_level = risk_level
        self._timeout_seconds = timeout_seconds

    @property
    def name(self) -> str:
        return self.descriptor.registry_name

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        if invocation.tool_name != self.name:
            return self._failure(
                invocation,
                MCPError("MCP Tool invocation identity is invalid.", code="MCP_CALL_FAILED"),
            )
        try:
            _validate_arguments(invocation.arguments, self.descriptor.input_schema)
        except MCPError as exc:
            return self._failure(invocation, exc)
        try:
            result = await self._manager.call_tool(
                server_id=self.descriptor.server_id,
                remote_name=self.descriptor.remote_name,
                arguments=invocation.arguments,
                timeout_seconds=self._timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except MCPError as exc:
            return self._failure(invocation, exc)
        except Exception:
            return self._failure(
                invocation,
                MCPError("MCP Tool call failed safely.", code="MCP_CALL_FAILED"),
            )
        try:
            if result.get("is_error") is True:
                return self._failure(
                    invocation,
                    MCPError(
                        _mcp_error_message(result),
                        code="MCP_TOOL_ERROR",
                        retryable=False,
                    ),
                )
            if result.get("is_error") is not False:
                raise MCPError("MCP returned an invalid Tool result.", code="MCP_INVALID_RESULT")
            content = result.get("content")
            structured = result.get("structured_content")
            if not isinstance(content, list) or (
                structured is not None and not isinstance(structured, dict)
            ):
                raise MCPError("MCP returned an invalid Tool result.", code="MCP_INVALID_RESULT")
            return ToolResult(
                invocation.invocation_id,
                invocation.tool_name,
                True,
                cast(JsonObject, dict(result)),
                None,
                self.risk_level,
                PolicyDecision.ALLOWED,
                None,
                0,
            )
        except asyncio.CancelledError:
            raise
        except MCPError as exc:
            return self._failure(invocation, exc)
        except Exception:
            return self._failure(
                invocation,
                MCPError("MCP returned an invalid Tool result.", code="MCP_INVALID_RESULT"),
            )

    def _failure(self, invocation: ToolInvocation, error: MCPError) -> ToolResult:
        return ToolResult(
            invocation.invocation_id,
            invocation.tool_name,
            False,
            None,
            ToolError(error.code, str(error), error.retryable),
            self.risk_level,
            PolicyDecision.ALLOWED,
            None,
            0,
        )


def _validate_arguments(arguments: JsonObject, schema: JsonObject) -> None:
    if schema.get("type", "object") != "object":
        raise MCPError("MCP Tool input schema must describe an object.", code="MCP_INVALID_SCHEMA")
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    if not isinstance(properties, dict) or not isinstance(required, list) or any(
        not isinstance(item, str) for item in required
    ):
        raise MCPError("MCP Tool input schema is malformed.", code="MCP_INVALID_SCHEMA")
    missing = [item for item in required if item not in arguments]
    if missing:
        raise MCPError("MCP Tool arguments omit required fields.", code="MCP_INVALID_SCHEMA")
    if schema.get("additionalProperties") is False and not set(arguments) <= set(properties):
        raise MCPError("MCP Tool arguments contain unknown fields.", code="MCP_INVALID_SCHEMA")
    for name, value in arguments.items():
        property_schema = properties.get(name)
        if isinstance(property_schema, dict):
            _validate_primitive(value, property_schema.get("type"))


def _validate_primitive(value: object, expected: object) -> None:
    valid = {
        "string": lambda item: isinstance(item, str),
        "boolean": lambda item: isinstance(item, bool),
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "number": lambda item: isinstance(item, (int, float)) and not isinstance(item, bool),
        "object": lambda item: isinstance(item, dict),
        "array": lambda item: isinstance(item, list),
        "null": lambda item: item is None,
    }
    if isinstance(expected, str) and expected in valid and not valid[expected](value):
        raise MCPError("MCP Tool argument type is invalid.", code="MCP_INVALID_SCHEMA")


def _mcp_error_message(result: JsonObject) -> str:
    content = result.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str) and text:
                    return text[:512]
    return "The MCP server reported a Tool error."
