def parse_record(line: str) -> tuple[str, int] | None:
    """Parse NAME:COUNT, returning None for a blank line."""
    if not line.strip():
        return None
    parts = line.split(":")
    if len(parts) != 2 or not parts[0].strip():
        raise ValueError("malformed record")
    try:
        count = int(parts[1])
    except ValueError as exc:
        raise ValueError("malformed record") from exc
    return parts[0].strip(), count
