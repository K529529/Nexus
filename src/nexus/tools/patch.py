"""Strict workspace-confined unified diffs; atomic per file, not per batch."""

from __future__ import annotations

import difflib
import hashlib
import os
import re
import stat
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath

from nexus.core.types import Emit, ExecutionContext, Json, ToolResult, json_text

MAX_BYTES = 1024 * 1024
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?:.*)$")


class PatchError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        super().__init__(detail)


@dataclass
class Hunk:
    start: int
    old_count: int
    new_count: int
    lines: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class Change:
    path: str
    kind: str
    hunks: list[Hunk] = field(default_factory=list)
    before: bytes | None = None
    after: bytes | None = None
    mode: int | None = None


def _name(header: str, prefix: str) -> str | None:
    name = header.split("\t", 1)[0]
    if name == "/dev/null":
        return None
    if not name.startswith(prefix):
        raise PatchError("unsupported_patch", "Use a/ and b/ relative path headers")
    return name[len(prefix) :]


def parse_patch(patch: str) -> list[Change]:
    if "\0" in patch:
        raise PatchError("unsupported_patch", "Binary patch content")
    if len(patch.encode("utf-8")) > MAX_BYTES:
        raise PatchError("patch_too_large", "Patch exceeds 1 MiB")
    lines = patch.splitlines(keepends=True)
    changes: list[Change] = []
    seen: set[str] = set()
    index = 0
    while index < len(lines):
        line = lines[index].rstrip("\r\n")
        if line.startswith(("diff --git ", "index ")):
            index += 1
            continue
        if not line.startswith("--- ") or index + 1 >= len(lines):
            raise PatchError("unsupported_patch", "Expected unified diff file headers")
        following = lines[index + 1].rstrip("\r\n")
        if not following.startswith("+++ "):
            raise PatchError("invalid_patch", "Missing +++ header")
        old, new = _name(line[4:], "a/"), _name(following[4:], "b/")
        if (old is None and new is None) or (old and new and old != new):
            raise PatchError("unsupported_patch", "Rename or invalid /dev/null headers")
        path = old or new or ""
        if path in seen:
            raise PatchError("invalid_patch", "Duplicate file section")
        seen.add(path)
        change = Change(path, "added" if old is None else "deleted" if new is None else "modified")
        changes.append(change)
        index += 2
        while index < len(lines) and lines[index].startswith("@@ "):
            match = HUNK.fullmatch(lines[index].rstrip("\r\n"))
            if match is None:
                raise PatchError("invalid_patch", "Invalid hunk header")
            start, count, _, new_count = match.groups()
            hunk = Hunk(int(start), int(count) if count else 1, int(new_count) if new_count else 1)
            index += 1
            old_seen = new_seen = 0
            while index < len(lines):
                body = lines[index]
                if body.rstrip("\r\n") == "\\ No newline at end of file":
                    if not hunk.lines:
                        raise PatchError("invalid_patch", "Unattached no-newline marker")
                    tag, text = hunk.lines[-1]
                    hunk.lines[-1] = (tag, text.removesuffix("\n"))
                    index += 1
                    continue
                if old_seen == hunk.old_count and new_seen == hunk.new_count:
                    break
                if not body or body[0] not in " +-":
                    raise PatchError("invalid_patch", "Invalid hunk body")
                tag = body[0]
                text = body[1:].rstrip("\r\n") + "\n"
                hunk.lines.append((tag, text))
                old_seen += tag != "+"
                new_seen += tag != "-"
                if old_seen > hunk.old_count or new_seen > hunk.new_count:
                    raise PatchError("invalid_patch", "Hunk counts mismatch")
                index += 1
            if old_seen != hunk.old_count or new_seen != hunk.new_count:
                raise PatchError("invalid_patch", "Incomplete hunk")
            change.hunks.append(hunk)
        if not change.hunks:
            raise PatchError("unsupported_patch", "A file section requires text hunks")
    if not changes:
        raise PatchError("invalid_patch", "Empty patch")
    return changes


def checked_path(workspace: Path, name: str) -> Path:
    normalized = name.replace("\\", "/")
    if (
        not normalized
        or normalized.startswith("/")
        or PureWindowsPath(name).drive
        or ":" in normalized
        or any(p in {"", ".", ".."} for p in normalized.split("/"))
    ):
        raise PatchError("path_escape", "Expected a workspace-relative file path")
    root = workspace.resolve()
    current = root
    for part in normalized.split("/"):
        current /= part
        if current.is_symlink() or current.is_junction():
            raise PatchError("path_escape", "Symlink/junction paths are not supported")
        if not current.resolve().is_relative_to(root):
            raise PatchError("path_escape", "Path resolves outside workspace")
    return current


def _logical(line: str) -> str:
    return line.replace("\r\n", "\n")


def apply_hunks(before: bytes, hunks: list[Hunk]) -> bytes:
    text = before.decode("utf-8")
    source = text.splitlines(keepends=True)
    logical = [_logical(line) for line in source]
    newline = "\r\n" if text.count("\r\n") > text.count("\n") / 2 else "\n"
    result: list[str] = []
    cursor = 0
    for hunk in hunks:
        old = [line for tag, line in hunk.lines if tag != "+"]
        expected = hunk.start if hunk.old_count == 0 else hunk.start - 1
        if expected < 0:
            raise PatchError("invalid_patch", "Invalid hunk start")
        if cursor <= expected <= len(source) and logical[expected : expected + len(old)] == old:
            position = expected
        elif old:
            candidates = [
                i
                for i in range(cursor, len(source) - len(old) + 1)
                if logical[i : i + len(old)] == old
            ]
            if len(candidates) != 1:
                raise PatchError("patch_conflict", "Old-side match is absent or ambiguous")
            position = candidates[0]
        else:
            raise PatchError("patch_conflict", "Insertion position is outside the file")
        result.extend(source[cursor:position])
        cursor = position
        for tag, line in hunk.lines:
            if tag == " ":
                result.append(source[cursor])
                cursor += 1
            elif tag == "-":
                cursor += 1
            else:
                result.append(line.replace("\n", newline))
    result.extend(source[cursor:])
    after = "".join(result).encode("utf-8")
    if len(after) > MAX_BYTES:
        raise PatchError("file_too_large", "Result exceeds 1 MiB")
    return after


def _hash(data: bytes | None) -> str | None:
    return hashlib.sha256(data).hexdigest() if data is not None else None


def _commit(path: Path, change: Change) -> None:
    if change.after is None:
        path.unlink()
        return
    descriptor, name = tempfile.mkstemp(prefix=".nexus-patch-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(change.after)
        if change.mode is not None:
            temporary.chmod(change.mode)
        if change.kind == "added":
            # link fails if the destination appeared after preflight; never clobber it.
            os.link(temporary, path)
        else:
            os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _fact(change: Change) -> Json:
    before = (change.before or b"").decode("utf-8").splitlines(keepends=True)
    after = (change.after or b"").decode("utf-8").splitlines(keepends=True)
    diff = list(
        difflib.unified_diff(
            before,
            after,
            fromfile="/dev/null" if change.before is None else f"a/{change.path}",
            tofile="/dev/null" if change.after is None else f"b/{change.path}",
        )
    )
    rendered = "".join(
        line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in diff
    )
    return {
        "path": change.path,
        "status": change.kind,
        "added_lines": sum(s.startswith("+") and not s.startswith("+++") for s in diff),
        "deleted_lines": sum(s.startswith("-") and not s.startswith("---") for s in diff),
        "before_hash": _hash(change.before),
        "after_hash": _hash(change.after),
        "diff": rendered,
    }


async def apply_patch(arguments: Json, context: ExecutionContext, emit: Emit) -> ToolResult:
    started = time.monotonic()
    facts: list[Json] = []
    created: list[str] = []
    error: str | None = None
    detail = ""
    failed_file: str | None = None
    try:
        if set(arguments) != {"patch"} or not isinstance(arguments["patch"], str):
            raise PatchError("invalid_arguments", "Expected only patch: string")
        changes = parse_patch(arguments["patch"])
        normalized: set[Path] = set()
        for change in changes:
            failed_file = change.path
            path = checked_path(context.workspace, change.path)
            if path in normalized:
                raise PatchError("invalid_patch", "Duplicate resolved target")
            normalized.add(path)
            if change.kind == "added":
                if path.exists():
                    raise PatchError("patch_conflict", "New file already exists")
            else:
                if not path.is_file() or path.stat().st_size > MAX_BYTES:
                    raise PatchError("patch_conflict", "Target missing, non-file or over 1 MiB")
                change.before = path.read_bytes()
                if b"\0" in change.before:
                    raise PatchError("unsupported_patch", "Binary target")
                change.mode = stat.S_IMODE(path.stat().st_mode)
            change.after = apply_hunks(change.before or b"", change.hunks)
            if change.kind == "deleted":
                if change.after:
                    raise PatchError("patch_conflict", "Deletion does not remove the whole file")
                change.after = None
        for change in changes:
            failed_file = change.path
            path = checked_path(context.workspace, change.path)
            current = path.read_bytes() if path.exists() else None
            if _hash(current) != _hash(change.before):
                raise PatchError("concurrent_change", "File changed after preflight")
            if change.before == change.after:
                continue
            parents: list[Path] = []
            parent = path.parent
            while not parent.exists():
                parents.append(parent)
                parent = parent.parent
            for parent in reversed(parents):
                parent.mkdir()
                created.append(parent.relative_to(context.workspace).as_posix())
            checked_path(context.workspace, change.path)
            _commit(path, change)
            facts.append(_fact(change))
        failed_file = None
    except PatchError as exc:
        error, detail = exc.code, str(exc)
    except UnicodeError:
        error, detail = "unsupported_patch", "Patch and target must be UTF-8 text"
    except OSError as exc:
        error, detail = "patch_io_error", type(exc).__name__
    # Bound the actual result, retaining counts/hashes and explicit omissions.
    remaining = context.output_limit_bytes
    visible: list[Json] = []
    truncated = False
    for fact in facts:
        raw = fact.pop("diff").encode("utf-8")
        cost = len(json_text(fact).encode("utf-8")) + 64
        if cost > remaining:
            truncated = True
            continue
        allowed = max(0, remaining - cost)
        fact["diff"] = raw[:allowed].decode("utf-8", errors="ignore")
        fact["truncated"] = len(raw) > allowed
        truncated |= fact["truncated"]
        remaining -= cost + min(len(raw), allowed)
        visible.append(fact)
    data = {
        "files": visible,
        "changed_files": len(facts),
        "omitted_files": len(facts) - len(visible),
        "created_directories": created,
        "partial": bool(error and (facts or created)),
        "no_changes": not error and not facts,
        "failed_file": failed_file,
        "detail": detail,
    }
    return ToolResult(
        context.call_id,
        error is None,
        data,
        error,
        int((time.monotonic() - started) * 1000),
        truncated,
    )
