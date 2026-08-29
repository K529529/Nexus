"""Nexus-owned lifecycle boundary for a graph checkpoint provider."""

from typing import Protocol


class CheckpointProvider(Protocol):
    async def setup(self) -> None: ...

    def get_checkpointer(self) -> object: ...

    async def close(self) -> None: ...
