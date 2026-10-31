from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from openai import AsyncOpenAI

from nexus.app.bootstrap import Conversation
from nexus.app.config import Config, ConfigError, ModelConfig, load_config
from nexus.core.model import ChatModel, CompactionModel
from nexus.core.types import Json, Limits, Message, ModelError, ToolCall, ToolSpec
from tests.test_model import Recorder, chunk, sse


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch) -> list[Json]:
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    monkeypatch.setenv("TEST_LITE_KEY", "first-key")
    from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler

    calls: list[Json] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(
            {"body": json.loads(request.content), "auth": request.headers.get("authorization")}
        )
        return responses.pop(0)

    responses: list[httpx.Response] = []

    def client(**kwargs: Any) -> AsyncOpenAI:
        return AsyncOpenAI(
            **kwargs, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        )

    monkeypatch.setattr("nexus.core.transport.AsyncOpenAI", client)
    monkeypatch.setattr(
        AsyncHTTPHandler,
        "create_client",
        lambda *a, **kw: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    # Attach the fixture queue without changing production interfaces.
    wire.responses = responses  # type: ignore[attr-defined]
    return calls


def response(items: list[Json]) -> None:
    wire.responses.append(httpx.Response(200, content=sse(items)))  # type: ignore[attr-defined]


def config(provider: str) -> ModelConfig:
    return ModelConfig(
        "deepseek-v4-pro" if provider == "deepseek" else "qwen3.8-flash",
        32768,
        "https://api.deepseek.com/v1"
        if provider == "deepseek"
        else "https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key_env="TEST_LITE_KEY",
        provider=provider,
    )


@pytest.mark.parametrize("provider", ["openai", "deepseek", "dashscope"])
async def test_real_sdk_text_and_unknown_usage(wire: list[Json], provider: str) -> None:
    response([chunk({"content": "hello"}), chunk({}, "stop")])
    model = ChatModel(config(provider))
    try:
        reply = await model.complete([Message("user", "hello")], [], Recorder())
        assert reply.message.content == "hello"
        assert reply.usage.input_tokens is None
        assert len(wire) == 1 and wire[0]["auth"] == "Bearer first-key"
    finally:
        await model.close()


@pytest.mark.parametrize("provider", ["deepseek", "dashscope"])
async def test_sdk_tools_reasoning_continuation_and_usage(wire: list[Json], provider: str) -> None:
    response(
        [
            chunk({"reasoning_content": "private reasoning"}),
            chunk(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "probe", "arguments": "{"},
                        }
                    ]
                }
            ),
            chunk({"tool_calls": [{"index": 0, "function": {"arguments": "}"}}]}, "tool_calls"),
            {
                "id": "test",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "test",
                "choices": [],
                "usage": {
                    "prompt_tokens": 20,
                    "completion_tokens": 3,
                    "total_tokens": 23,
                    "prompt_tokens_details": {"cached_tokens": 10},
                },
            },
        ]
    )
    response([chunk({"content": "done"}, "stop")])
    model = ChatModel(config(provider))
    recorder = Recorder()
    try:
        first = await model.complete(
            [Message("user", "probe")],
            [ToolSpec("probe", "probe", {"type": "object", "properties": {}})],
            recorder,
        )
        assert first.message.tool_calls == [ToolCall("c1", "probe", "{}")]
        assert first.usage.input_tokens == 20 and first.usage.cached_input_tokens == 10
        assert first.message.protocol_data is not None
        await model.complete(
            [Message("user", "probe"), first.message, Message("tool", "ok", tool_call_id="c1")],
            [],
            recorder,
        )
        assert wire[1]["body"]["messages"][1]["reasoning_content"] == "private reasoning"
        assert wire[1]["body"]["messages"][2]["tool_call_id"] == "c1"
        starts = [d for k, d in recorder.events if k == "model_started"]
        assert len(starts) == len(wire) == 2
        assert starts[0]["request_id"] != starts[1]["request_id"]
    finally:
        await model.close()


async def test_sdk_retry_only_nexus_and_missing_finish(wire: list[Json]) -> None:
    wire_fixture = globals()["wire"]
    wire_fixture.responses.append(httpx.Response(429, json={"error": {"message": "limited"}}))
    response([chunk({"content": "done"}, "stop")])
    model = ChatModel(config("deepseek"))
    recorder = Recorder()
    try:
        assert (
            await model.complete([Message("user", "hello")], [], recorder)
        ).message.content == "done"
        assert len(wire) == 2
        starts = [d for k, d in recorder.events if k == "model_started"]
        assert starts[0]["request_id"] == starts[1]["request_id"]
        response([chunk({"content": "partial"})])
        with pytest.raises(ModelError, match="incomplete_stream"):
            await model.complete([Message("user", "hello")], [], Recorder())
        assert len(wire) == 3  # no whole-response replay after visible output
    finally:
        await model.close()


async def test_switch_keeps_history_and_isolates_keys(
    tmp_path: Path, wire: list[Json], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SECOND_LITE_KEY", "second-key")
    first = config("deepseek")
    second = replace(config("dashscope"), api_key_env="SECOND_LITE_KEY")
    cfg = Config(first, Limits(), models={"qwen": second}, memory_enabled=False)

    async def consume(event: Any) -> None:
        pass

    c = Conversation(tmp_path, cfg, consume, home=tmp_path / "state", registry={})
    c.model = ChatModel(first)
    old = c.model
    c.session.messages = [
        Message(
            "assistant",
            "public",
            protocol_data={
                "format": "chat-v1",
                "binding": old.binding,
                "fields": {"reasoning_content": "PRIVATE"},
            },
        ),
        Message("tool", "public tool result", tool_call_id="c1"),
    ]
    try:
        with pytest.raises(ConfigError):
            await c.select_model("missing")
        assert c.model is old
        await c.select_model("qwen")
        assert c.model is not None
        assert c.session.messages[1].content == "public tool result"
        assert "PRIVATE" not in json.dumps(c.model.wire_messages(c.session.messages))
        response([chunk({"content": "done"}, "stop")])
        await c.model.complete([Message("user", "hello")], [], Recorder())
        assert wire[0]["auth"] == "Bearer second-key"
        async with c.turn_lock:
            with pytest.raises(ConfigError, match="idle"):
                await c.select_model("default")
    finally:
        await c.close()


def test_model_alias_config(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[model]\nname="legacy"\n[models.qwen]\nname="qwen3.8-flash"\n'
        'provider="dashscope"\napi_key_env="DASHSCOPE_API_KEY"\n',
        encoding="utf-8",
    )
    cfg = load_config(path, require_key=False)
    assert cfg.model.provider == "openai"
    assert cfg.models["qwen"].name == "qwen3.8-flash"


async def test_sdk_compaction_structured(wire: list[Json]) -> None:
    from nexus.core.compaction import COMPACTION_PROMPT

    response([chunk({"content": "{}"}, "stop")])
    model = CompactionModel(config("dashscope"))
    recorder = Recorder()
    try:
        await model.complete(
            [Message("system", COMPACTION_PROMPT), Message("user", "history")], [], recorder
        )
        assert wire[0]["body"]["response_format"] == {"type": "json_object"}
        assert all(
            d["purpose"] == "compaction" for k, d in recorder.events if k.startswith("model_")
        )
    finally:
        await model.close()


async def test_sdk_empty_final_repairs_once(wire: list[Json]) -> None:
    response([chunk({}, "stop")])
    response([chunk({"content": "fixed"}, "stop")])
    model = ChatModel(config("deepseek"))
    try:
        assert (
            await model.complete([Message("user", "hi")], [], Recorder())
        ).message.content == "fixed"
        assert len(wire) == 2
    finally:
        await model.close()


@pytest.mark.parametrize("mode", ["error", "cancel", "timeout"])
async def test_sdk_partial_stream_failure_never_replays(wire: list[Json], mode: str) -> None:
    import asyncio

    class Body(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self) -> Any:
            yield sse([chunk({"content": "partial"})]).replace(b"data: [DONE]\n\n", b"")
            if mode == "error":
                raise httpx.ReadError("private error")
            if mode == "cancel":
                raise asyncio.CancelledError
            await asyncio.sleep(10)

        async def aclose(self) -> None:
            self.closed = True

    body = Body()
    globals()["wire"].responses.append(httpx.Response(200, stream=body))
    model = ChatModel(replace(config("deepseek"), request_timeout_seconds=1))
    recorder = Recorder()
    try:
        with pytest.raises(asyncio.CancelledError if mode == "cancel" else ModelError):
            await model.complete([Message("user", "hi")], [], recorder)
        assert len(wire) == 1
        assert body.closed
        finished = [d for k, d in recorder.events if k == "model_finished"]
        assert len(finished) == 1 and finished[0]["error"]
    finally:
        await model.close()


async def test_sdk_gzip_sse_reports_real_usage(wire: list[Json]) -> None:
    import gzip

    payload = sse(
        [
            chunk({"content": "compressed"}, "stop"),
            {
                "id": "a",
                "created": 1,
                "model": "test",
                "choices": [],
                "usage": {"prompt_tokens": 9, "completion_tokens": 2, "total_tokens": 11},
            },
        ]
    )

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self) -> Any:
            encoded = gzip.compress(payload)
            yield encoded[:12]
            yield encoded[12:]

    globals()["wire"].responses.append(
        httpx.Response(200, headers={"content-encoding": "gzip"}, stream=Body())
    )
    model = ChatModel(config("deepseek"))
    try:
        reply = await model.complete([Message("user", "hello")], [], Recorder())
        assert reply.message.content == "compressed"
        assert reply.usage.input_tokens == 9
    finally:
        await model.close()
