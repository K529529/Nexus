"""Pending user input; delivery is owned by the loop at a protocol-safe boundary."""

import asyncio
from dataclasses import dataclass
from typing import Literal
from uuid import uuid4

from nexus.core.types import Emit


@dataclass
class InputMessage:
    id: str
    text: str
    mode: Literal["guide", "queue"]


class Inbox:
    def __init__(self) -> None:
        self.pending: list[InputMessage] = []
        self.changed = asyncio.Event()

    async def receive(self, text: str, mode: Literal["guide", "queue"], emit: Emit) -> None:
        if not text.strip():
            return
        item = InputMessage(uuid4().hex[:8], text, mode)
        await emit("input_received", {"id": item.id, "mode": mode, "text": text})
        self.pending.append(item)
        self.changed.set()

    async def change(self, ident: str, text: str | None, emit: Emit) -> bool:
        item = next((m for m in self.pending if m.id == ident), None)
        if item is None:
            return False
        await emit("input_updated" if text else "input_withdrawn", {"id": ident, "text": text})
        if text:
            item.text = text
        else:
            self.pending.remove(item)
        return True

    def guides(self) -> list[InputMessage]:
        return [m for m in self.pending if m.mode == "guide"]
