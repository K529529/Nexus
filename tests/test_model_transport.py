"""Deterministic transport faults: no provider calls, actual adapter/loop/events."""

from __future__ import annotations

import asyncio
import io
import json
from collections.abc import AsyncIterator
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from openai import APIConnectionError, APIStatusError, APITimeoutError
from openai.types.chat import ChatCompletionChunk
from rich.console import Console

from nexus.app.config import ModelConfig
from nexus.app.events import Events
from nexus.app.profile import RunProfiler, profile_metrics
from nexus.app.session import SessionLog, read_records, replay, resume_session
from nexus.app.tui import Transcript
from nexus.core import model as model_module
from nexus.core.agent import run_turn
from nexus.core.model import ChatModel
from nexus.core.types import Json, Limits, Message, ModelError, RuntimeEvent, Session
from tests.test_model import Recorder, chunk, model_for, sse

SECRET = "https://secret.invalid/?api_key=DO_NOT_LOG"


class Stream:
    def __init__(self, items: list[Json | BaseException], close: str = "ok") -> None:
        self.items, self.close_behavior = items, close
        self.closes = 0

    async def __aiter__(self) -> AsyncIterator[ChatCompletionChunk]:
        for item in self.items:
            if isinstance(item, BaseException):
                raise item
            yield ChatCompletionChunk.model_validate(item)

    async def close(self) -> None:
        self.closes += 1
        if self.close_behavior == "hang":
            await asyncio.Event().wait()
        if self.close_behavior == "transport":
            raise httpx.RemoteProtocolError(SECRET)
        if self.close_behavior == "bug":
            raise ValueError(SECRET)
        if self.close_behavior == "cancel":
            raise asyncio.CancelledError()


class Client:
    def __init__(self, attempts: list[Stream | BaseException], close: str = "ok") -> None:
        self.attempts, self.close_behavior = attempts, close
        self.requests: list[Json] = []
        self.closes = 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def create(self, **request: Any) -> Stream:
        self.requests.append(deepcopy(request))
        item = self.attempts[len(self.requests) - 1]
        if isinstance(item, BaseException):
            raise item
        return item

    async def close(self) -> None:
        self.closes += 1
        if self.close_behavior == "hang":
            await asyncio.Event().wait()
        if self.close_behavior == "error":
            raise httpx.ReadError(SECRET)


def success(close: str = "ok") -> Stream:
    return Stream([chunk({"content": "COMPLETE"}, "stop")], close)


def adapter(client: Client) -> ChatModel:
    return ChatModel(ModelConfig("test", 32768), client=client)


def finished(emit: Recorder, attempts: int) -> list[Json]:
    starts = [d for k, d in emit.events if k == "model_started"]
    ends = [d for k, d in emit.events if k == "model_finished"]
    assert len(starts) == len(ends) == attempts
    assert [d["attempt"] for d in starts] == [d["attempt"] for d in ends]
    assert all(d["duration_ms"] >= 0 for d in ends)
    assert SECRET not in json.dumps(emit.events)
    return ends


@pytest.mark.parametrize("phase", ["create", "stream"])
@pytest.mark.parametrize("error", ["remote", "sdk", "timeout", "read", "proxy"])
async def test_transient_before_output_retries_once(phase: str, error: str) -> None:
    errors = {
        "remote": httpx.RemoteProtocolError(SECRET),
        "sdk": APIConnectionError(request=httpx.Request("POST", SECRET)),
        "timeout": APITimeoutError(request=httpx.Request("POST", SECRET)),
        "read": httpx.ReadError(SECRET),
        "proxy": httpx.ProxyError(SECRET),
    }
    first = errors[error]
    stream = Stream([chunk({"role": "assistant"}), first])
    client = Client([first if phase == "create" else stream, success()])
    emit = Recorder()
    messages = [Message("user", "task")]
    before = deepcopy(messages)
    reply = await adapter(client).complete(messages, [], emit)
    assert reply.message.content == "COMPLETE" and messages == before
    assert len(client.requests) == 2 and client.requests[0] == client.requests[1]
    ends = finished(emit, 2)
    assert ends[0]["error"] == "model_transport" and ends[0]["phase"] == phase
    assert ends[0]["retry"] and not ends[0]["seen_delta"]
    assert ends[0]["failure_kind"] == ("timeout" if error == "timeout" else "transport")
    assert "error" not in ends[1] and not ends[1]["retry"]
    assert stream.closes == (1 if phase == "stream" else 0)


@pytest.mark.parametrize(
    "delta",
    [
        {"content": "PARTIAL"},
        {"reasoning_content": "PRIVATE_REASONING"},
        {
            "tool_calls": [
                {
                    "index": 0,
                    "id": "half",
                    "type": "function",
                    "function": {"name": "exec_command", "arguments": '{"command":'},
                }
            ]
        },
    ],
)
async def test_partial_output_never_retried(delta: Json) -> None:
    stream = Stream([chunk(delta), httpx.RemoteProtocolError(SECRET)])
    client, emit = Client([stream]), Recorder()
    with pytest.raises(ModelError, match="model_transport"):
        await adapter(client).complete([Message("user", "task")], [], emit)
    end = finished(emit, 1)[0]
    assert end["seen_delta"] and not end["retry"] and end["phase"] == "stream"
    assert len(client.requests) == 1 and stream.closes == 1
    assert "PRIVATE_REASONING" not in json.dumps(emit.events)


async def test_two_failures_are_two_closed_attempts() -> None:
    client = Client([httpx.RemoteProtocolError(SECRET), httpx.RemoteProtocolError(SECRET)])
    emit = Recorder()
    with pytest.raises(ModelError, match="model_transport"):
        await adapter(client).complete([], [], emit)
    assert [d["retry"] for d in finished(emit, 2)] == [True, False]


@pytest.mark.parametrize("error", [ValueError(SECRET), httpx.LocalProtocolError(SECRET)])
async def test_programming_or_local_protocol_errors_not_transport(error: Exception) -> None:
    emit, client = Recorder(), Client([error])
    with pytest.raises(type(error)) as caught:
        await adapter(client).complete([], [], emit)
    assert caught.value is error
    end = finished(emit, 1)[0]
    assert end["error"] == "runtime_error" and not end["retry"]


@pytest.mark.parametrize("status", [400, 401, 429, 500, 503])
async def test_http_status_semantics(status: int) -> None:
    response = httpx.Response(status, request=httpx.Request("POST", SECRET))
    error = APIStatusError(SECRET, response=response, body={})
    count = 2 if status == 429 or status >= 500 else 1
    emit, client = Recorder(), Client([error] * count)
    with pytest.raises(ModelError, match=f"model_http_{status}"):
        await adapter(client).complete([], [], emit)
    assert all(d["failure_kind"] == "http" for d in finished(emit, count))


async def test_context_overflow_never_retried() -> None:
    response = httpx.Response(400, request=httpx.Request("POST", SECRET))
    error = APIStatusError(
        SECRET, response=response, body={"error": {"code": "context_length_exceeded"}}
    )
    emit = Recorder()
    with pytest.raises(ModelError, match="context_limit"):
        await adapter(Client([error])).complete([], [], emit)
    assert finished(emit, 1)[0]["error"] == "context_limit"


@pytest.mark.parametrize("close", ["transport", "hang"])
async def test_valid_reply_survives_recoverable_cleanup(close: str, monkeypatch: Any) -> None:
    monkeypatch.setattr(model_module, "CLEANUP_TIMEOUT_SECONDS", 0.01)
    stream, emit = success(close), Recorder()
    client = Client([stream])
    model = adapter(client)
    reply = await model.complete([], [], emit)
    assert reply.message.content == "COMPLETE"
    end = finished(emit, 1)[0]
    assert "error" not in end and not end["retry"]
    assert end["cleanup_warning"]["recovery"] == "client_closed"
    assert client.closes == 1
    with pytest.raises(ModelError, match="model_client_closed"):
        await model.complete([], [], emit)
    finished(emit, 1)  # Closed injected clients are never silently reused.


async def test_owned_client_replaced_after_cleanup(monkeypatch: Any) -> None:
    clients = [Client([success("transport")]), Client([success()])]
    monkeypatch.setattr(model_module, "AsyncOpenAI", lambda **kwargs: clients.pop(0))
    monkeypatch.setenv("TRANSPORT_TEST_KEY", "fake")
    model = ChatModel(ModelConfig("test", 32768, api_key_env="TRANSPORT_TEST_KEY"))
    first = model.client
    emit = Recorder()
    assert (await model.complete([], [], emit)).message.content == "COMPLETE"
    assert (await model.complete([], [], emit)).message.content == "COMPLETE"
    assert first.closes == 1 and model.client is not first
    finished(emit, 2)


@pytest.mark.parametrize("client_close", ["ok", "error", "hang"])
async def test_cleanup_does_not_replace_read_error(client_close: str, monkeypatch: Any) -> None:
    monkeypatch.setattr(model_module, "CLEANUP_TIMEOUT_SECONDS", 0.01)
    stream = Stream([chunk({"content": "PARTIAL"}), httpx.ReadError(SECRET)], "transport")
    client, emit = Client([stream], client_close), Recorder()
    model = adapter(client)
    with pytest.raises(ModelError, match="model_transport"):
        await model.complete([], [], emit)
    end = finished(emit, 1)[0]
    assert end["exception_type"] == "ReadError" and end["phase"] == "stream"
    assert end["cleanup_warning"]["exception_type"] == "RemoteProtocolError"
    assert not end["retry"]
    if client_close != "ok":
        assert end["cleanup_warning"]["recovery"] == "failed"
        with pytest.raises(ModelError, match="model_cleanup_incomplete"):
            await model.complete([], [], emit)


async def test_unconfirmed_cleanup_fails_closed() -> None:
    client, emit = Client([success("transport")], "error"), Recorder()
    with pytest.raises(ModelError, match="model_cleanup_incomplete"):
        await adapter(client).complete([], [], emit)
    end = finished(emit, 1)[0]
    assert end["phase"] == "close" and not end["retry"]


async def test_cleanup_programming_error_not_suppressed() -> None:
    emit = Recorder()
    with pytest.raises(ValueError):
        await adapter(Client([success("bug")])).complete([], [], emit)
    assert finished(emit, 1)[0]["failure_kind"] == "cleanup"


@pytest.mark.parametrize("phase", ["create", "stream", "close"])
async def test_cancellation_is_not_retryable(phase: str, monkeypatch: Any) -> None:
    monkeypatch.setattr(model_module, "CLEANUP_TIMEOUT_SECONDS", 0.01)
    item: Stream | BaseException = asyncio.CancelledError()
    if phase == "stream":
        item = Stream([asyncio.CancelledError()], "hang")
    elif phase == "close":
        item = success("cancel")
    client, emit = Client([item]), Recorder()
    with pytest.raises(asyncio.CancelledError):
        await adapter(client).complete([], [], emit)
    end = finished(emit, 1)[0]
    assert end["error"] == "cancelled" and end["phase"] == phase and not end["retry"]


async def test_real_task_cancel_bounds_cleanup(monkeypatch: Any) -> None:
    monkeypatch.setattr(model_module, "CLEANUP_TIMEOUT_SECONDS", 0.01)
    entered = asyncio.Event()

    class WaitingStream(Stream):
        async def __aiter__(self) -> AsyncIterator[ChatCompletionChunk]:
            entered.set()
            await asyncio.Event().wait()
            yield ChatCompletionChunk.model_validate(chunk({}))

    stream = WaitingStream([], "hang")
    client, emit = Client([stream], "hang"), Recorder()
    task = asyncio.create_task(adapter(client).complete([], [], emit))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 0.5)
    end = finished(emit, 1)[0]
    assert end["error"] == "cancelled" and end["cleanup_warning"]["recovery"] == "failed"
    assert stream.closes == client.closes == 1


async def test_sdk_raw_stream_fault_not_just_fake_client() -> None:
    class Bytes(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield sse([chunk({"content": "PARTIAL"})]).split(b"data: [DONE]")[0]
            raise httpx.RemoteProtocolError(SECRET)

    model = model_for(lambda request: httpx.Response(200, stream=Bytes()))
    emit = Recorder()
    try:
        with pytest.raises(ModelError, match="model_transport"):
            await model.complete([], [], emit)
        assert finished(emit, 1)[0]["exception_type"] == "RemoteProtocolError"
    finally:
        await model.close()


@pytest.mark.parametrize("partial", [False, True])
async def test_loop_replay_tui_and_eval_metrics(tmp_path: Path, partial: bool) -> None:
    half = chunk(
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
    )
    stream = Stream([chunk({"content": "PARTIAL"}), half, httpx.RemoteProtocolError(SECRET)])
    client = Client([stream] if partial else [httpx.RemoteProtocolError(SECRET), success()])
    session = Session(tmp_path, run_id="run")
    writer = SessionLog.create(session, "test", {}, tmp_path)
    path = writer.path
    output = io.StringIO()
    tui = Transcript(Console(file=output, force_terminal=False))
    profiler = RunProfiler()

    async def consumer(event: RuntimeEvent) -> None:
        profiler.consume(event)
        await tui(event)

    events = Events(session, writer, consumer)
    try:
        result = await run_turn(session, "task", adapter(client), {}, events, Limits())
    finally:
        writer.close()
    assert result.outcome == ("failed" if partial else "completed")
    assert result.tool_calls == 0  # An incomplete Tool Call never reaches dispatch.
    assert output.getvalue().count("PARTIAL" if partial else "COMPLETE") == 1
    with path.open("rb") as source:
        rows, truncated = read_records(source)
    assert not truncated
    assert len([r for r in rows if r["kind"] == "model_started"]) == (1 if partial else 2)
    assert len([r for r in rows if r["kind"] == "model_finished"]) == (1 if partial else 2)
    assert replay(rows, tmp_path).messages == session.messages
    old_rows = deepcopy(rows)
    for row in old_rows:
        if row["kind"] == "model_finished":
            for key in ("phase", "seen_delta", "retry", "exception_type", "failure_kind"):
                row["data"].pop(key, None)
    assert replay(old_rows, tmp_path).messages == session.messages
    restored, resumed, _ = resume_session(path, tmp_path, tmp_path)
    try:
        assert restored.messages == session.messages
    finally:
        resumed.close()
    metrics = profile_metrics(profiler.profile)
    assert metrics["failed_attempts"] == 1
    assert metrics["retries"] == (0 if partial else 1)
    assert all(m.finished for m in profiler.profile.models)
    assert SECRET not in path.read_text(encoding="utf-8")


async def test_sink_failure_is_not_retried_or_swallowed() -> None:
    class FailingSink(Recorder):
        async def __call__(
            self, kind: str, data: Json, *, protocol_data: Json | None = None
        ) -> int:
            if kind == "assistant_delta":
                raise OSError("private storage failure")
            return await super().__call__(kind, data, protocol_data=protocol_data)

    stream, emit = success(), FailingSink()
    client = Client([stream])
    with pytest.raises(OSError, match="private storage failure"):
        await adapter(client).complete([], [], emit)
    end = finished(emit, 1)[0]
    assert end["phase"] == "publish" and end["failure_kind"] == "internal"
    assert not end["retry"] and stream.closes == 1
    assert "private storage failure" not in json.dumps(emit.events)


async def test_terminal_sink_failure_does_not_retry() -> None:
    class FailingSink(Recorder):
        async def __call__(
            self, kind: str, data: Json, *, protocol_data: Json | None = None
        ) -> int:
            await super().__call__(kind, data, protocol_data=protocol_data)
            if kind == "model_finished":
                raise OSError("persist unavailable")
            return len(self.events)

    emit = FailingSink()
    client = Client([httpx.RemoteProtocolError(SECRET), success()])
    with pytest.raises(OSError, match="persist unavailable"):
        await adapter(client).complete([], [], emit)
    assert len(client.requests) == 1
    finished(emit, 1)  # One emit invocation; persistence cannot be promised on sink failure.


async def test_owned_pool_cleanup_then_safe_retry(monkeypatch: Any) -> None:
    first = Client([Stream([httpx.ReadError(SECRET)], "transport")])
    second = Client([success()])
    clients = [first, second]
    monkeypatch.setattr(model_module, "AsyncOpenAI", lambda **kwargs: clients.pop(0))
    monkeypatch.setenv("TRANSPORT_TEST_KEY", "fake")
    model = ChatModel(ModelConfig("test", 32768, api_key_env="TRANSPORT_TEST_KEY"))
    emit = Recorder()
    assert (await model.complete([], [], emit)).message.content == "COMPLETE"
    ends = finished(emit, 2)
    assert ends[0]["retry"] and ends[0]["cleanup_warning"]["recovery"] == "client_closed"
    assert first.closes == 1 and first.requests == second.requests
