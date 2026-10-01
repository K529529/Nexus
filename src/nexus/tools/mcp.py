"""Explicit user-configured stdio servers are dynamic tool providers only."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import time
from contextlib import AsyncExitStack
from typing import Any

from mcp import Client, StdioServerParameters

from nexus.core.types import (
    Emit,
    ExecutionContext,
    Json,
    Tool,
    ToolCancelled,
    ToolResult,
    ToolSpec,
    json_text,
)


def public_name(server: str, remote: str) -> str:
    readable = re.sub(r"[^a-zA-Z0-9_-]", "_", f"mcp_{server}_{remote}")[:46]
    digest = hashlib.sha256(json_text([server, remote]).encode()).hexdigest()[:16]
    return f"{readable}_{digest}"


def server_parameters(raw: Any) -> StdioServerParameters:
    if not isinstance(raw, dict) or raw.keys() - {"command", "args", "env_from"}:
        raise ValueError("invalid server fields")
    command, args, env_from = raw.get("command"), raw.get("args", []), raw.get("env_from", {})
    if not isinstance(command, str) or not command.strip():
        raise ValueError("command must be a non-empty string")
    if not isinstance(args, list) or any(not isinstance(a, str) for a in args):
        raise ValueError("args must be a string list")
    if not isinstance(env_from, dict):
        raise ValueError("env_from must be a table")
    env: dict[str, str] = {}
    for target, source in env_from.items():
        if not isinstance(source, str) or not os.environ.get(source):
            raise ValueError("a configured environment variable is missing")
        env[target] = os.environ[source]
    return StdioServerParameters(command=command, args=args, env=env)


def _bounded_result(result: Any, limit: int) -> tuple[Json, bool, bool]:
    data: Json = {"text": [], "unsupported_content": []}
    truncated = False
    for block in result.content:
        if block.type == "text":
            raw = block.text.encode("utf-8")
            data["text"].append(raw[:limit].decode("utf-8", errors="ignore"))
            truncated |= len(raw) > limit
            limit = max(0, limit - len(raw))
        else:
            if block.type not in data["unsupported_content"]:
                data["unsupported_content"].append(block.type)
    if result.structured_content is not None:
        raw = json_text(result.structured_content).encode("utf-8")
        if len(raw) <= limit:
            data["structured_content"] = result.structured_content
        else:
            data["structured_content_preview"] = raw[:limit].decode("utf-8", errors="ignore")
            truncated = True
    unsupported = bool(data["unsupported_content"]) or result.result_type != "complete"
    if result.result_type != "complete":
        data["detail"] = "input-required/sampling/elicitation is unsupported"
    return data, truncated, unsupported


def adapt_tool(
    client: Any,
    remote: str,
    spec: ToolSpec,
    server: str,
    disabled: set[str],
    registry: dict[str, Tool],
    server_names: list[str],
) -> Tool:
    async def execute(arguments: Json, context: ExecutionContext, emit: Emit) -> ToolResult:
        started = time.monotonic()
        try:
            if server in disabled:
                return ToolResult(context.call_id, False, {"server": server}, "mcp_unavailable")
            async with asyncio.timeout(120):
                result = await client.call_tool(remote, arguments, read_timeout_seconds=120)
            data, truncated, unsupported = _bounded_result(result, context.output_limit_bytes)
            code = (
                "mcp_unsupported_content"
                if unsupported
                else "mcp_tool_error"
                if result.is_error
                else None
            )
            return ToolResult(
                context.call_id,
                code is None,
                data,
                code,
                int((time.monotonic() - started) * 1000),
                truncated,
            )
        except asyncio.CancelledError:
            # Bootstrap owns the client context in this same turn task and closes it.
            raise ToolCancelled(
                ToolResult(
                    context.call_id,
                    False,
                    {
                        "server": server,
                        "side_effects": "unknown",
                        "cancelled": True,
                    },
                    "interrupted_unknown",
                )
            ) from None
        except Exception as exc:
            disabled.add(server)
            for name in server_names:
                registry.pop(name, None)
            code = "mcp_timeout" if isinstance(exc, TimeoutError) else "mcp_unavailable"
            await emit("warning", {"detail": f"MCP server {server} disabled ({code}); no retry."})
            return ToolResult(
                context.call_id,
                False,
                {"server": server, "side_effects": "unknown"},
                code,
                int((time.monotonic() - started) * 1000),
            )

    return Tool(spec, execute)


async def connect_servers(
    configured: Json,
    registry: dict[str, Tool],
    stack: AsyncExitStack,
    emit: Emit,
    *,
    client_factory: Any = Client,
) -> list[str]:
    unavailable: list[str] = []
    disabled: set[str] = set()
    for server, raw in configured.items():
        local = AsyncExitStack()
        try:
            parameters = server_parameters(raw)
            client = client_factory(
                parameters, read_timeout_seconds=15, input_required_max_rounds=0
            )
            async with asyncio.timeout(15):
                await local.enter_async_context(client)
                found: dict[str, ToolSpec] = {}
                cursor: str | None = None
                seen: set[str] = set()
                while True:
                    page = await client.list_tools(cursor=cursor)
                    if page.result_type != "complete":
                        raise ValueError("unsupported discovery response")
                    for remote in page.tools:
                        if (
                            not remote.name
                            or remote.name in found
                            or not isinstance(remote.input_schema, dict)
                            or remote.input_schema.get("type") != "object"
                        ):
                            raise ValueError("invalid tool definition")
                        name = public_name(server, remote.name)
                        if name in registry or any(t.name == name for t in found.values()):
                            raise ValueError("tool name collision")
                        found[remote.name] = ToolSpec(
                            name, remote.description or "MCP tool", remote.input_schema
                        )
                    cursor = page.next_cursor
                    if cursor is None:
                        break
                    if not cursor or cursor in seen:
                        raise ValueError("invalid/repeated pagination cursor")
                    seen.add(cursor)
            names = [spec.name for spec in found.values()]
            for remote, spec in found.items():
                registry[spec.name] = adapt_tool(
                    client, remote, spec, server, disabled, registry, names
                )
            stack.push_async_callback(local.aclose)
        except asyncio.CancelledError:
            await local.aclose()
            raise
        except Exception as exc:
            await local.aclose()
            unavailable.append(server)
            await emit(
                "warning", {"detail": f"MCP server {server} unavailable ({type(exc).__name__})."}
            )
    return unavailable
