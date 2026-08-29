"""Async Day 3 tool, policy, and sandbox ports."""

from collections.abc import Awaitable, Callable
from typing import Protocol

from nexus.domain.approvals import ApprovalRequest
from nexus.domain.tooling import (
    ApprovalDecision,
    JsonObject,
    RiskLevel,
    SandboxRequest,
    SandboxResult,
    ToolInvocation,
    ToolResult,
)

InteractiveDecisionCallback = Callable[
    [ApprovalRequest],
    Awaitable[tuple[ApprovalDecision, str | None]],
]


class Tool(Protocol):
    @property
    def name(self) -> str: ...

    async def execute(self, invocation: ToolInvocation) -> ToolResult: ...


class CommandPolicy(Protocol):
    def classify(self, *, operation: str, arguments: JsonObject) -> RiskLevel: ...


class ApprovalPolicy(Protocol):
    async def request(self, request: ApprovalRequest) -> ApprovalRequest: ...


class SandboxExecutor(Protocol):
    async def execute(self, request: SandboxRequest) -> SandboxResult: ...
