from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from openai import APIConnectionError, AsyncOpenAI

from nexus.app.config import ConfigError, ModelConfig, load_config
from nexus.app.events import Events
from nexus.app.session import SessionLog, read_records, replay, resume_session
from nexus.core import model as model_module
from nexus.core.agent import append_message, run_turn
from nexus.core.context import Context, ContextBuilder, estimate
from nexus.core.model import ChatModel
from nexus.core.types import (
    Json,
    Limits,
    Message,
    ModelError,
    RuntimeEvent,
    Session,
    Tool,
    ToolCall,
    ToolResult,
    ToolSpec,
)


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
        assert reply.message.protocol_data == {
            "format": "chat-v1",
            "binding": model.binding,
            "fields": {"reasoning_content": "PRIVATE_REASONING"},
        }
        assert "PRIVATE_REASONING" not in json.dumps(reply.message.public())
        assert "PRIVATE_REASONING" not in json.dumps(emit.events)
        assert requests[0]["reasoning_effort"] == "high"
        assert requests[0]["max_tokens"] == 8192
        assert set(requests[0]) == {
            "model",
            "messages",
            "stream",
            "n",
            "max_tokens",
            "tools",
            "reasoning_effort",
            "stream_options",
        }
        assert requests[0]["stream"] is True and requests[0]["n"] == 1
        assert requests[0]["stream_options"] == {"include_usage": True}
        assert "strict" not in requests[0]["tools"][0]["function"]
        history = [
            reply.message,
            Message("tool", "{}", tool_call_id="call_1"),
            Message("tool", "{}", tool_call_id="call_2"),
        ]
        wire = model.wire_messages(history)
        assert wire[0]["tool_calls"][0]["function"]["name"] == "exec_command"
        assert wire[0]["reasoning_content"] == "PRIVATE_REASONING"
        assert wire[1]["tool_call_id"] == "call_1"
    finally:
        await model.close()


async def test_fragmented_reasoning_session_resume_and_next_request(tmp_path: Path) -> None:
    requests: list[Json] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        if len(requests) == 2:
            return httpx.Response(200, content=sse([chunk({"content": "done"}, "stop")]))
        return httpx.Response(
            200,
            content=sse(
                [
                    chunk({"reasoning_content": "PRIVATE_"}),
                    chunk({"reasoning_content": ""}),
                    chunk({"reasoning_content": "REASONING"}),
                    chunk(
                        {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "exec_command",
                                        "arguments": '{"command":"pwd"}',
                                    },
                                }
                            ]
                        },
                        "tool_calls",
                    ),
                ]
            ),
        )

    model = model_for(handle)
    session = Session(tmp_path, run_id="run")
    writer = SessionLog.create(session, "test", {}, tmp_path)
    public: list[RuntimeEvent] = []

    async def consumer(event: RuntimeEvent) -> None:
        public.append(event)

    events = Events(session, writer, consumer)
    try:
        await events("run_started", {})
        await append_message(session, Message("user", "inspect"), events)
        reply = await model.complete(session.messages, [], events)
        expected = {
            "format": "chat-v1",
            "binding": model.binding,
            "fields": {"reasoning_content": "PRIVATE_REASONING"},
        }
        assert reply.message.protocol_data == expected
        assert reply.message.content == ""
        assert not any(event.kind == "assistant_delta" for event in public)
        await append_message(session, reply.message, events)
        await append_message(
            session, ToolResult("call_1", True, {"stdout": "output " * 2000}).message(), events
        )
        records, _ = read_records(writer.stream)
        assert "PRIVATE_REASONING" not in json.dumps([r["data"] for r in records])
        assert [r["protocol_data"] for r in records if "protocol_data" in r] == [expected]
        path = writer.path
        writer.close()
        restored, writer, warnings = resume_session(path, tmp_path, tmp_path)
        events = Events(restored, writer, consumer)
        assert not warnings
        assert restored.resume_run_id == "run"
        logical = ContextBuilder().build_active_context(restored, "run")
        assistant = next(m for m in logical if m.role == "assistant")
        assert assistant.protocol_data == expected
        assert "PRIVATE_REASONING" not in json.dumps(assistant.public())
        assert estimate([assistant], []) > estimate([replace(assistant, protocol_data=None)], [])
        context = Context(Limits(context_window=32768))
        projected = context.project(restored, logical).messages
        final = await model.complete(projected, [], events)
        wire = requests[1]["messages"]
        assert wire[1]["reasoning_content"] == "PRIVATE_REASONING"
        assert wire[1]["tool_calls"][0]["id"] == wire[2]["tool_call_id"] == "call_1"
        assert final.message.content == "done" and final.message.protocol_data is None
        await append_message(restored, final.message, events)
        # Once consumed and outside W, the observation shrinks but its assistant stays intact.
        logical = ContextBuilder().build_active_context(restored, "run")
        cold_projection = Context(Limits(context_window=4096, max_output_tokens=512)).project(
            restored, logical
        )
        assert cold_projection.diagnostics["cold_compacted_count"] == 1
        assert cold_projection.messages[1] is assistant
        assert cold_projection.messages[1].protocol_data == expected
        assert "PRIVATE_REASONING" not in json.dumps([asdict(event) for event in public])
    finally:
        writer.close()
        await model.close()


async def test_safety_snapshot_keeps_continuation_private(tmp_path: Path) -> None:
    requests: list[Json] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            content=sse(
                [
                    chunk({"reasoning_content": "PRIVATE_REASONING"}),
                    chunk({"content": "Inspected code; work remains."}, "stop"),
                ]
            ),
        )

    model = model_for(handle)
    session = Session(tmp_path, run_id="run")
    writer = SessionLog.create(session, "test", {}, tmp_path)
    public: list[RuntimeEvent] = []

    async def consumer(event: RuntimeEvent) -> None:
        public.append(event)

    events = Events(session, writer, consumer)
    continuation = {
        "format": "chat-v1",
        "binding": model.binding,
        "fields": {"reasoning_content": "PRIVATE_REASONING"},
    }
    try:
        await events("run_started", {})
        await append_message(session, Message("user", "inspect"), events)
        for index in range(3):
            ident = f"call_{index}"
            await append_message(
                session,
                Message(
                    "assistant",
                    "observed code " * (1000 if index == 0 else 1),
                    [ToolCall(ident, "exec_command", "{}")],
                    protocol_data=continuation,
                ),
                events,
            )
            await append_message(session, ToolResult(ident, True, {}).message(), events)
        context = Context(Limits(context_window=8192, max_output_tokens=512))
        builder = ContextBuilder()
        projected = context.project(session, builder.build_active_context(session, "run"))
        assert await context.prepare(session, projected.messages, [], model, events, force=True)
        assert requests[0]["messages"][1]["reasoning_content"] == "PRIVATE_REASONING"
        active = context.project(session, builder.build_active_context(session, "run")).messages
        assistants = [m for m in active if m.role == "assistant"]
        assert len(assistants) == 2
        assert all(m.protocol_data == continuation for m in assistants)
        assert all("PRIVATE_REASONING" not in m.content for m in active)
        assert all(m.protocol_data is None for m in active if m.role == "user")
        records, _ = read_records(writer.stream)
        assert "PRIVATE_REASONING" not in json.dumps([r["data"] for r in records])
        assert "PRIVATE_REASONING" not in json.dumps([asdict(event) for event in public])
        restored = replay(records, tmp_path)
        restored_context = builder.build_active_context(restored, "run")
        assert context.project(restored, restored_context).messages == active
    finally:
        writer.close()
        await model.close()


@pytest.mark.parametrize(
    "malformed", [123, False, [], {}, ["PRIVATE_REASONING"], {"text": "PRIVATE_REASONING"}]
)
async def test_malformed_reasoning_fails_privately(malformed: Any) -> None:
    model = model_for(
        lambda request: httpx.Response(
            200,
            content=sse(
                [
                    chunk({"reasoning_content": "PRIVATE_REASONING"}),
                    chunk({"reasoning_content": malformed}, "stop"),
                ]
            ),
        )
    )
    emit = Recorder()
    try:
        with pytest.raises(ModelError, match="^invalid_reasoning_content$"):
            await model.complete([Message("user", "test")], [], emit)
        assert "PRIVATE_REASONING" not in json.dumps(emit.events)
        assert len([e for e in emit.events if e[0] == "model_started"]) == 1
    finally:
        await model.close()


async def test_reasoning_after_finish_is_rejected() -> None:
    model = model_for(
        lambda request: httpx.Response(
            200,
            content=sse(
                [
                    chunk({"content": "done"}, "stop"),
                    chunk({"reasoning_content": "PRIVATE_REASONING"}),
                ]
            ),
        )
    )
    emit = Recorder()
    try:
        with pytest.raises(ModelError, match="delta_after_finish"):
            await model.complete([Message("user", "test")], [], emit)
        assert "PRIVATE_REASONING" not in json.dumps(emit.events)
    finally:
        await model.close()


@pytest.mark.parametrize("status,expected", [(429, 2), (500, 2), (401, 1), (400, 1)])
async def test_retry_before_delta_only(status: int, expected: int, monkeypatch: Any) -> None:
    ticks = iter([10.0, 11.25, 20.0, 22.5])
    monkeypatch.setattr(model_module, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
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
        finishes = [data for kind, data in emit.events if kind == "model_finished"]
        assert [data["duration_ms"] for data in finishes] == [1250, 2500][:expected]
        assert [data["error"] for data in finishes] == [f"model_http_{status}"] * expected
    finally:
        await model.close()


async def test_partial_stream_not_retried_or_returned(monkeypatch: Any) -> None:
    ticks = iter([10.0, 11.25])
    monkeypatch.setattr(model_module, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
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
    emit = Recorder()
    try:
        with pytest.raises(ModelError, match="incomplete_stream"):
            await model.complete([Message("user", "test")], [], emit)
        assert attempts == 1
        assert emit.events[-1] == (
            "model_finished",
            {"error": "stream_interrupted", "attempt": 1, "duration_ms": 1250},
        )
    finally:
        await model.close()


@pytest.mark.parametrize("empty_text", ["", " \n\t"])
async def test_complete_empty_stop_retries_without_replaying_tools(
    tmp_path: Path, empty_text: str
) -> None:
    requests: list[Json] = []
    executions: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            items = [chunk({"content": empty_text, "reasoning_content": "PRIVATE"}, "stop")]
        elif len(requests) == 2:
            items = [
                chunk(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "inspect", "arguments": "{}"},
                            }
                        ]
                    },
                    "tool_calls",
                )
            ]
        else:
            items = [chunk({"content": "done"}, "stop")]
        items.append(
            {
                "id": "test",
                "created": 1,
                "model": "test",
                "object": "chat.completion.chunk",
                "choices": [],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
            }
        )
        return httpx.Response(200, content=sse(items))

    async def execute(arguments: Any, context: Any, emit: Any) -> ToolResult:
        executions.append(context.call_id)
        return ToolResult(context.call_id, True, {})

    model, emit = model_for(handle), Recorder()
    session = Session(tmp_path)
    tool = Tool(ToolSpec("inspect", "Inspect", {"type": "object"}), execute)
    try:
        result = await run_turn(session, "inspect", model, {"inspect": tool}, emit, Limits())
        assert result.outcome == "completed"
        assert result.steps == 2 and result.model_calls == 3 and result.tool_calls == 1
        assert result.usage.total_tokens == 360
        assert executions == ["call_1"]
        assert requests[0] == requests[1]
        assert "PRIVATE" not in json.dumps(requests)
        assert [m.content for m in session.messages if m.role == "assistant"] == ["", "done"]
        assert all(m.protocol_data is None for m in session.messages)
        finishes = [data for kind, data in emit.events if kind == "model_finished"]
        assert [d["attempt"] for d in finishes] == [1, 2, 1]
        assert finishes[0]["error"] == "empty_or_invalid_final"
        assert sum(d["usage"]["total_tokens"] for d in finishes) == 360
        assert "PRIVATE" not in json.dumps(emit.events)
    finally:
        await model.close()


@pytest.mark.parametrize("first_status", [200, 500])
async def test_empty_stop_shares_retry_limit_and_remains_failed(
    tmp_path: Path, first_status: int
) -> None:
    requests: list[Json] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        if len(requests) == 1 and first_status != 200:
            return httpx.Response(first_status, json={"error": {"message": "unavailable"}})
        return httpx.Response(200, content=sse([chunk({"reasoning_content": "PRIVATE"}, "stop")]))

    model, emit = model_for(handle), Recorder()
    session = Session(tmp_path)
    try:
        result = await run_turn(session, "inspect", model, {}, emit, Limits())
        assert result.outcome == "failed" and result.reason == "empty_or_invalid_final"
        assert result.steps == 1 and result.model_calls == 2 and result.tool_calls == 0
        assert len(requests) == 2 and requests[0] == requests[1]
        assert not any(m.role == "assistant" for m in session.messages)
        finishes = [data for kind, data in emit.events if kind == "model_finished"]
        assert len(finishes) == 2
        assert finishes[-1]["error"] == "empty_or_invalid_final"
        assert finishes[-1]["usage"]["source"] == "unknown"
        assert "PRIVATE" not in json.dumps(emit.events)
    finally:
        await model.close()


@pytest.mark.parametrize("failure", ["connection", "timeout", "model", "cancel"])
async def test_failed_request_duration_preserves_retries_and_errors(
    failure: str, monkeypatch: Any
) -> None:
    ticks = iter([10.0, 11.25, 20.0, 22.5])
    monkeypatch.setattr(model_module, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    requests: list[Json] = []

    async def create(**request: Any) -> None:
        requests.append(request)
        if failure == "connection":
            raise APIConnectionError(request=httpx.Request("POST", "https://example.invalid"))
        if failure == "timeout":
            raise TimeoutError("private exception detail")
        if failure == "model":
            raise ModelError("incomplete_stream")
        raise asyncio.CancelledError()

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    model = ChatModel(ModelConfig("test", 32768), client=client)
    emit = Recorder()
    with pytest.raises(asyncio.CancelledError if failure == "cancel" else ModelError) as caught:
        await model.complete([Message("user", "test")], [], emit)
    retryable = failure in {"connection", "timeout"}
    expected = 2 if retryable else 1
    assert len(requests) == expected
    if retryable:
        assert requests[0] == requests[1]
        assert str(caught.value) == "model_transport"
    elif failure == "model":
        assert str(caught.value) == "incomplete_stream"
    starts = [data for kind, data in emit.events if kind == "model_started"]
    finishes = [data for kind, data in emit.events if kind == "model_finished"]
    assert len(starts) == len(finishes) == expected
    assert [data["duration_ms"] for data in finishes] == [1250, 2500][:expected]
    assert [data["attempt"] for data in finishes] == list(range(1, expected + 1))
    assert {data["error"] for data in finishes} == {
        "model_transport" if retryable else "stream_interrupted"
    }
    assert "private exception detail" not in json.dumps(emit.events)


@pytest.mark.parametrize("extra", [{}, {"reasoning_content": None}, {"reasoning_content": ""}])
async def test_unknown_usage_and_optional_request_fields(extra: Json) -> None:
    requests: list[Json] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, content=sse([chunk({"content": "done", **extra}, "stop")]))

    model = model_for(handle)
    model.config.reasoning_effort = None
    model.config.include_usage = False
    try:
        reply = await model.complete([Message("user", "test")], [], Recorder())
        assert reply.message.content == "done"
        assert reply.message.protocol_data is None
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


async def test_request_timeout_applies_to_sdk_and_whole_stream(monkeypatch: Any) -> None:
    deadline = asyncio.timeout
    observed: list[float | None] = []

    def timed(seconds: float | None) -> Any:
        observed.append(seconds)
        return deadline(seconds)

    def client(**kwargs: Any) -> AsyncOpenAI:
        return AsyncOpenAI(
            **kwargs,
            http_client=httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(
                        200, content=sse([chunk({"content": "done"}, "stop")])
                    )
                )
            ),
        )

    monkeypatch.setenv("TEST_MODEL_KEY", "test-secret")
    monkeypatch.setattr(model_module, "AsyncOpenAI", client)
    monkeypatch.setattr(asyncio, "timeout", timed)
    model = ChatModel(
        ModelConfig("test", 32768, api_key_env="TEST_MODEL_KEY", request_timeout_seconds=300)
    )
    try:
        assert model.client.timeout == 300
        reply = await model.complete([Message("user", "test")], [], Recorder())
        assert reply.message.content == "done"
        assert observed == [300]
    finally:
        await model.close()


@pytest.mark.parametrize("value", ["0", "-1", "601", "true", '"300"', "1.5"])
def test_invalid_request_timeout_config(tmp_path: Path, value: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[model]\nname="test"\ncontext_window=32768\nrequest_timeout_seconds=' + value,
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="model.request_timeout_seconds"):
        load_config(path, require_key=False)


@pytest.mark.parametrize("value,expected", [("", 120), ("1", 1), ("300", 300), ("600", 600)])
def test_request_timeout_config_default_and_bounds(
    tmp_path: Path, value: str, expected: int
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[model]\nname="test"\ncontext_window=32768\n'
        + ("request_timeout_seconds=" + value if value else ""),
        encoding="utf-8",
    )
    assert load_config(path, require_key=False).model.request_timeout_seconds == expected


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


@pytest.mark.parametrize("parameter", [None, "max_tokens", "max_completion_tokens"])
async def test_output_limit_config_reaches_wire_and_context(
    tmp_path: Path, parameter: str | None
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[model]\nname="test"\ncontext_window=32768\nmax_output_tokens=1234\n'
        + (f'output_token_parameter="{parameter}"\n' if parameter else ""),
        encoding="utf-8",
    )
    config = load_config(path, require_key=False)
    selected = parameter or "max_tokens"
    assert config.model.output_token_parameter == selected
    assert Context(config.limits).budget == 32768 - 1234 - 1024
    requests: list[Json] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, content=sse([chunk({"content": "done"}, "stop")]))

    model = model_for(handle)
    model.config = config.model
    try:
        await model.complete([Message("user", "task")], [], Recorder())
        request = requests[0]
        assert request[selected] == config.limits.max_output_tokens == 1234
        other = "max_completion_tokens" if selected == "max_tokens" else "max_tokens"
        assert other not in request
        assert "extra_body" not in request
    finally:
        await model.close()


@pytest.mark.parametrize(
    "value", ['"unknown"', '""', '"MAX_TOKENS"', '" max_tokens "', "1", "true"]
)
def test_invalid_output_token_parameter(tmp_path: Path, value: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[model]\nname="test"\ncontext_window=32768\noutput_token_parameter=' + value,
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="model.output_token_parameter"):
        load_config(path, require_key=False)
