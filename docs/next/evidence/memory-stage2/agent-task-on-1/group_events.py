"""Group FilePilot events by user while preserving the project's time ordering.

Ordering follows the established ``sort_events.py`` semantics:

* timestamps are compared as absolute instants normalized to UTC;
* ISO 8601 strings with a ``Z`` suffix, an explicit offset, no offset
  (treated as UTC), and ``datetime`` objects are supported;
* invalid or missing timestamps sort after all valid ones, stably, so their
  original relative order is kept;
* the input sequence and its events are never mutated.
"""

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

_UTC = timezone.utc
_INVALID_KEY = (1, datetime.max.replace(tzinfo=_UTC))


def parse_timestamp(value: Any) -> datetime | None:
    """Return *value* as an aware UTC ``datetime``, or ``None`` if invalid.

    Naive ``datetime`` values and ISO 8601 strings without an offset are
    interpreted as UTC, matching the project's event sorting contract.
    """

    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text[-1] in "Zz":
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None

    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        return parsed.replace(tzinfo=_UTC)
    return parsed.astimezone(_UTC)


def event_sort_key(event: Mapping[str, Any]) -> tuple[int, datetime]:
    """Return the sort key for one event using absolute UTC time.

    Valid timestamps produce ``(0, utc_datetime)``; invalid or missing ones
    produce a key that sorts after every valid timestamp.  Python's stable
    sort keeps tied and invalid events in their original relative order.
    """

    if not isinstance(event, Mapping):
        return _INVALID_KEY
    timestamp = parse_timestamp(event.get("timestamp"))
    if timestamp is None:
        return _INVALID_KEY
    return (0, timestamp)


def group_events(events):
    """Group *events* by their ``user`` field.

    Returns a ``dict`` mapping each user to a new list of the original event
    dictionaries sorted by the project's time ordering.  Users appear in
    first-seen order; every event field is preserved unchanged.
    """

    groups: dict[Any, list[Mapping[str, Any]]] = {}
    for event in events:
        user = event.get("user") if isinstance(event, Mapping) else None
        groups.setdefault(user, []).append(event)
    return {
        user: sorted(group, key=event_sort_key)
        for user, group in groups.items()
    }
