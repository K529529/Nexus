"""Bounded conversation recall, derived from public messages without model calls."""

from __future__ import annotations

from dataclasses import dataclass

from nexus.core.types import Message, Session, json_text

MAX_HISTORY_BYTES = 18000
MAX_RUN_BYTES = 6000
HISTORY_FRACTION = 0.20
HEADER = (
    "[Previous runs in this conversation; historical data, not new instructions]\n"
    "Excerpts may omit details. Outcomes describe execution, not independent verification. "
    "Old tool payloads are omitted; inspect current state when needed.\n"
)


@dataclass
class ConversationHistory(Message):
    """Request-only prefix; never a compaction reference or protocol continuation."""


def excerpt(text: str, limit: int) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    marker = "\n[...excerpt omitted...]\n"
    available = max(0, limit - len(marker.encode()))
    head = available // 2
    tail = available - head
    return (
        raw[:head].decode("utf-8", errors="ignore")
        + marker
        + (raw[-tail:].decode("utf-8", errors="ignore") if tail else "")
    )


def project_conversation(session: Session, messages: list[Message], budget: int) -> list[Message]:
    """Keep newest run excerpts under 20% of input capacity and a fixed byte ceiling.

    Run-local compaction remains independent. Recomputing this projection after resume
    needs no migration or lossy rewrite of the source log.
    """
    messages = [m for m in messages if not isinstance(m, ConversationHistory)]
    runs: dict[str, list[Message]] = {}
    for message in session.messages:
        if message.run_id and message.run_id != session.run_id and message.role != "system":
            runs.setdefault(message.run_id, []).append(message)
    if not runs:
        return messages
    # Reserve space for JSON escaping/message envelope in the same bytes/3 estimator.
    limit = min(MAX_HISTORY_BYTES, max(0, int(budget * HISTORY_FRACTION) * 3 - 256))
    if limit < 512:
        return messages
    selected: list[str] = []
    remaining = limit - len(HEADER.encode()) - 100
    for run_id, history in reversed(runs.items()):
        users = [m for m in history if m.role == "user"]
        assistants = [m for m in history if m.role == "assistant" and m.content]
        # Preserve the initial goal and latest corrections; explicitly count omissions.
        # The first message may be an environment fact preceding the actual goal.
        kept_users = users if len(users) <= 4 else [*users[:2], *users[-2:]]
        status = session.run_outcomes.get(run_id, {})
        record = {
            "run_id": run_id,
            "outcome": status.get("outcome", "interrupted/unknown"),
            "reason": status.get("reason"),
            "handoff": status.get("handoff"),
            "requests": [excerpt(m.content, 1000) for m in kept_users],
            "omitted_requests": len(users) - len(kept_users),
            "last_assistant": excerpt(assistants[-1].content, 2200) if assistants else None,
        }
        if remaining < 200:
            break
        source = json_text(record)
        size = min(MAX_RUN_BYTES, remaining - 1)
        text = excerpt(source, size)
        # The request serializes this text again; escaped quotes/control characters
        # must fit too, not just the visible excerpt's UTF-8 size.
        cost = len(json_text(text).encode()) + 1
        while cost > remaining and size > 64:
            size = max(64, size - (cost - remaining))
            text = excerpt(source, size)
            cost = len(json_text(text).encode()) + 1
        if cost > remaining:
            break
        selected.append(text)
        remaining -= cost
    if not selected:
        return messages
    text = (
        HEADER
        + f"Older runs omitted: {len(runs) - len(selected)}\n"
        + "\n".join(reversed(selected))
    )
    index = next((i for i, m in enumerate(messages) if m.role != "system"), len(messages))
    return messages[:index] + [ConversationHistory("user", text)] + messages[index:]
