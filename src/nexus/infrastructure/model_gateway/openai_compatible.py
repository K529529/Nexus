"""LangChain-backed adapter for the approved OpenAI-compatible provider."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

from langchain_openai import ChatOpenAI

from nexus.config.models import SUPPORTED_MODEL_PROVIDER, RuntimeConfig
from nexus.domain.model import ModelChunk, ModelMessage, ModelResponse
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
        return ModelResponse(content=content)

    async def stream(self, messages: Sequence[ModelMessage]) -> AsyncIterator[ModelChunk]:
        try:
            async for chunk in self._get_client().astream(self._convert_messages(messages)):
                yield ModelChunk(content=_normalize_content(chunk.content))
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
