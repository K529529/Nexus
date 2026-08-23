"""Provider-neutral model request and response values."""

from dataclasses import dataclass
from typing import Literal

ModelRole = Literal["system", "user", "assistant"]


@dataclass(frozen=True, slots=True)
class ModelMessage:
    """A minimal Nexus-owned chat message."""

    role: ModelRole
    content: str


@dataclass(frozen=True, slots=True)
class ModelResponse:
    """A normalized non-streaming model response."""

    content: str


@dataclass(frozen=True, slots=True)
class ModelChunk:
    """A normalized streaming model fragment."""

    content: str

