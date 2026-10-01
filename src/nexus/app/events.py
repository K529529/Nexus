"""Persist first, then deliver a redacted public event to the terminal/export."""

from __future__ import annotations

import sys
from collections.abc import Awaitable, Callable
from typing import Any

from nexus.app.session import SessionLog, now
from nexus.core.types import Json, RuntimeEvent, Session

DELTA_KINDS = {"assistant_delta", "tool_output_delta"}


def redact(value: Any, secrets: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, dict):
        return {key: redact(item, secrets) for key, item in value.items() if key != "protocol_data"}
    if isinstance(value, list):
        return [redact(item, secrets) for item in value]
    return value


class Events:
    def __init__(
        self,
        session: Session,
        writer: SessionLog,
        consumer: Callable[[RuntimeEvent], Awaitable[None]],
        secrets: tuple[str, ...] = (),
    ) -> None:
        self.session, self.writer, self.consumer = session, writer, consumer
        self.secrets = tuple(s for s in secrets if s)
        self.pending: dict[tuple[str, str, str], tuple[str, Json]] = {}
        self.consumer_failed = False

    async def _publish(self, kind: str, data: Json) -> None:
        event = RuntimeEvent(
            kind, now(), self.session.session_id, self.session.run_id, redact(data, self.secrets)
        )
        if not self.consumer_failed:
            try:
                await self.consumer(event)
                return
            except Exception:
                self.consumer_failed = True
                print("Terminal rendering failed; continuing as plain text.", file=sys.stderr)
        if kind == "assistant_delta":
            # No terminal control characters in the plain fallback either.
            text = event.data.get("text", "")
            print("".join(c for c in text if c in "\n\t" or c.isprintable()), end="", flush=True)
        elif kind == "tool_finished":
            status = "ok" if event.data["ok"] else event.data.get("error_code") or "failed"
            print(f"\ntool: {status}", file=sys.stderr)
        elif kind in {"warning", "run_finished"}:
            detail = (
                event.data.get("reason") or event.data.get("detail") or event.data.get("outcome")
            )
            print(
                f"\n{kind}: {detail}",
                file=sys.stderr,
            )

    async def __call__(
        self,
        kind: str,
        data: Json,
        *,
        protocol_data: Json | None = None,
    ) -> int:
        if kind in DELTA_KINDS:
            key = (kind, str(data.get("call_id", "")), str(data.get("stream", "")))
            text = self.pending.get(key, ("", {}))[0] + data["text"]
            # Delay only a possible secret suffix; even cross-chunk values stay private.
            retain = max(
                (
                    length
                    for secret in self.secrets
                    for length in range(1, min(len(secret), len(text) + 1))
                    if text.endswith(secret[:length])
                ),
                default=0,
            )
            cutoff = max(0, len(text) - retain)
            for secret in self.secrets:
                position = text.find(secret)
                while position != -1:
                    if position < cutoff < position + len(secret):
                        cutoff = position
                    position = text.find(secret, position + 1)
            self.pending[key] = (text[cutoff:], data)
            if cutoff:
                await self._publish(kind, {**data, "text": text[:cutoff]})
            return 0
        for (pending_kind, _, _), (text, metadata) in self.pending.items():
            if text:
                await self._publish(pending_kind, {**metadata, "text": text})
        self.pending.clear()
        clean = redact(data, self.secrets)
        event = RuntimeEvent(kind, now(), self.session.session_id, self.session.run_id, clean)
        seq = self.writer.append(event, protocol_data)
        await self._publish(kind, clean)
        return seq
