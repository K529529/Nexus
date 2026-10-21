"""One async Chat Completions adapter; provider wire details stay here."""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import asdict
from typing import Any

from openai import APIConnectionError, APIStatusError, AsyncOpenAI

from nexus.app.config import ModelConfig
from nexus.core.tokens import wire_message, wire_tools
from nexus.core.types import Emit, Json, Message, ModelError, ModelReply, ToolCall, ToolSpec, Usage


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
        from nexus.core.compaction import COMPACTION_PROMPT

        if tools or any(m.protocol_data or m.tool_calls or m.role == "tool" for m in messages):
            raise ModelError("invalid_compaction_request")
        if (
            len(messages) != 2
            or messages[0].role != "system"
            or messages[0].content != COMPACTION_PROMPT
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
        self.client: Any = client or AsyncOpenAI(
            api_key=config.key(),
            base_url=config.base_url,
            timeout=config.request_timeout_seconds,
            max_retries=0,
        )
        self.binding = hashlib.sha256(
            f"{config.base_url.rstrip('/')}\n{config.name}".encode(),
        ).hexdigest()

    async def close(self) -> None:
        await self.client.close()

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
            await emit("model_started", {"model": self.config.name, "attempt": attempt})
            try:
                async with asyncio.timeout(self.config.request_timeout_seconds):
                    stream = await self.client.chat.completions.create(**request)
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
                        if delta.model_dump(exclude_none=True):
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
                            await emit("assistant_delta", {"text": delta.content})
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
                await emit(
                    "model_finished",
                    {
                        "model": self.config.name,
                        "attempt": attempt,
                        "duration_ms": int((time.monotonic() - started) * 1000),
                        "finish_reason": finish,
                        "usage": asdict(usage),
                    },
                )
                return reply
            except (APIConnectionError, APIStatusError, TimeoutError) as exc:
                status = exc.status_code if isinstance(exc, APIStatusError) else None
                code = "model_transport" if status is None else f"model_http_{status}"
                if isinstance(exc, APIStatusError):
                    body = exc.body if isinstance(exc.body, dict) else {}
                    inner = body.get("error", body)
                    if isinstance(inner, dict) and inner.get("code") in {
                        "context_length_exceeded",
                        "context_length_error",
                    }:
                        code = "context_limit"
                await emit(
                    "model_finished",
                    {
                        "error": code,
                        "attempt": attempt,
                        "duration_ms": int((time.monotonic() - started) * 1000),
                    },
                )
                transient = status is None or status == 429 or status >= 500
                if attempt == 1 and not seen_delta and transient:
                    await asyncio.sleep(0.25)
                    continue
                raise ModelError(code) from None
            except (ModelError, asyncio.CancelledError) as exc:
                # A fully received empty stop has no action to replay. Keep partial streams,
                # malformed tool calls and visible answers outside this narrow retry case.
                empty = isinstance(exc, ModelError) and exc.code == "empty_or_invalid_final"
                await emit(
                    "model_finished",
                    {
                        "error": "empty_or_invalid_final" if empty else "stream_interrupted",
                        "attempt": attempt,
                        "duration_ms": int((time.monotonic() - started) * 1000),
                        **({"finish_reason": finish, "usage": asdict(usage)} if empty else {}),
                    },
                )
                if empty and attempt == 1:
                    continue
                raise
            finally:
                if stream is not None:
                    await stream.close()
        raise AssertionError("unreachable")
