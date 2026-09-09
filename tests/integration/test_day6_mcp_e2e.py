from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import cast

import pytest

from nexus.application.runtime import NexusRuntime
from nexus.config.models import (
    ApprovalMode,
    MCPServerConfig,
    MCPToolRiskConfig,
    RuntimeConfig,
)
from nexus.domain.agent_state import AgentState
from nexus.domain.model import ModelChunk, ModelMessage, ModelResponse
from nexus.domain.planning import TerminalStatus
from nexus.domain.runtime_events import FinalResult, ToolFinished, ToolStarted
from nexus.domain.tooling import RiskLevel
from nexus.infrastructure.bootstrap import bootstrap_application
from nexus.infrastructure.graph.day4_runtime import Day4LangGraphRuntime

_PACKAGE = "@modelcontextprotocol/server-everything@2026.8.31"
_REGISTRY_NAME = "mcp.everything.echo"


class RealMCPGateway:
    def __init__(self) -> None:
        responses = [
            {
                "selected_skill_ids": [],
                "selection_reason_summary": "No builtin Skill is relevant to the MCP fixture.",
            },
            {
                "rationale_summary": "Use the discovered SAFE MCP echo tool and validate.",
                "steps": [
                    {
                        "description": "Echo the Day 6 acceptance message",
                        "tool_name": _REGISTRY_NAME,
                        "target_paths": [],
                        "command_argv": None,
                        "command_cwd": None,
                    },
                    {
                        "description": "BASIC_EXECUTION: confirm Python runtime",
                        "tool_name": "shell",
                        "target_paths": [],
                        "command_argv": ["python", "--version"],
                        "command_cwd": ".",
                    },
                ],
            },
            {
                "kind": "TOOL_ACTION",
                "summary": "Call the selected discovered MCP echo tool.",
                "action": {
                    "tool_name": _REGISTRY_NAME,
                    "arguments": {"message": "nexus-day6-e2e"},
                },
            },
            {
                "kind": "TASK_READY",
                "summary": "The MCP echo action completed and is ready for validation.",
                "action": None,
            },
        ]
        self._responses = [json.dumps(response) for response in responses]
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


def _server_config() -> MCPServerConfig:
    command: str
    args: tuple[str, ...]
    if os.name == "nt":
        command = "cmd"
        args = ("/c", "npx", "-y", _PACKAGE)
    else:
        command = "npx"
        args = ("-y", _PACKAGE)
    return MCPServerConfig(
        server_id="everything",
        command=command,
        args=args,
        connect_timeout_seconds=60,
        tool_timeout_seconds=30,
        max_connect_attempts=1,
        tool_risks=(
            MCPToolRiskConfig(tool_name="echo", risk_level=RiskLevel.SAFE),
        ),
    )


def _require_local_tools() -> str:
    npx = shutil.which("npx")
    if npx is None:
        pytest.skip("npx is unavailable for the pinned Everything Server E2E.")
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is unavailable for the real Agent-path fixture.")
    return git


def _initialize_repository(git: str, workspace: Path) -> None:
    (workspace / "README.md").write_text("# Day 6 MCP E2E\n", encoding="utf-8")
    for arguments in (
        ("init",),
        ("config", "core.autocrlf", "false"),
        ("add", "README.md"),
        (
            "-c",
            "user.name=Nexus Test",
            "-c",
            "user.email=nexus@example.invalid",
            "commit",
            "-m",
            "fixture",
        ),
    ):
        subprocess.run(
            [git, *arguments],
            cwd=workspace,
            check=True,
            capture_output=True,
            text=True,
        )


async def _checkpoint_state(runtime: NexusRuntime, run_id: str) -> AgentState:
    graph_runtime = cast(Day4LangGraphRuntime, runtime._graph_runtime)  # noqa: SLF001
    snapshot = await graph_runtime._graph.aget_state(  # noqa: SLF001
        {"configurable": {"thread_id": f"nexus-run:{run_id}"}}
    )
    return AgentState(**snapshot.values)


@pytest.mark.postgres
@pytest.mark.mcp_e2e
@pytest.mark.asyncio
async def test_real_everything_server_runs_through_production_agent_path(
    migrated_database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    git = _require_local_tools()
    monkeypatch.setenv(
        "npm_config_cache", str(Path(tempfile.gettempdir()) / "nexus-day6-npm-cache")
    )
    monkeypatch.setenv("npm_config_registry", "https://registry.npmjs.org/")
    _initialize_repository(git, tmp_path)
    gateway = RealMCPGateway()
    config = RuntimeConfig(
        database_url=migrated_database_url,
        model_name="fixture-model",
        approval_mode=ApprovalMode.AUTO,
        mcp_enabled=True,
        mcp_servers=(_server_config(),),
    )
    cleanup_confirmed = False

    async with bootstrap_application(
        config,
        model_gateway=gateway,
        workspace_path=tmp_path,
    ) as application:
        events = [
            event
            async for event in application.runtime.run(
                "Echo nexus-day6-e2e through the discovered SAFE MCP tool"
            )
        ]
        final = events[-1]
        assert isinstance(final, FinalResult)
        state = await _checkpoint_state(application.runtime, final.run_id)
    cleanup_confirmed = True

    assert cleanup_confirmed
    assert final.terminal_status is TerminalStatus.SUCCEEDED
    assert state.plan is not None
    assert state.plan.steps[0].tool_name == _REGISTRY_NAME
    assert len(state.observations) == 1
    assert state.observations[0].tool_name == _REGISTRY_NAME
    assert state.observations[0].success is True
    mcp_results = [item for item in state.tool_results if item.tool_name == _REGISTRY_NAME]
    assert len(mcp_results) == 1
    assert mcp_results[0].success is True
    assert mcp_results[0].output is not None
    assert mcp_results[0].output["is_error"] is False
    content = mcp_results[0].output["content"]
    assert isinstance(content, list) and content
    assert isinstance(content[0], dict)
    assert "nexus-day6-e2e" in str(content[0].get("text"))
    assert sum(
        isinstance(event, ToolStarted) and event.tool_name == _REGISTRY_NAME
        for event in events
    ) == 1
    assert sum(
        isinstance(event, ToolFinished)
        and event.tool_name == _REGISTRY_NAME
        and event.success
        for event in events
    ) == 1
    assert len(gateway.messages) == 4
    for messages in gateway.messages[1:]:
        assert _REGISTRY_NAME in messages[0].content
