from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from nexus.domain.mcp import MCPConnectionInfo, MCPToolDescriptor
from nexus.domain.tooling import PolicyDecision, RiskLevel, ToolInvocation
from nexus.errors import MCPError
from nexus.tools.mcp import MCPToolAdapter


class FakeManager:
    def __init__(self, result: dict[str, object] | BaseException) -> None:
        self.result = result
        self.calls: list[tuple[str, str, dict[str, object], float]] = []

    async def connect(self) -> tuple[MCPConnectionInfo, ...]:
        return ()

    async def list_tools(self) -> tuple[MCPToolDescriptor, ...]:
        return ()

    async def call_tool(
        self,
        *,
        server_id: str,
        remote_name: str,
        arguments: dict[str, object],
        timeout_seconds: float,
    ) -> dict[str, object]:
        self.calls.append((server_id, remote_name, dict(arguments), timeout_seconds))
        if isinstance(self.result, asyncio.CancelledError):
            raise self.result
        if isinstance(self.result, BaseException):
            raise self.result
        return dict(self.result)

    async def close(self) -> None:
        return None


def _descriptor() -> MCPToolDescriptor:
    return MCPToolDescriptor(
        "everything",
        "echo",
        "mcp.everything.echo",
        "Echo a message.",
        {
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
            "additionalProperties": False,
        },
    )


def _invocation(arguments: dict[str, object] | None = None) -> ToolInvocation:
    return ToolInvocation(
        str(uuid4()),
        "mcp.everything.echo",
        arguments or {"message": "hello"},
        str(uuid4()),
        str(uuid4()),
    )


@pytest.mark.asyncio
async def test_success_maps_to_tool_result_and_preserves_identity() -> None:
    manager = FakeManager(
        {
            "content": [{"type": "text", "text": "hello"}],
            "structured_content": {"message": "hello"},
            "is_error": False,
        }
    )
    adapter = MCPToolAdapter(
        _descriptor(), manager, risk_level=RiskLevel.SAFE, timeout_seconds=12
    )
    invocation = _invocation()

    result = await adapter.execute(invocation)

    assert result.success
    assert result.invocation_id == invocation.invocation_id
    assert result.tool_name == invocation.tool_name
    assert result.risk_level is RiskLevel.SAFE
    assert result.policy_decision is PolicyDecision.ALLOWED
    assert result.output == manager.result
    assert manager.calls == [("everything", "echo", {"message": "hello"}, 12)]


@pytest.mark.asyncio
async def test_mcp_declared_error_maps_to_bounded_tool_error() -> None:
    manager = FakeManager(
        {
            "content": [{"type": "text", "text": "remote failure"}],
            "structured_content": None,
            "is_error": True,
        }
    )
    adapter = MCPToolAdapter(
        _descriptor(), manager, risk_level=RiskLevel.SAFE, timeout_seconds=5
    )

    result = await adapter.execute(_invocation())

    assert not result.success
    assert result.error is not None
    assert result.error.code == "MCP_TOOL_ERROR"
    assert result.error.message == "remote failure"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("manager_error", "expected_code"),
    [
        (MCPError("safe timeout", code="MCP_TOOL_TIMEOUT", retryable=True), "MCP_TOOL_TIMEOUT"),
        (MCPError("safe call failure", code="MCP_CALL_FAILED"), "MCP_CALL_FAILED"),
    ],
)
async def test_manager_errors_map_without_raw_exception_escape(
    manager_error: MCPError, expected_code: str
) -> None:
    adapter = MCPToolAdapter(
        _descriptor(),
        FakeManager(manager_error),
        risk_level=RiskLevel.SAFE,
        timeout_seconds=5,
    )

    result = await adapter.execute(_invocation())

    assert not result.success
    assert result.error is not None and result.error.code == expected_code


@pytest.mark.asyncio
async def test_unexpected_manager_exception_is_sanitized_as_call_failure() -> None:
    adapter = MCPToolAdapter(
        _descriptor(),
        FakeManager(RuntimeError("private SDK detail")),
        risk_level=RiskLevel.SAFE,
        timeout_seconds=5,
    )

    result = await adapter.execute(_invocation())

    assert not result.success
    assert result.error is not None
    assert result.error.code == "MCP_CALL_FAILED"
    assert "private SDK detail" not in result.error.message


@pytest.mark.asyncio
async def test_malformed_arguments_fail_before_manager_call() -> None:
    manager = FakeManager(
        {"content": [], "structured_content": None, "is_error": False}
    )
    adapter = MCPToolAdapter(
        _descriptor(), manager, risk_level=RiskLevel.SAFE, timeout_seconds=5
    )

    result = await adapter.execute(_invocation({"message": 1}))

    assert not result.success
    assert result.error is not None and result.error.code == "MCP_INVALID_SCHEMA"
    assert manager.calls == []


@pytest.mark.asyncio
async def test_invalid_result_maps_to_invalid_result() -> None:
    adapter = MCPToolAdapter(
        _descriptor(),
        FakeManager({"content": "not-a-list", "is_error": False}),
        risk_level=RiskLevel.SAFE,
        timeout_seconds=5,
    )

    result = await adapter.execute(_invocation())

    assert not result.success
    assert result.error is not None and result.error.code == "MCP_INVALID_RESULT"


@pytest.mark.asyncio
async def test_cancellation_is_reraised() -> None:
    adapter = MCPToolAdapter(
        _descriptor(),
        FakeManager(asyncio.CancelledError()),
        risk_level=RiskLevel.SAFE,
        timeout_seconds=5,
    )

    with pytest.raises(asyncio.CancelledError):
        await adapter.execute(_invocation())
