"""Pending user input; delivery is owned by the loop at a protocol-safe boundary."""

import asyncio
from dataclasses import dataclass
from typing import Literal
from uuid import uuid4

from nexus.core.types import Emit, Json


@dataclass
class InputMessage:
    id: str
    text: str
    mode: Literal["guide", "queue"]
    delivering: bool = False


class Inbox:
    def __init__(self) -> None:
        self.pending: list[InputMessage] = []
        self.changed = asyncio.Event()

    async def receive(self, text: str, mode: Literal["guide", "queue"], emit: Emit) -> None:
        if not text.strip():
            return
        item = InputMessage(uuid4().hex[:8], text, mode)

        async def commit() -> None:
            await emit("input_received", {"id": item.id, "mode": mode, "text": text})
            self.pending.append(item)
            self.changed.set()

        receipt = asyncio.create_task(commit())
        try:
            await asyncio.shield(receipt)
        except asyncio.CancelledError:
            # Finish the durable receipt + memory update before acknowledging cancellation.
            await asyncio.shield(receipt)
            raise

    async def change(self, ident: str, text: str | None, emit: Emit) -> bool:
        item = next((m for m in self.pending if m.id == ident), None)
        if item is None or item.delivering:
            return False
        # Make the edit visible before the event consumer can yield to delivery.
        if text:
            item.text = text
        else:
            self.pending.remove(item)
        await emit("input_updated" if text else "input_withdrawn", {"id": ident, "text": text})
        return True

    def guides(self) -> list[InputMessage]:
        return [m for m in self.pending if m.mode == "guide"]

    def restore(self, records: list[Json]) -> None:
        pending: dict[str, InputMessage] = {}
        for row in records:
            data = row["data"]
            if row["kind"] == "input_received":
                pending[data["id"]] = InputMessage(data["id"], data["text"], data["mode"])
            elif row["kind"] == "input_updated" and data["id"] in pending:
                pending[data["id"]].text = data["text"]
            elif row["kind"] == "input_withdrawn":
                pending.pop(data["id"], None)
            elif row["kind"] == "message" and data.get("input_id"):
                pending.pop(data["input_id"], None)
        self.pending = list(pending.values())
