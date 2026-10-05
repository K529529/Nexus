"""Nexus patch syntax and deterministic matching; no filesystem operations."""

from __future__ import annotations

from dataclasses import dataclass, field

MAX_ERROR_DETAIL_BYTES = 2048
MAX_ERROR_EXCERPT_BYTES = 256


def bounded_text(text: str, limit: int) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    marker = "...[truncated]"
    return raw[: limit - len(marker)].decode("utf-8", errors="ignore") + marker


class PatchError(ValueError):
    def __init__(
        self, code: str, detail: str, path: str | None = None, hunk: int | None = None
    ) -> None:
        self.code = code
        self.path = path
        self.hunk = hunk
        super().__init__(bounded_text(detail, MAX_ERROR_DETAIL_BYTES))


@dataclass
class Chunk:
    anchor: str | None
    lines: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class FilePatch:
    path: str
    kind: str
    chunks: list[Chunk] = field(default_factory=list)
    added: list[str] = field(default_factory=list)


def _error(code: str, detail: str, file: FilePatch | None, hunk: int | None = None) -> PatchError:
    prefix = bounded_text(file.path, MAX_ERROR_EXCERPT_BYTES) if file else "Patch"
    if hunk is not None:
        prefix += f" chunk {hunk}"
    return PatchError(code, f"{prefix}: {detail}", file.path if file else None, hunk)


def parse_nexus_patch(patch: str) -> list[FilePatch]:
    # Split only physical LF/CRLF lines; Unicode separators are ordinary source text.
    lines = [line.removesuffix("\r") for line in patch.split("\n")]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines or lines[0] != "*** Begin Patch" or lines[-1] != "*** End Patch":
        raise PatchError("invalid_patch", "Expected *** Begin Patch and *** End Patch boundaries")
    files: list[FilePatch] = []
    current: FilePatch | None = None
    chunk: Chunk | None = None

    def finish_chunk() -> None:
        if chunk is not None and not any(tag in "+-" for tag, _ in chunk.lines):
            raise _error(
                "invalid_patch",
                "Chunk requires '+' or '-'",
                current,
                len(current.chunks) if current else None,
            )

    def finish_file() -> None:
        finish_chunk()
        if current and current.kind == "modified" and not current.chunks:
            raise _error("invalid_patch", "Update File requires an @@ chunk", current)
        if current and current.kind == "added" and not current.added:
            raise _error("invalid_patch", "Add File requires at least one '+' line", current)

    headers = {
        "*** Update File: ": "modified",
        "*** Add File: ": "added",
        "*** Delete File: ": "deleted",
    }
    for number, line in enumerate(lines[1:-1], 2):
        header = next((prefix for prefix in headers if line.startswith(prefix)), None)
        if header:
            finish_file()
            current = FilePatch(line[len(header) :], headers[header])
            files.append(current)
            chunk = None
            continue
        hunk = len(current.chunks) if current and chunk is not None else None
        if line.startswith(
            (
                "*** Move to:",
                "rename ",
                "GIT binary patch",
                "new file mode ",
                "old mode ",
                "new mode ",
            )
        ):
            raise _error(
                "unsupported_patch", f"line {number}: unsupported capability", current, hunk
            )
        if current is None:
            raise _error("invalid_patch", f"line {number}: expected a file section", None)
        if current.kind == "modified":
            if (
                line == "@@"
                or line.startswith("@@ ")
                or (line.startswith("@@") and not line[2:].strip())
            ):
                finish_chunk()
                anchor = line[3:] if line.startswith("@@ ") else ""
                chunk = Chunk(anchor if anchor.strip() else None)
                current.chunks.append(chunk)
            elif chunk is not None and (not line or line[0] in " +-"):
                chunk.lines.append((line[0], line[1:]) if line else (" ", ""))
            else:
                raise _error(
                    "invalid_patch",
                    f"line {number}: update lines must start with ' ', '+', '-', or '@@'",
                    current,
                    hunk,
                )
        elif current.kind == "added" and line.startswith("+"):
            current.added.append(line[1:])
        else:
            raise _error(
                "invalid_patch", f"line {number}: unexpected {current.kind} file body", current
            )
    finish_file()
    if not files:
        raise PatchError("invalid_patch", "Patch requires at least one file section")
    return files


def _find_unique(source: list[str], old: list[str], start: int, label: str) -> int:
    for tolerant in (False, True):
        needle = [line.rstrip() for line in old] if tolerant else old
        haystack = [line.rstrip() for line in source] if tolerant else source
        matches = [
            i
            for i in range(start, len(source) - len(old) + 1)
            if haystack[i : i + len(old)] == needle
        ]
        if len(matches) == 1:
            return matches[0]
        if matches:
            raise PatchError(
                "patch_conflict",
                f"{label} matched {len(matches)} locations; "
                "add more unchanged context or a more specific @@ anchor",
            )
    raise PatchError("patch_conflict", f"{label} not found")


def resolve_update(before: bytes, file: FilePatch) -> bytes:
    text = before.decode("utf-8")
    # Retain original context text and terminators, including mixed newline files.
    parts = text.split("\n")
    source = [line + "\n" for line in parts[:-1]]
    if parts[-1]:
        source.append(parts[-1])
    logical = [line[:-2] if line.endswith("\r\n") else line.removesuffix("\n") for line in source]
    newline = "\r\n" if text.count("\r\n") > text.count("\n") / 2 else "\n"
    cursor = 0
    edits: list[tuple[int, int, list[str]]] = []
    for number, chunk in enumerate(file.chunks, 1):
        try:
            start = cursor
            if chunk.anchor is not None:
                label = "anchor " + bounded_text(chunk.anchor, MAX_ERROR_EXCERPT_BYTES)
                start = _find_unique(logical, [chunk.anchor], cursor, label) + 1
            old = [line for tag, line in chunk.lines if tag != "+"]
            if old:
                start = _find_unique(logical, old, start, "old block")
            elif chunk.anchor is None and source:
                raise PatchError(
                    "patch_conflict",
                    "Pure insertion into a non-empty file requires an @@ anchor "
                    "or unchanged context",
                )
            end = start + len(old)
            if edits and edits[-1][0] == start and bool(edits[-1][1] - start) != bool(old):
                raise PatchError(
                    "patch_conflict", "Insertion and replacement at the same source position"
                )
            replacement: list[str] = []
            position = start
            for tag, line in chunk.lines:
                if tag == "+":
                    replacement.append(line + newline)
                else:
                    if tag == " ":
                        replacement.append(source[position])
                    position += 1
            if edits and not old and edits[-1][0] == edits[-1][1] == start:
                edits[-1][2].extend(replacement)
            else:
                edits.append((start, end, replacement))
            cursor = end
        except PatchError as exc:
            raise _error(exc.code, str(exc), file, number) from exc
    # Forward assembly preserves patch order for coalesced insertions and never
    # searches content introduced by a previous chunk.
    result: list[str] = []
    cursor = 0
    for start, end, replacement in edits:
        result.extend(source[cursor:start])
        result.extend(replacement)
        cursor = end
    result.extend(source[cursor:])
    for index in range(len(result) - 1):
        if not result[index].endswith("\n"):
            result[index] += newline
    if result:
        if text.endswith("\n"):
            if not result[-1].endswith("\n"):
                result[-1] += newline
        else:
            last = result[-1]
            result[-1] = last[:-2] if last.endswith("\r\n") else last.removesuffix("\n")
    return "".join(result).encode("utf-8")
