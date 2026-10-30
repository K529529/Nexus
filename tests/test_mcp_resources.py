from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from mcp.types import (
    BlobResourceContents,
    CallToolResult,
    EmbeddedResource,
    ResourceLink,
    TextContent,
    TextResourceContents,
)

from nexus.core.types import ExecutionContext, ToolResult, ToolSpec
from nexus.tools.mcp import adapt_tool
from tests.conftest import Recorder


async def execute_result(result: CallToolResult, limit: int = 1024) -> ToolResult:
    class Client:
        async def call_tool(self, *args: Any, **kwargs: Any) -> CallToolResult:
            return result

    tool = adapt_tool(
        Client(), "read", ToolSpec("read", "Read", {"type": "object"}), "test", set(), {}, []
    )
    return await tool.execute({}, ExecutionContext(Path.cwd(), "call", "unused", limit), Recorder())


@pytest.mark.parametrize("is_error", [False, True])
@pytest.mark.parametrize("text", ["", "[project]\nname = 'example'\n说明"])
async def test_embedded_text_matches_plain_text_and_preserves_server_error(
    text: str,
    is_error: bool,
) -> None:
    plain = await execute_result(
        CallToolResult(
            content=[TextContent(text=text)],
            structured_content={"name": "example"},
            is_error=is_error,
        )
    )
    embedded = await execute_result(
        CallToolResult(
            content=[
                EmbeddedResource(
                    resource=TextResourceContents(
                        uri="repo://example/project.toml", mime_type="text/plain", text=text
                    )
                )
            ],
            structured_content={"name": "example"},
            is_error=is_error,
        )
    )
    assert (
        embedded.data
        == plain.data
        == {"text": [text], "unsupported_content": [], "structured_content": {"name": "example"}}
    )
    assert embedded.ok == plain.ok == (not is_error)
    assert embedded.error_code == plain.error_code == ("mcp_tool_error" if is_error else None)
    assert not embedded.truncated
    message = json.loads(embedded.message().content)
    assert message["data"]["text"] == [text] and message["ok"] == (not is_error)


@pytest.mark.parametrize("limit", [0, 4, 5, 6, 10, 100])
async def test_mixed_text_resources_share_utf8_budget_with_structured_content(limit: int) -> None:
    result = await execute_result(
        CallToolResult(
            content=[
                TextContent(text="abc"),
                EmbeddedResource(resource=TextResourceContents(uri="test://one", text="你好")),
                EmbeddedResource(resource=TextResourceContents(uri="test://two", text="z")),
            ],
            structured_content={"ok": True},
        ),
        limit,
    )
    assert result.ok and result.data["unsupported_content"] == []
    texts = result.data["text"]
    assert "�" not in "".join(texts)
    remaining = max(0, limit - 10)
    if remaining >= len('{"ok":true}'):
        assert result.data["structured_content"] == {"ok": True}
        structured = '{"ok":true}'
    else:
        assert "structured_content" not in result.data
        structured = result.data["structured_content_preview"]
        assert structured == '{"ok":true}'[:remaining]
    assert sum(len(text.encode("utf-8")) for text in texts) + len(structured) <= limit
    assert result.truncated == (limit < 21)
    if limit == 5:
        assert texts == ["abc", "", ""]
    elif limit == 6:
        assert texts == ["abc", "你", ""]
    elif limit >= 10:
        assert texts == ["abc", "你好", "z"]


async def test_binary_resource_and_link_remain_unsupported_without_fetching() -> None:
    # This client has only call_tool: URI reads must never be attempted by adaptation.
    result = await execute_result(
        CallToolResult(
            content=[
                EmbeddedResource(resource=TextResourceContents(uri="test://text", text="usable")),
                EmbeddedResource(
                    resource=BlobResourceContents(uri="test://binary", blob="cHJpdmF0ZQ==")
                ),
                ResourceLink(uri="https://example.invalid/private", name="remote"),
            ],
            structured_content={"partial": True},
        )
    )
    assert not result.ok and result.error_code == "mcp_unsupported_content"
    assert result.data["text"] == ["usable"]
    assert result.data["unsupported_content"] == ["resource", "resource_link"]
    assert result.data["structured_content"] == {"partial": True}
    assert "cHJpdmF0ZQ==" not in result.message().content
    assert "example.invalid" not in result.message().content
