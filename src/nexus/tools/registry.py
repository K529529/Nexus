"""Immutable exact-name Tool registry."""

from collections.abc import Sequence

from nexus.domain.ports.tooling import Tool
from nexus.errors import ToolExecutionError


class ToolRegistry:
    def __init__(self, tools: Sequence[Tool]) -> None:
        registry: dict[str, Tool] = {}
        for tool in tools:
            if not tool.name:
                raise ValueError("Tool names must not be empty.")
            if tool.name in registry:
                raise ValueError(f"Duplicate Tool name: {tool.name}")
            registry[tool.name] = tool
        self._tools = registry

    def resolve(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ToolExecutionError(
                "The requested Tool is not registered.",
                code="TOOL_NOT_FOUND",
            ) from exc
