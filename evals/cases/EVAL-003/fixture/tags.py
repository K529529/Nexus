def normalize_tags(values: list[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    for value in values:
        cleaned = value.strip().casefold()
        if cleaned and cleaned not in normalized:
            normalized.append(cleaned)
    return tuple(sorted(normalized))
