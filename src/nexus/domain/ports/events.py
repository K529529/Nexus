"""Nexus-owned runtime event publication boundary."""

from __future__ import annotations

from typing import Protocol

from nexus.domain.runtime_events import RuntimeEvent


class EventSubscriber(Protocol):
    async def on_event(self, event: RuntimeEvent) -> None: ...


class EventSubscription(Protocol):
    async def aclose(self) -> None: ...


class EventPublisher(Protocol):
    def subscribe(self, subscriber: EventSubscriber) -> EventSubscription: ...

    async def publish(self, event: RuntimeEvent) -> None: ...
