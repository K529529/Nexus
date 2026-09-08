from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest

from nexus.application.planning import JsonAgentDecisionAdapter, ModelPlanner
from nexus.config.models import MCPServerConfig, MCPToolRiskConfig, RuntimeConfig
from nexus.domain.agent_decision import AgentDecisionKind, AgentDecisionRequest
from nexus.domain.exploration import WorkingContext
from nexus.domain.mcp import MCPConnectionInfo, MCPToolDescriptor
from nexus.domain.model import ModelChunk, ModelMessage, ModelResponse
from nexus.domain.planning import PlanKind
from nexus.domain.ports.mcp import MCPManager
from nexus.domain.ports.planning import PlanningRequest
from nexus.domain.tooling import JsonObject, RiskLevel
from nexus.infrastructure.bootstrap import bootstrap_tool_application
from nexus.infrastructure.bootstrap import composition as composition_module
from nexus.infrastructure.bootstrap.composition import _adapt_mcp_tools
from nexus.tools.registry import ToolRegistry


class NeverCalledManager:
    async def connect(self) -> tuple[MCPConnectionInfo, ...]:
        raise AssertionError("connection is outside this adaptation fixture")

    async def list_tools(self) -> tuple[MCPToolDescriptor, ...]:
        raise AssertionError("discovery is outside this adaptation fixture")

    async def call_tool(
        self,
        *,
        server_id: str,
        remote_name: str,
        arguments: JsonObject,
        timeout_seconds: float,
    ) -> JsonObject:
        raise AssertionError((server_id, remote_name, arguments, timeout_seconds))

    async def close(self) -> None:
        return None


class CaptureGateway:
    def __init__(self, *responses: JsonObject) -> None:
        self._responses = [json.dumps(item) for item in responses]
        self.messages: list[Sequence[ModelMessage]] = []

    async def complete(self, messages: Sequence[ModelMessage]) -> ModelResponse:
        self.messages.append(tuple(messages))
        return ModelResponse(self._responses.pop(0))

    async def stream(
        self, messages: Sequence[ModelMessage]
    ) -> AsyncIterator[ModelChunk]:
        del messages
        if False:
            yield ModelChunk("")


class NativeCollision:
    name = "mcp.everything.echo"

    async def execute(self, invocation: object) -> object:
        raise AssertionError(invocation)


def _adapted() -> tuple[ToolRegistry, list[JsonObject]]:
    config = MCPServerConfig(
        server_id="everything",
        command="secret-command-must-not-reach-model",
        args=("--secret-token",),
        tool_risks=(
            MCPToolRiskConfig(tool_name="echo", risk_level=RiskLevel.SAFE),
            MCPToolRiskConfig(tool_name="write", risk_level=RiskLevel.WRITE),
            MCPToolRiskConfig(tool_name="danger", risk_level=RiskLevel.DANGEROUS),
        ),
    )
    descriptors = tuple(
        MCPToolDescriptor(
            "everything",
            remote_name,
            f"mcp.everything.{remote_name}",
            f"Fixture {remote_name} tool.",
            {
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
            },
        )
        for remote_name in ("echo", "write", "danger", "unconfigured")
    )
    adapters, risks, unsupported_writes, metadata = _adapt_mcp_tools(
        descriptors,
        (config,),
        cast(MCPManager, NeverCalledManager()),
    )
    assert risks == {
        "mcp.everything.echo": RiskLevel.SAFE,
        "mcp.everything.write": RiskLevel.WRITE,
        "mcp.everything.danger": RiskLevel.DANGEROUS,
        "mcp.everything.unconfigured": RiskLevel.DANGEROUS,
    }
    assert unsupported_writes == {"mcp.everything.write"}
    return ToolRegistry(adapters), metadata


def test_safe_tool_is_registered_and_visible() -> None:
    registry, metadata = _adapted()

    assert registry.resolve("mcp.everything.echo").name == "mcp.everything.echo"
    assert [item["registry_name"] for item in metadata] == ["mcp.everything.echo"]


def test_mcp_registry_collision_cannot_overwrite_existing_tool() -> None:
    registry, _ = _adapted()
    adapted = registry.resolve("mcp.everything.echo")

    with pytest.raises(ValueError, match="Duplicate Tool name"):
        ToolRegistry([cast(Any, NativeCollision()), adapted])


@pytest.mark.asyncio
async def test_mcp_disabled_bootstrap_makes_zero_connection_attempts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fail_if_constructed(*args: object, **kwargs: object) -> object:
        raise AssertionError((args, kwargs))

    monkeypatch.setattr(composition_module, "SDKMCPManager", fail_if_constructed)
    config = RuntimeConfig(
        mcp_enabled=False,
        mcp_servers=(MCPServerConfig(server_id="configured", command="never-run"),),
    )

    async with bootstrap_tool_application(config, workspace_path=tmp_path) as application:
        assert application.tool_runtime is not None


def test_write_tool_is_registered_but_not_visible() -> None:
    registry, metadata = _adapted()

    assert registry.resolve("mcp.everything.write").name == "mcp.everything.write"
    assert "mcp.everything.write" not in {item["registry_name"] for item in metadata}


@pytest.mark.parametrize("remote_name", ["danger", "unconfigured"])
def test_dangerous_or_unconfigured_tool_is_registered_but_not_visible(
    remote_name: str,
) -> None:
    registry, metadata = _adapted()

    full_name = f"mcp.everything.{remote_name}"
    assert registry.resolve(full_name).name == full_name
    assert full_name not in {item["registry_name"] for item in metadata}


@pytest.mark.asyncio
async def test_safe_metadata_drives_concrete_planner_and_agent_only() -> None:
    _, metadata = _adapted()
    gateway = CaptureGateway(
        {
            "rationale_summary": "Use the discovered SAFE echo tool.",
            "steps": [
                {
                    "description": "Echo the requested message",
                    "tool_name": "mcp.everything.echo",
                    "target_paths": [],
                    "command_argv": None,
                    "command_cwd": None,
                }
            ],
        },
        {
            "kind": "TOOL_ACTION",
            "summary": "Call the discovered SAFE echo tool.",
            "action": {
                "tool_name": "mcp.everything.echo",
                "arguments": {"message": "hello"},
            },
        },
    )
    context = WorkingContext("echo hello", (), (), (), (), False)
    run_id = str(uuid4())
    session_id = str(uuid4())
    planner = ModelPlanner(gateway, tool_metadata=metadata)
    plan = await planner.create_plan(
        PlanningRequest(
            "echo hello",
            context,
            PlanKind.INITIAL,
            None,
            None,
            run_id,
            session_id,
        )
    )
    agent = JsonAgentDecisionAdapter(gateway, tool_metadata=metadata)

    decision = await agent.decide(
        AgentDecisionRequest("echo hello", context, plan, ())
    )

    assert plan.steps[0].tool_name == "mcp.everything.echo"
    assert decision.kind is AgentDecisionKind.TOOL_ACTION
    assert decision.action is not None
    assert decision.action.tool_name == "mcp.everything.echo"
    assert len(gateway.messages) == 2
    for model_messages in gateway.messages:
        system_prompt = model_messages[0].content
        assert "mcp.everything.echo" in system_prompt
        assert "mcp.everything.write" not in system_prompt
        assert "mcp.everything.danger" not in system_prompt
        assert "mcp.everything.unconfigured" not in system_prompt
        assert "secret-command-must-not-reach-model" not in system_prompt
        assert "--secret-token" not in system_prompt
