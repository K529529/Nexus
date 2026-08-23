"""Provider-neutral model boundary."""

from collections.abc import AsyncIterator, Sequence
from typing import Protocol

from nexus.domain.model import ModelChunk, ModelMessage, ModelResponse


class ModelGateway(Protocol):
    """Acquire completions without exposing concrete provider types."""

    async def complete(self, messages: Sequence[ModelMessage]) -> ModelResponse:
        """Return one normalized model completion."""

        ...

    def stream(self, messages: Sequence[ModelMessage]) -> AsyncIterator[ModelChunk]:
        """Stream normalized model content fragments."""

        ...

