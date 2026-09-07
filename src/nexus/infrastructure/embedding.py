"""Independent LangChain OpenAI-compatible embedding adapter."""

from collections.abc import Sequence
from math import isfinite

from langchain_openai import OpenAIEmbeddings

from nexus.config.models import RuntimeConfig
from nexus.domain.context import EmbeddingConfig
from nexus.errors import ContextError


class OpenAICompatibleEmbeddingGateway:
    def __init__(self, config: RuntimeConfig) -> None:
        config.require_embedding()
        self._config = EmbeddingConfig(
            config.embedding_provider,
            config.embedding_model,
            config.embedding_dimension,
        )
        self._client = OpenAIEmbeddings(
            model=config.embedding_model,
            base_url=config.embedding_base_url,
            api_key=config.embedding_api_key,
            dimensions=config.embedding_dimension,
            check_embedding_ctx_length=False,
        )

    @property
    def config(self) -> EmbeddingConfig:
        return self._config

    async def embed_documents(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        if not texts:
            return ()
        try:
            vectors = await self._client.aembed_documents(list(texts))
            return validate_vectors(vectors, len(texts), self.config.dimension)
        except Exception as exc:
            raise ContextError(
                "Embedding request failed or returned invalid vectors.",
                code="EMBEDDING_FAILED",
                retryable=True,
            ) from exc

    async def embed_query(self, text: str) -> tuple[float, ...]:
        return (await self.embed_documents((text,)))[0]


def validate_vectors(
    vectors: Sequence[Sequence[float]],
    count: int,
    dimension: int,
) -> tuple[tuple[float, ...], ...]:
    result = tuple(tuple(float(value) for value in vector) for vector in vectors)
    if len(result) != count or any(
        len(vector) != dimension or not all(isfinite(value) for value in vector) or not any(vector)
        for vector in result
    ):
        raise ContextError("Invalid embedding shape or values.", code="EMBEDDING_FAILED")
    return result
