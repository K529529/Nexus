"""Nexus-owned, SDK-neutral MCP metadata values."""

from __future__ import annotations

from dataclasses import dataclass

from nexus.domain.tooling import JsonObject


@dataclass(frozen=True, slots=True)
class MCPToolDescriptor:
    server_id: str
    remote_name: str
    registry_name: str
    description: str | None
    input_schema: JsonObject

    def __post_init__(self) -> None:
        if not self.server_id or not self.remote_name or not self.registry_name:
            raise ValueError("MCP Tool identity must not be empty.")
        if self.description is not None and not isinstance(self.description, str):
            raise ValueError("MCP Tool description must be text or None.")
        if not _is_json_value(self.input_schema):
            raise ValueError("MCP input schema must be a JSON object.")
        object.__setattr__(self, "input_schema", dict(self.input_schema))


@dataclass(frozen=True, slots=True)
class MCPConnectionInfo:
    server_id: str
    connected: bool
    server_name: str | None
    server_version: str | None
    negotiated_protocol_version: str | None

    def __post_init__(self) -> None:
        if not self.server_id:
            raise ValueError("MCP server_id must not be empty.")


def _is_json_value(value: object) -> bool:
    if value is None or isinstance(value, (str, int, float, bool)):
        return True
    if isinstance(value, list):
        return all(_is_json_value(item) for item in value)
    if isinstance(value, dict):
        return all(
            isinstance(key, str) and _is_json_value(item)
            for key, item in value.items()
        )
    return False
