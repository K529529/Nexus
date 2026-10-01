"""One async Chat Completions adapter; provider wire details stay here."""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import asdict
from typing import Any

from openai import APIConnectionError, APIStatusError, AsyncOpenAI

from nexus.app.config import ModelConfig
from nexus.core.types import Emit, Json, Message, ModelError, ModelReply, ToolCall, ToolSpec, Usage


class ChatModel:
    def __init__(self, config: ModelConfig, *, client: Any = None) -> None:
        self.config = config
        self.client: Any = client or AsyncOpenAI(
            api_key=config.key(),
            base_url=config.base_url,
            timeout=120,
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
            wire: Json = {"role": message.role, "content": message.content}
            if message.tool_calls:
                wire["tool_calls"] = [
                    {
                        "id": c.id,
                        "type": "function",
                        "function": {
                            "name": c.name,
                            "arguments": c.arguments_json,
                        },
                    }
                    for c in message.tool_calls
                ]
            if message.tool_call_id is not None:
                wire["tool_call_id"] = message.tool_call_id
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
            "max_tokens": self.config.max_output_tokens,
        }
        if tools:
            request["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.input_schema,
                    },
                }
                for t in tools
            ]
        if self.config.reasoning_effort is not None:
            request["reasoning_effort"] = self.config.reasoning_effort
        if self.config.include_usage:
            request["stream_options"] = {"include_usage": True}
        for attempt in (1, 2):
            started = time.monotonic()
            seen_delta = False
            stream = None
            await emit("model_started", {"model": self.config.name, "attempt": attempt})
            try:
                async with asyncio.timeout(120):
                    stream = await self.client.chat.completions.create(**request)
                    content: list[str] = []
                    calls: dict[int, Json] = {}
                    finish: str | None = None
                    usage = Usage()
                    async for chunk in stream:
                        if chunk.usage is not None:
                            usage = Usage(
                                chunk.usage.prompt_tokens,
                                chunk.usage.completion_tokens,
                                chunk.usage.total_tokens,
                                "reported",
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
                        if finish is not None and (delta.content or delta.tool_calls):
                            raise ModelError("delta_after_finish")
                        if delta.content:
                            content.append(delta.content)
                            await emit("assistant_delta", {"text": delta.content})
                        # reasoning_content is private. The initial compatible profile does
                        # not retain it; a live-required continuation must be verified first.
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
                        ),
                        finish,
                        usage,
                    )
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
                await emit("model_finished", {"error": code, "attempt": attempt})
                transient = status is None or status == 429 or status >= 500
                if attempt == 1 and not seen_delta and transient:
                    await asyncio.sleep(0.25)
                    continue
                raise ModelError(code) from None
            except (ModelError, asyncio.CancelledError):
                await emit("model_finished", {"error": "stream_interrupted", "attempt": attempt})
                raise
            finally:
                if stream is not None:
                    await stream.close()
        raise AssertionError("unreachable")
