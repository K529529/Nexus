from collections.abc import AsyncIterator
from typing import cast

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from nexus.config.models import RuntimeConfig
from nexus.domain.model import ModelMessage
from nexus.errors import ConfigurationError
from nexus.infrastructure.model_gateway.openai_compatible import OpenAICompatibleModelGateway


class FakeChatClient:
    async def ainvoke(self, messages: object) -> AIMessage:
        return AIMessage(content="Normalized response")

    async def astream(self, messages: object) -> AsyncIterator[AIMessageChunk]:
        yield AIMessageChunk(content="Normal")
        yield AIMessageChunk(content="ized")


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


@pytest.mark.asyncio
async def test_stream_normalizes_provider_chunks(gateway: OpenAICompatibleModelGateway) -> None:
    chunks = [
        chunk.content
        async for chunk in gateway.stream([ModelMessage(role="user", content="hello")])
    ]
    assert chunks == ["Normal", "ized"]


@pytest.mark.asyncio
async def test_missing_model_configuration_is_safe_configuration_error() -> None:
    gateway = OpenAICompatibleModelGateway(RuntimeConfig())
    with pytest.raises(ConfigurationError, match="model name"):
        await gateway.complete([ModelMessage(role="user", content="hello")])

