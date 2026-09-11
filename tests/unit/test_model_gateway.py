from collections.abc import AsyncIterator
from typing import cast

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from nexus.config.models import RuntimeConfig
from nexus.domain.model import ModelMessage, UsageAvailability
from nexus.errors import ConfigurationError
from nexus.infrastructure.model_gateway.openai_compatible import OpenAICompatibleModelGateway


class FakeChatClient:
    async def ainvoke(self, messages: object) -> AIMessage:
        return AIMessage(
            content="Normalized response",
            usage_metadata={"input_tokens": 2, "output_tokens": 3, "total_tokens": 5},
        )

    async def astream(self, messages: object) -> AsyncIterator[AIMessageChunk]:
        yield AIMessageChunk(content="Normal")
        yield AIMessageChunk(
            content="ized",
            usage_metadata={"input_tokens": 2, "output_tokens": 3, "total_tokens": 5},
        )


class MalformedUsageChatClient:
    async def ainvoke(self, messages: object) -> AIMessage:
        return AIMessage(
            content="Still a valid response",
            usage_metadata={"input_tokens": 2, "output_tokens": 3, "total_tokens": 99},
        )


@pytest.fixture
def gateway() -> OpenAICompatibleModelGateway:
    config = RuntimeConfig(model_name="test-model", model_api_key=SecretStr("test-secret"))
    model_gateway = OpenAICompatibleModelGateway(config)
    model_gateway._client = cast(ChatOpenAI, FakeChatClient())
    return model_gateway


@pytest.mark.asyncio
async def test_complete_normalizes_provider_response(
    gateway: OpenAICompatibleModelGateway,
) -> None:
    response = await gateway.complete([ModelMessage(role="user", content="hello")])
    assert response.content == "Normalized response"
    assert response.usage is not None
    assert response.usage.availability is UsageAvailability.REPORTED
    assert response.usage.total_tokens == 5


@pytest.mark.asyncio
async def test_stream_normalizes_provider_chunks(gateway: OpenAICompatibleModelGateway) -> None:
    chunks = [
        chunk
        async for chunk in gateway.stream([ModelMessage(role="user", content="hello")])
    ]
    assert [chunk.content for chunk in chunks] == ["Normal", "ized"]
    assert chunks[0].usage is None
    assert chunks[1].usage is not None and chunks[1].usage.total_tokens == 5


@pytest.mark.asyncio
async def test_malformed_usage_degrades_without_failing_model_response() -> None:
    config = RuntimeConfig(
        model_name="test-model", model_api_key=SecretStr("test-secret")
    )
    gateway = OpenAICompatibleModelGateway(config)
    gateway._client = cast(ChatOpenAI, MalformedUsageChatClient())

    response = await gateway.complete([ModelMessage(role="user", content="hello")])

    assert response.content == "Still a valid response"
    assert response.usage is not None
    assert response.usage.availability is UsageAvailability.PARTIAL
    assert response.usage.input_tokens == 2
    assert response.usage.output_tokens == 3
    assert response.usage.total_tokens is None


@pytest.mark.asyncio
async def test_missing_model_configuration_is_safe_configuration_error() -> None:
    gateway = OpenAICompatibleModelGateway(RuntimeConfig())
    with pytest.raises(ConfigurationError, match="model name"):
        await gateway.complete([ModelMessage(role="user", content="hello")])
