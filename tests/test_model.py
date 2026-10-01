from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import httpx
import pytest
from openai import AsyncOpenAI

from nexus.app.config import ConfigError, ModelConfig, load_config
from nexus.core.model import ChatModel
from nexus.core.types import Json, Message, ModelError, ToolCall, ToolSpec


class Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, Json]] = []

    async def __call__(
        self,
        kind: str,
        data: Json,
        *,
        protocol_data: Json | None = None,
    ) -> int:
        self.events.append((kind, data))
        return len(self.events)


def chunk(delta: Json, finish: str | None = None) -> Json:
    return {
        "id": "test",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "test",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }


def sse(items: list[Json]) -> bytes:
    return ("".join(f"data: {json.dumps(c)}\n\n" for c in items) + "data: [DONE]\n\n").encode()


def model_for(handler: Any) -> ChatModel:
    config = ModelConfig("test", 32768, reasoning_effort="high")
    client = AsyncOpenAI(
        api_key="test-secret",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return ChatModel(config, client=client)


async def test_stream_tool_fragments_usage_and_private_reasoning() -> None:
    requests: list[Json] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            content=sse(
                [
                    chunk({"role": "assistant", "reasoning_content": "PRIVATE_REASONING"}),
                    chunk(
                        {
                            "content": "Checking ",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "exec_command",
                                        "arguments": '{"command":',
                                    },
                                },
                                {
                                    "index": 1,
                                    "id": "call_2",
                                    "type": "function",
                                    "function": {
                                        "name": "apply_patch",
                                        "arguments": '{"patch":"x"}',
                                    },
                                },
                            ],
                        }
                    ),
                    chunk(
                        {
                            "content": "files.",
                            "tool_calls": [
                                {"index": 0, "function": {"arguments": '"pwd"}'}},
                            ],
                        },
                        "tool_calls",
                    ),
                    {
                        "id": "test",
                        "created": 1,
                        "model": "test",
                        "object": "chat.completion.chunk",
                        "choices": [],
                        "usage": {
                            "prompt_tokens": 100,
                            "completion_tokens": 20,
                            "total_tokens": 120,
                        },
                    },
                ]
            ),
        )

    model = model_for(handle)
    emit = Recorder()
    tool = ToolSpec("exec_command", "Run shell", {"type": "object"})
    try:
        reply = await model.complete([Message("user", "inspect")], [tool], emit)
        assert reply.message.tool_calls[0].arguments_json == '{"command":"pwd"}'
        assert len(reply.message.tool_calls) == 2
        assert reply.message.content == "Checking files."
        assert reply.usage.total_tokens == 120
        assert reply.usage.source == "reported"
        assert reply.message.protocol_data is None
        assert "PRIVATE_REASONING" not in json.dumps(emit.events)
        assert requests[0]["reasoning_effort"] == "high"
        assert requests[0]["max_tokens"] == 8192
        assert "temperature" not in requests[0] and "extra_body" not in requests[0]
        assert "strict" not in requests[0]["tools"][0]["function"]
        history = [reply.message, Message("tool", "{}", tool_call_id="call_1")]
        wire = model.wire_messages(history)
        assert wire[0]["tool_calls"][0]["function"]["name"] == "exec_command"
        assert wire[1]["tool_call_id"] == "call_1"
    finally:
        await model.close()


@pytest.mark.parametrize("status,expected", [(429, 2), (500, 2), (401, 1), (400, 1)])
async def test_retry_before_delta_only(status: int, expected: int) -> None:
    attempts = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(status, json={"error": {"message": "test-secret"}})

    model = model_for(handle)
    emit = Recorder()
    try:
        with pytest.raises(ModelError) as error:
            await model.complete([Message("user", "test")], [], emit)
        assert attempts == expected
        assert "test-secret" not in str(error.value)
    finally:
        await model.close()


async def test_partial_stream_not_retried_or_returned() -> None:
    attempts = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(
            200,
            content=sse(
                [
                    chunk(
                        {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "half",
                                    "type": "function",
                                    "function": {"name": "exec_command", "arguments": "{"},
                                }
                            ]
                        }
                    ),
                ]
            ),
        )

    model = model_for(handle)
    try:
        with pytest.raises(ModelError, match="incomplete_stream"):
            await model.complete([Message("user", "test")], [], Recorder())
        assert attempts == 1
    finally:
        await model.close()


async def test_unknown_usage_and_optional_request_fields() -> None:
    requests: list[Json] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, content=sse([chunk({"content": "done"}, "stop")]))

    model = model_for(handle)
    model.config.reasoning_effort = None
    model.config.include_usage = False
    try:
        reply = await model.complete([Message("user", "test")], [], Recorder())
        assert reply.usage.total_tokens is None
        assert not {"tools", "reasoning_effort", "stream_options"} & requests[0].keys()
    finally:
        await model.close()


async def test_continuation_bound_to_service_and_model() -> None:
    model = model_for(lambda request: httpx.Response(400))
    message = Message(
        "assistant",
        tool_calls=[ToolCall("a", "exec_command", "{}")],
        protocol_data={
            "format": "chat-v1",
            "binding": model.binding,
            "fields": {"reasoning_content": "private"},
        },
    )
    try:
        assert model.wire_messages([message])[0]["reasoning_content"] == "private"
        assert "private" not in json.dumps(message.public())
        assert message.protocol_data is not None
        message.protocol_data["binding"] = "other-service"
        with pytest.raises(ModelError, match="protocol_binding"):
            model.wire_messages([message])
    finally:
        await model.close()


def test_config_env_validation_and_no_repo_config(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("NEXUS_MODEL_NAME", "overridden")
    monkeypatch.setenv("NEXUS_MODEL_BASE_URL", "https://example.com/v1")
    monkeypatch.setenv("TEST_NEXUS_KEY", "secret")
    path = tmp_path / "config.toml"
    path.write_text(
        '[model]\nname="test"\ncontext_window=32768\n'
        'api_key_env="TEST_NEXUS_KEY"\nreasoning_effort="high"\n',
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.model.name == "overridden"
    assert config.model.reasoning_effort == "high"
    assert config.model.key() == "secret"
    assert "secret" not in json.dumps(asdict(config))
    path.write_text('[model]\nname="test"\ncontext_window=8192\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="exceed"):
        load_config(path)
    path.write_text('[model]\napi_key="secret"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="unknown"):
        load_config(path)
