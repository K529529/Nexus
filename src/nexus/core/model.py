"""One async Chat Completions adapter; provider wire details stay here."""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from dataclasses import asdict
from typing import Any
from urllib.parse import urlsplit

import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI

from nexus.app.config import ModelConfig
from nexus.core.tokens import wire_message, wire_tools
from nexus.core.types import Emit, Json, Message, ModelError, ModelReply, ToolCall, ToolSpec, Usage

CLEANUP_TIMEOUT_SECONDS = 2.0
TRANSPORT_ERRORS = (
    APIConnectionError,
    httpx.NetworkError,
    httpx.RemoteProtocolError,
    httpx.ProxyError,
    httpx.TimeoutException,
    TimeoutError,
)


def context_overflow(status: int, body: object, base_url: str) -> bool:
    if not isinstance(body, dict):
        return False
    inner = body.get("error", body)
    if not isinstance(inner, dict):
        return False
    if inner.get("code") in {"context_length_exceeded", "context_length_error"}:
        return True
    # Provider-specific wording, not a generic 400 or quota classification. The
    # quota_limit_reached code is also used for account/usage limits; require text.
    if status != 400 or urlsplit(base_url).hostname != "api.deepseek.com":
        return False
    message = inner.get("message")
    if not isinstance(message, str):
        return False
    return bool(
        re.match(r"input[\s_-]tokens?\s+exceed(?:s|ed)?\s+the\s+limit\b", message, re.I)
        or re.match(r"this model's maximum context length is \d+ tokens\b", message, re.I)
        or re.match(r"the input is longer than the model's context length\b", message, re.I)
    )


class CompactionModel:
    """A separate client with no tool or private-protocol continuation capability."""

    def __init__(self, config: ModelConfig, *, client: Any = None) -> None:
        self.config = config
        self.adapter = ChatModel(config, client=client, json_object=True)

    async def close(self) -> None:
        await self.adapter.close()

    async def complete(
        self, messages: list[Message], tools: list[ToolSpec], emit: Emit
    ) -> ModelReply:
        from nexus.core.compaction import COMPACTION_PROMPT, UPDATE_COMPACTION_PROMPT

        if tools or any(m.protocol_data or m.tool_calls or m.role == "tool" for m in messages):
            raise ModelError("invalid_compaction_request")
        if (
            len(messages) != 2
            or messages[0].role != "system"
            or messages[0].content not in {COMPACTION_PROMPT, UPDATE_COMPACTION_PROMPT}
            or messages[1].role != "user"
        ):
            raise ModelError("invalid_compaction_prompt")

        async def events(kind: str, data: Json, **_: Any) -> int:
            if kind == "assistant_delta":
                return 0
            return await emit(kind, {**data, "purpose": "compaction"})

        reply = await self.adapter.complete(messages, [], events)
        if reply.message.tool_calls or reply.finish_reason != "stop":
            raise ModelError("invalid_compaction_response")
        reply.message.protocol_data = None
        return reply


class ChatModel:
    def __init__(
        self, config: ModelConfig, *, client: Any = None, json_object: bool = False
    ) -> None:
        self.config = config
        self.json_object = json_object
        self._owns_client = client is None
        self._client_retired = False
        self._cleanup_incomplete = False
        self.client: Any = client or self._new_client()
        self.binding = hashlib.sha256(
            f"{config.base_url.rstrip('/')}\n{config.name}".encode(),
        ).hexdigest()

    def _new_client(self) -> AsyncOpenAI:
        config = self.config
        return AsyncOpenAI(
            api_key=config.key(),
            base_url=config.base_url,
            timeout=config.request_timeout_seconds,
            max_retries=0,
        )

    async def close(self) -> None:
        await self.client.close()

    async def _close_stream(self, stream: Any) -> tuple[Json | None, BaseException | None]:
        try:
            async with asyncio.timeout(CLEANUP_TIMEOUT_SECONDS):
                await stream.close()
            return None, None
        except (Exception, asyncio.CancelledError) as exc:
            # Never trust Response.is_closed: HTTPX sets it before transport cleanup.
            # Retire the entire pool rather than reuse a possibly live connection.
            self._client_retired = True
            warning: Json = {
                "phase": "close",
                "exception_type": type(exc).__name__[:80],
            }
            try:
                async with asyncio.timeout(CLEANUP_TIMEOUT_SECONDS):
                    await self.client.close()
            except (Exception, asyncio.CancelledError) as cleanup:
                self._cleanup_incomplete = True
                warning.update(
                    recovery="failed",
                    recovery_exception_type=type(cleanup).__name__[:80],
                )
                if isinstance(exc, asyncio.CancelledError):
                    return warning, exc
                if isinstance(cleanup, asyncio.CancelledError):
                    return warning, cleanup
                return warning, ModelError("model_cleanup_incomplete")
            warning["recovery"] = "client_closed"
            # A programming error in cleanup is not a recoverable transport warning.
            return warning, None if isinstance(exc, TRANSPORT_ERRORS) else exc

    def wire_messages(self, messages: list[Message]) -> list[Json]:
        result: list[Json] = []
        for message in messages:
            wire = wire_message(message)
            if message.protocol_data:
                # Only adapter-generated, bound continuation fields can cross the wire.
                data = message.protocol_data
                if data.get("format") != "chat-v1" or data.get("binding") != self.binding:
                    raise ModelError("protocol_binding", "Start a new session for this service.")
                fields = data.get("fields")
                if not isinstance(fields, dict) or set(fields) != {"reasoning_content"}:
                    raise ModelError("protocol_data_invalid")
                if not isinstance(fields["reasoning_content"], str):
                    raise ModelError("protocol_data_invalid")
                wire.update(fields)
            result.append(wire)
        return result

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        emit: Emit,
    ) -> ModelReply:
        if self._cleanup_incomplete:
            raise ModelError("model_cleanup_incomplete")
        if self._client_retired:
            if not self._owns_client:
                raise ModelError("model_client_closed")
            self.client = self._new_client()
            self._client_retired = False
        request: Json = {
            "model": self.config.name,
            "messages": self.wire_messages(messages),
            "stream": True,
            "n": 1,
            self.config.output_token_parameter: self.config.max_output_tokens,
        }
        if tools:
            request["tools"] = wire_tools(tools)
        if self.json_object:
            request["response_format"] = {"type": "json_object"}
        if self.config.reasoning_effort is not None:
            request["reasoning_effort"] = self.config.reasoning_effort
        if self.config.enable_thinking is not None:
            request["extra_body"] = {"enable_thinking": self.config.enable_thinking}
        if self.config.include_usage:
            request["stream_options"] = {"include_usage": True}
        for attempt in (1, 2):
            started = time.monotonic()
            seen_delta = False
            stream = None
            reply = None
            failure: BaseException | None = None
            diagnostic: Json = {}
            phase = "create"
            retry = False
            await emit("model_started", {"model": self.config.name, "attempt": attempt})
            try:
                async with asyncio.timeout(self.config.request_timeout_seconds):
                    stream = await self.client.chat.completions.create(**request)
                    phase = "stream"
                    content: list[str] = []
                    reasoning: list[str] = []
                    calls: dict[int, Json] = {}
                    finish: str | None = None
                    usage = Usage()
                    async for chunk in stream:
                        if chunk.usage is not None:
                            details = getattr(chunk.usage, "prompt_tokens_details", None)
                            cached = getattr(details, "cached_tokens", None)
                            if (
                                type(cached) is not int
                                or cached < 0
                                or type(chunk.usage.prompt_tokens) is not int
                                or cached > chunk.usage.prompt_tokens
                            ):
                                cached = None
                            usage = Usage(
                                chunk.usage.prompt_tokens,
                                chunk.usage.completion_tokens,
                                chunk.usage.total_tokens,
                                "reported",
                                cached,
                            )
                        if not chunk.choices:
                            continue
                        if len(chunk.choices) != 1 or chunk.choices[0].index != 0:
                            raise ModelError("invalid_choices")
                        choice = chunk.choices[0]
                        delta = choice.delta
                        # Role-only/empty heartbeat chunks do not constitute output.
                        if (
                            delta.content
                            or delta.tool_calls
                            or delta.refusal
                            or getattr(delta, "reasoning_content", None)
                        ):
                            seen_delta = True
                        if delta.refusal:
                            raise ModelError("model_refusal")
                        fragment = getattr(delta, "reasoning_content", None)
                        if fragment is not None and not isinstance(fragment, str):
                            raise ModelError("invalid_reasoning_content")
                        if finish is not None and (delta.content or delta.tool_calls or fragment):
                            raise ModelError("delta_after_finish")
                        if fragment:
                            reasoning.append(fragment)
                        if delta.content:
                            content.append(delta.content)
                            phase = "publish"
                            await emit("assistant_delta", {"text": delta.content})
                            phase = "stream"
                        for call in delta.tool_calls or []:
                            if call.index < 0 or (call.type and call.type != "function"):
                                raise ModelError("invalid_tool_delta")
                            item = calls.setdefault(call.index, {"id": "", "name": "", "args": ""})
                            item["id"] += call.id or ""
                            if call.function:
                                item["name"] += call.function.name or ""
                                item["args"] += call.function.arguments or ""
                        if choice.finish_reason is not None:
                            if finish is not None:
                                raise ModelError("duplicate_finish")
                            finish = choice.finish_reason
                    if finish is None:
                        raise ModelError("incomplete_stream")
                    reply = ModelReply(
                        Message(
                            "assistant",
                            "".join(content),
                            [
                                ToolCall(item["id"], item["name"], item["args"])
                                for _, item in sorted(calls.items())
                            ],
                            protocol_data={
                                "format": "chat-v1",
                                "binding": self.binding,
                                "fields": {"reasoning_content": "".join(reasoning)},
                            }
                            if reasoning
                            else None,
                        ),
                        finish,
                        usage,
                    )
                    if (
                        finish == "stop"
                        and not reply.message.tool_calls
                        and not reply.message.content.strip()
                    ):
                        raise ModelError("empty_or_invalid_final")
                diagnostic.update(finish_reason=finish, usage=asdict(usage))
            except (*TRANSPORT_ERRORS, APIStatusError) as exc:
                if phase == "publish":
                    # Sink failures are neither provider errors nor safe retry signals.
                    failure = exc
                    diagnostic.update(error="runtime_error", failure_kind="internal")
                else:
                    status = exc.status_code if isinstance(exc, APIStatusError) else None
                    code = "model_transport" if status is None else f"model_http_{status}"
                    if isinstance(exc, APIStatusError):
                        if context_overflow(exc.status_code, exc.body, self.config.base_url):
                            code = "context_limit"
                    diagnostic.update(
                        error=code,
                        failure_kind="http"
                        if status is not None
                        else (
                            "timeout"
                            if isinstance(
                                exc,
                                (TimeoutError, httpx.TimeoutException, APITimeoutError),
                            )
                            else "transport"
                        ),
                    )
                    transient = status is None or status == 429 or status >= 500
                    retry = (
                        attempt == 1 and not seen_delta and transient and code != "context_limit"
                    )
                    failure = ModelError(code)
                diagnostic["exception_type"] = type(exc).__name__[:80]
            except (ModelError, asyncio.CancelledError) as exc:
                failure = exc
                empty = isinstance(exc, ModelError) and exc.code == "empty_or_invalid_final"
                diagnostic.update(
                    error=exc.code if isinstance(exc, ModelError) else "cancelled",
                    failure_kind="protocol" if isinstance(exc, ModelError) else "cancelled",
                    exception_type=type(exc).__name__[:80],
                )
                if empty:
                    # Preserve the existing fully received empty-stop recovery exception.
                    diagnostic.update(finish_reason=finish, usage=asdict(usage))
                    retry = attempt == 1
            except Exception as exc:
                # Record completion but re-raise: never relabel parser/programming bugs.
                failure = exc
                diagnostic.update(
                    error="runtime_error",
                    failure_kind="internal",
                    exception_type=type(exc).__name__[:80],
                )
            finally:
                if stream is not None:
                    warning, cleanup_failure = await self._close_stream(stream)
                    if warning is not None:
                        diagnostic["cleanup_warning"] = warning
                    if cleanup_failure is not None:
                        retry = False
                        if failure is None or isinstance(cleanup_failure, asyncio.CancelledError):
                            failure = cleanup_failure
                            phase = "close"
                            diagnostic.update(
                                error="cancelled"
                                if isinstance(failure, asyncio.CancelledError)
                                else (
                                    failure.code
                                    if isinstance(failure, ModelError)
                                    else "runtime_error"
                                ),
                                failure_kind="cancelled"
                                if isinstance(failure, asyncio.CancelledError)
                                else "cleanup",
                                exception_type=type(failure).__name__[:80],
                            )
                if self._client_retired and not self._owns_client:
                    retry = False
                await emit(
                    "model_finished",
                    {
                        "model": self.config.name,
                        "attempt": attempt,
                        "phase": phase,
                        "duration_ms": int((time.monotonic() - started) * 1000),
                        "seen_delta": seen_delta,
                        "retry": retry,
                        **diagnostic,
                    },
                )
            if retry:
                await asyncio.sleep(0.25)
                if self._client_retired:
                    if not self._owns_client:
                        raise ModelError("model_client_closed")
                    self.client = self._new_client()
                    self._client_retired = False
                continue
            if failure is not None:
                raise failure from None
            assert reply is not None
            return reply
        raise AssertionError("unreachable")
