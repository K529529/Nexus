"""Owned LiteLLM SDK transport; Nexus retains retries and response assembly."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import zlib
from types import SimpleNamespace
from typing import Any, cast

import httpx
from openai import AsyncOpenAI

from nexus.app.config import ModelConfig
from nexus.core.types import ModelError


class LiteStream:
    def __init__(self, stream: Any, facts: WireFacts) -> None:
        self.stream = stream
        self.facts = facts

    def __aiter__(self) -> Any:
        return self

    async def __anext__(self) -> Any:
        chunk = await self.stream.__anext__()
        if self.facts.failure is not None:
            raise self.facts.failure
        if any(c.finish_reason for c in chunk.choices):
            if not self.facts.finished:
                raise ModelError("incomplete_stream")
        if getattr(chunk, "usage", None) is not None:
            # LiteLLM can synthesize usage when the provider reported none.
            # Only provider-bearing chunks count as reported Nexus usage.
            raw = self.facts.usage
            if raw is None:
                chunk.usage = None
            else:
                details = raw.get("prompt_tokens_details") or {}
                chunk.usage = SimpleNamespace(
                    prompt_tokens=raw.get("prompt_tokens"),
                    completion_tokens=raw.get("completion_tokens"),
                    total_tokens=raw.get("total_tokens"),
                    prompt_tokens_details=SimpleNamespace(
                        cached_tokens=details.get(
                            "cached_tokens", raw.get("prompt_cache_hit_tokens")
                        )
                    ),
                )
        return chunk

    async def close(self) -> None:
        # SDK aclose currently swallows provider cleanup failures. Close the raw
        # stream ourselves so the existing Nexus pool-retirement policy sees them.
        raw = getattr(self.stream, "completion_stream", None)
        if raw is not None:
            close = getattr(raw, "aclose", None) or getattr(raw, "close", None)
            if close:
                result = close()
                if inspect.isawaitable(result):
                    await result
            self.stream.completion_stream = None
        await self.stream.aclose()


class WireFacts:
    """Audit only finish/usage in the supported providers' OpenAI-compatible SSE.

    LiteLLM still owns payload normalization. These facts distinguish provider
    reports from SDK-synthesized values; they never enter logs or callbacks.
    """

    def __init__(self) -> None:
        self.pending = b""
        self.finished = False
        self.usage: dict[str, Any] | None = None
        self.failure: BaseException | None = None

    def observe(self, data: bytes) -> None:
        self.pending += data
        while b"\n" in self.pending:
            line, self.pending = self.pending.split(b"\n", 1)
            if not line.startswith(b"data:"):
                continue
            try:
                chunk = json.loads(line[5:])
            except (ValueError, UnicodeError):
                continue
            if not isinstance(chunk, dict):
                continue
            self.finished |= any(
                c.get("finish_reason") is not None for c in chunk.get("choices", [])
            )
            if isinstance(chunk.get("usage"), dict):
                self.usage = chunk["usage"]


class AuditedStream(httpx.AsyncByteStream):
    def __init__(self, source: httpx.AsyncByteStream, facts: WireFacts, encoding: str = "") -> None:
        self.source, self.facts = source, facts
        self.decoder = zlib.decompressobj(16 + zlib.MAX_WBITS) if encoding == "gzip" else None
        if encoding not in {"", "identity", "gzip"}:
            raise ModelError("unsupported_stream_encoding")

    async def __aiter__(self) -> Any:
        try:
            async for data in self.source:
                self.facts.observe(self.decoder.decompress(data) if self.decoder else data)
                yield data
        except (Exception, asyncio.CancelledError) as exc:
            self.facts.failure = exc
            raise

    async def aclose(self) -> None:
        await self.source.aclose()


class LiteTransport:
    def __init__(self, config: ModelConfig) -> None:
        # Avoid SDK import-time remote model-cost downloads. No credentials in globals.
        os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
        import litellm
        from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler

        self.sdk = litellm
        self.config = config
        litellm.telemetry = False
        litellm.suppress_debug_info = True
        litellm.turn_off_message_logging = True
        # Explicit ownership avoids LiteLLM global cached clients surviving the loop.
        self.client: Any = (
            AsyncOpenAI(
                api_key=config.key(),
                base_url=config.base_url,
                timeout=config.request_timeout_seconds,
                max_retries=0,
            )
            if config.provider in {"openai", "dashscope"}
            else AsyncHTTPHandler(timeout=config.request_timeout_seconds)
        )
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
        self.facts = WireFacts()
        http_client = (
            self.client._client
            if config.provider in {"openai", "dashscope"}
            else cast(Any, self.client).client
        )
        http_client.event_hooks["response"].append(self.audit)
        http_client.headers["Accept-Encoding"] = "identity"

    async def audit(self, response: httpx.Response) -> None:
        if response.is_success:
            if response.is_stream_consumed:
                self.facts.observe(response.content)
            else:
                response.stream = AuditedStream(
                    cast(httpx.AsyncByteStream, response.stream),
                    self.facts,
                    response.headers.get("content-encoding", "").lower(),
                )

    async def create(self, **request: Any) -> LiteStream:
        # Neither Router nor fallback nor callbacks. Both wrapper and wire retries off.
        config = self.config
        self.facts = WireFacts()
        stream = await self.sdk.acompletion(
            **{**request, "model": f"{config.provider}/{config.name}", "no-log": True},
            api_base=config.base_url,
            api_key=config.key(),
            client=self.client,
            timeout=config.request_timeout_seconds,
            num_retries=0,
            max_retries=0,
            drop_params=False,
            fallbacks=[],
            caching=False,
        )
        return LiteStream(stream, self.facts)

    async def close(self) -> None:
        await self.client.close()
