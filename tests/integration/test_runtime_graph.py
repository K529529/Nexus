from collections.abc import AsyncIterator, Sequence

import pytest
from pydantic import SecretStr

from nexus.config.models import RuntimeConfig
from nexus.domain.model import ModelChunk, ModelMessage, ModelResponse
from nexus.domain.runtime_events import FinalResult
from nexus.infrastructure.bootstrap import bootstrap_application


class MockModelGateway:
    def __init__(self) -> None:
        self.requests: list[list[ModelMessage]] = []

    async def complete(self, messages: Sequence[ModelMessage]) -> ModelResponse:
        self.requests.append(list(messages))
        return ModelResponse(content="Hello from the mocked model.")

    async def stream(self, messages: Sequence[ModelMessage]) -> AsyncIterator[ModelChunk]:
        for content in ("Hello ", "from stream."):
            yield ModelChunk(content=content)


@pytest.mark.asyncio
async def test_runtime_to_langgraph_to_mocked_gateway() -> None:
    gateway = MockModelGateway()
    config = RuntimeConfig(model_name="mock-model", model_api_key=SecretStr("mock-secret"))

    async with bootstrap_application(config, model_gateway=gateway) as application:
        events = [event async for event in application.runtime.run("Reply with a greeting.")]

    assert [type(event).__name__ for event in events] == ["TaskStarted", "FinalResult"]
    final = events[-1]
    assert isinstance(final, FinalResult)
    assert final.content == "Hello from the mocked model."
    assert gateway.requests == [[ModelMessage(role="user", content="Reply with a greeting.")]]
