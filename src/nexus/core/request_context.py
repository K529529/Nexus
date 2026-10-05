"""Ephemeral request suffixes, kept separate from logical conversation history."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field

from nexus.core.types import Message


@dataclass
class RequestContext(Message):
    # Never persisted or used as a source-seq reference. Content alone crosses the wire.
    sections: dict[str, str] = field(default_factory=dict)


def logical_messages(messages: list[Message]) -> list[Message]:
    return [m for m in messages if not isinstance(m, RequestContext)]


def project_context(messages: list[Message], name: str, text: str | None) -> list[Message]:
    sections = {
        k: v for m in messages if isinstance(m, RequestContext) for k, v in m.sections.items()
    }
    if text:
        sections[name] = text
    else:
        sections.pop(name, None)
    result = [
        deepcopy(m) if m.role in {"system", "user"} else m for m in logical_messages(messages)
    ]
    if sections:
        result.append(RequestContext("user", "\n\n".join(sections.values()), sections=sections))
    return result
