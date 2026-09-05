from collections.abc import Sequence

from nexus.domain.context import EmbeddingConfig


class FixtureEmbedding:
    """Known-vector fixture for retrieval mechanics, not evidence of model quality."""

    def __init__(self, model: str = "fixture", dimension: int = 3) -> None:
        self._config = EmbeddingConfig("openai_compatible", model, dimension)
        self.document_calls = 0
        self.query_calls = 0
        self.fail = False

    @property
    def config(self) -> EmbeddingConfig:
        return self._config

    def _vector(self, text: str) -> tuple[float, ...]:
        tokens = text.lower()
        if "credential" in tokens or "authenticate" in tokens:
            values = (1.0, 0.0, 0.0)
        elif "alpha" in tokens or "value" in tokens:
            values = (0.0, 1.0, 0.0)
        else:
            values = (0.0, 0.0, 1.0)
        return tuple(values[i % 3] for i in range(self.config.dimension))

    async def embed_documents(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        self.document_calls += len(texts)
        if self.fail:
            raise RuntimeError("Fixture embedding failure")
        return tuple(self._vector(text) for text in texts)

    async def embed_query(self, text: str) -> tuple[float, ...]:
        self.query_calls += 1
        if self.fail:
            raise RuntimeError("Fixture embedding failure")
        return self._vector(text)
