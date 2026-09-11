"""LangChain-backed adapter for the approved OpenAI-compatible provider."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

from langchain_openai import ChatOpenAI

from nexus.config.models import SUPPORTED_MODEL_PROVIDER, RuntimeConfig
from nexus.domain.model import (
    ModelChunk,
    ModelMessage,
    ModelResponse,
    TokenUsage,
    UsageAvailability,
)
from nexus.errors import ConfigurationError, ModelError


class OpenAICompatibleModelGateway:
    """Normalize LangChain/OpenAI-compatible calls into Nexus-owned values."""

    def __init__(self, config: RuntimeConfig) -> None:
        self._config = config
        self._client: ChatOpenAI | None = None

    async def complete(self, messages: Sequence[ModelMessage]) -> ModelResponse:
        try:
            response = await self._get_client().ainvoke(self._convert_messages(messages))
            content = _normalize_content(response.content)
        except ConfigurationError:
            raise
        except Exception as exc:
            raise ModelError(
                "The configured model request failed.",
                retryable=True,
            ) from exc
        if not content.strip():
            raise ModelError(
                "The configured model returned empty output.",
                code="EMPTY_MODEL_OUTPUT",
            )
        return ModelResponse(content=content, usage=_normalize_usage(response.usage_metadata))

    async def stream(self, messages: Sequence[ModelMessage]) -> AsyncIterator[ModelChunk]:
        pending_content: str | None = None
        terminal_usage: TokenUsage | None = None
        try:
            async for chunk in self._get_client().astream(self._convert_messages(messages)):
                if pending_content is not None:
                    yield ModelChunk(content=pending_content)
                pending_content = _normalize_content(chunk.content)
                usage = _normalize_usage(chunk.usage_metadata)
                if usage is not None:
                    terminal_usage = usage
            if pending_content is not None:
                yield ModelChunk(content=pending_content, usage=terminal_usage)
        except ConfigurationError:
            raise
        except Exception as exc:
            raise ModelError(
                "The configured model stream failed.",
                retryable=True,
            ) from exc

    def _get_client(self) -> ChatOpenAI:
        if self._client is not None:
            return self._client
        if self._config.model_provider != SUPPORTED_MODEL_PROVIDER:
            raise ConfigurationError("Only the openai_compatible provider is supported on Day 1.")
        if self._config.model_name is None:
            raise ConfigurationError("Configure a model name before running nexus chat.")
        if self._config.model_api_key is None:
            raise ConfigurationError("Set NEXUS_MODEL_API_KEY before running nexus chat.")

        try:
            self._client = ChatOpenAI(
                model=self._config.model_name,
                base_url=self._config.model_base_url,
                api_key=self._config.model_api_key,
            )
        except Exception as exc:
            raise ModelError("The configured model could not be initialized.") from exc
        return self._client

    @staticmethod
    def _convert_messages(messages: Sequence[ModelMessage]) -> list[tuple[str, str]]:
        return [(message.role, message.content) for message in messages]


def _normalize_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""

    text_parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            text_parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                text_parts.append(text)
    return "".join(text_parts)


def _normalize_usage(value: object) -> TokenUsage | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        return TokenUsage.unavailable()
    raw_values = (
        value.get("input_tokens"),
        value.get("output_tokens"),
        value.get("total_tokens"),
    )
    normalized = tuple(
        item
        if isinstance(item, int) and not isinstance(item, bool) and item >= 0
        else None
        for item in raw_values
    )
    input_tokens, output_tokens, total_tokens = normalized
    if (
        input_tokens is not None
        and output_tokens is not None
        and total_tokens is not None
        and total_tokens != input_tokens + output_tokens
    ):
        normalized = (input_tokens, output_tokens, None)
    known = sum(item is not None for item in normalized)
    if known == 0:
        return TokenUsage.unavailable()
    availability = (
        UsageAvailability.REPORTED if known == 3 else UsageAvailability.PARTIAL
    )
    return TokenUsage(normalized[0], normalized[1], normalized[2], availability)
