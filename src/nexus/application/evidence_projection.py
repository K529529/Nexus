"""Deterministic bounded text projection for local model evidence."""

from __future__ import annotations

_OMISSION = "\n...[output omitted]...\n"


def bounded_output(value: object, limit: int) -> tuple[str, bool]:
    """Preserve both ends of a stream within *limit* characters."""
    if limit <= len(_OMISSION):
        raise ValueError("Output evidence limit must fit the omission marker.")
    if not isinstance(value, str):
        return "", False
    if len(value) <= limit:
        return value, False
    visible = limit - len(_OMISSION)
    head = visible // 2
    tail = visible - head
    return value[:head] + _OMISSION + value[-tail:], True
