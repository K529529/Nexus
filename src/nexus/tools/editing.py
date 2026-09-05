"""Atomic text-only Day 4 editing Tools."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from nexus.domain.tooling import PolicyDecision, RiskLevel, ToolError, ToolInvocation, ToolResult
from nexus.errors import NexusError, ToolExecutionError
from nexus.security.workspace import WorkspaceGuard

_MAX_BYTES = 1_048_576
_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: .*)?$")


@dataclass(frozen=True, slots=True)
class _Line:
    text: str
    ending: str


class PatchTool:
    name = "apply_patch"

    def __init__(self, guard: WorkspaceGuard) -> None:
        self._guard = guard

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        started = time.perf_counter()
        try:
            path, patch = _two_string_arguments(invocation, "patch")
            try:
                target = self._guard.resolve_existing(path, require_file=True)
            except NexusError as exc:
                if exc.code == "TOOL_EXECUTION_ERROR":
                    raise ToolExecutionError(
                        "The patch target does not exist.", code="FILE_NOT_FOUND"
                    ) from exc
                raise
            before = _read_bounded(target)
            after = _apply_unified_patch(path, before, patch)
            if after == before:
                raise ToolExecutionError(
                    "The patch does not change file content.", code="PATCH_NO_CHANGES"
                )
            encoded = after.encode("utf-8")
            if len(encoded) > _MAX_BYTES:
                raise ToolExecutionError(
                    "The patched file exceeds the Day 4 size limit.", code="INVALID_PATCH"
                )
            _atomic_replace(target, encoded)
            return _success(
                invocation,
                {
                    "path": self._guard.relative_display(target),
                    "change_kind": "MODIFIED",
                    "bytes_before": len(before.encode("utf-8")),
                    "bytes_after": len(encoded),
                    "sha256_before": _sha256(before.encode("utf-8")),
                    "sha256_after": _sha256(encoded),
                },
                started,
            )
        except NexusError as exc:
            return _failure(invocation, exc, started)
        except (OSError, TypeError, UnicodeError, ValueError):
            return _failure(
                invocation,
                ToolExecutionError("The patch is invalid.", code="INVALID_PATCH"),
                started,
            )


class WriteFileTool:
    name = "write_file"

    def __init__(self, guard: WorkspaceGuard) -> None:
        self._guard = guard

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        started = time.perf_counter()
        try:
            path, content = _two_string_arguments(invocation, "content", allow_empty=True)
            encoded = content.encode("utf-8")
            if len(encoded) > _MAX_BYTES:
                raise ToolExecutionError(
                    "The new file exceeds the Day 4 size limit.", code="INVALID_TOOL_ARGUMENTS"
                )
            target = self._guard.resolve_for_creation(path)
            _atomic_create(target, encoded)
            return _success(
                invocation,
                {
                    "path": self._guard.relative_display(target),
                    "change_kind": "ADDED",
                    "bytes_written": len(encoded),
                    "sha256": _sha256(encoded),
                },
                started,
            )
        except NexusError as exc:
            return _failure(invocation, exc, started)
        except (OSError, TypeError, UnicodeError, ValueError):
            return _failure(
                invocation,
                ToolExecutionError(
                    "The Tool arguments are invalid.", code="INVALID_TOOL_ARGUMENTS"
                ),
                started,
            )


def _two_string_arguments(
    invocation: ToolInvocation,
    body_key: str,
    *,
    allow_empty: bool = False,
) -> tuple[str, str]:
    if set(invocation.arguments) != {"path", body_key}:
        raise ValueError("Unexpected editing Tool arguments.")
    path = invocation.arguments.get("path")
    body = invocation.arguments.get(body_key)
    if not isinstance(path, str) or not path:
        raise ValueError("path must be a non-empty string.")
    if not isinstance(body, str) or (not allow_empty and not body):
        raise ValueError(f"{body_key} must be a string with valid content.")
    return path, body


def _read_bounded(path: Path) -> str:
    data = path.read_bytes()
    if len(data) > _MAX_BYTES or b"\x00" in data:
        raise ToolExecutionError(
            "The patch target is not supported UTF-8 text.", code="INVALID_PATCH"
        )
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ToolExecutionError(
            "The patch target is not valid UTF-8 text.", code="INVALID_PATCH"
        ) from exc


def _apply_unified_patch(path: str, source: str, patch: str) -> str:
    if len(patch.encode("utf-8")) > _MAX_BYTES:
        raise ToolExecutionError("The patch exceeds the Day 4 size limit.", code="INVALID_PATCH")
    patch_lines = patch.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    if len(patch_lines) < 3:
        raise ToolExecutionError("The patch has no valid hunk.", code="INVALID_PATCH")
    if patch_lines[0] != f"--- a/{path}" or patch_lines[1] != f"+++ b/{path}":
        raise ToolExecutionError("Patch headers do not match the target.", code="INVALID_PATCH")
    if any(line.startswith(("--- ", "+++ ")) for line in patch_lines[2:]):
        raise ToolExecutionError("Multi-file patches are not allowed.", code="INVALID_PATCH")

    source_lines = _split_source(source)
    output: list[_Line] = []
    source_index = 0
    patch_index = 2
    saw_hunk = False
    default_ending = "\r\n" if "\r\n" in source else "\n"
    while patch_index < len(patch_lines):
        match = _HUNK_HEADER.match(patch_lines[patch_index])
        if match is None:
            raise ToolExecutionError(
                "The patch contains invalid hunk syntax.",
                code="INVALID_PATCH",
            )
        saw_hunk = True
        old_start = int(match.group(1))
        old_count = int(match.group(2) or "1")
        new_count = int(match.group(4) or "1")
        hunk_source_index = 0 if old_start == 0 else old_start - 1
        if hunk_source_index < source_index or hunk_source_index > len(source_lines):
            raise ToolExecutionError("Patch hunk position is invalid.", code="PATCH_CONFLICT")
        output.extend(source_lines[source_index:hunk_source_index])
        source_index = hunk_source_index
        patch_index += 1
        old_seen = 0
        new_seen = 0
        previous_prefix: str | None = None
        previous_source_line: _Line | None = None
        while patch_index < len(patch_lines) and not patch_lines[patch_index].startswith("@@ "):
            line = patch_lines[patch_index]
            if line == "\\ No newline at end of file":
                if previous_prefix not in {" ", "+", "-"}:
                    raise ToolExecutionError("Invalid no-newline marker.", code="INVALID_PATCH")
                if previous_prefix in {" ", "+"}:
                    output[-1] = _Line(output[-1].text, "")
                if previous_prefix in {" ", "-"} and (
                    previous_source_line is None or previous_source_line.ending
                ):
                    raise ToolExecutionError(
                        "No-newline marker conflicts with source.", code="PATCH_CONFLICT"
                    )
                patch_index += 1
                previous_prefix = None
                continue
            if not line or line[0] not in {" ", "+", "-"}:
                raise ToolExecutionError("Patch hunk body is invalid.", code="INVALID_PATCH")
            prefix, text = line[0], line[1:]
            previous_prefix = prefix
            previous_source_line = None
            if prefix in {" ", "-"}:
                if source_index >= len(source_lines) or source_lines[source_index].text != text:
                    raise ToolExecutionError("Patch context does not match.", code="PATCH_CONFLICT")
                previous_source_line = source_lines[source_index]
                if prefix == " ":
                    output.append(source_lines[source_index])
                    new_seen += 1
                source_index += 1
                old_seen += 1
            else:
                output.append(_Line(text, default_ending))
                new_seen += 1
            patch_index += 1
        if old_seen != old_count or new_seen != new_count:
            raise ToolExecutionError("Patch hunk counts do not match.", code="INVALID_PATCH")
    if not saw_hunk:
        raise ToolExecutionError("The patch has no hunk.", code="INVALID_PATCH")
    output.extend(source_lines[source_index:])
    return "".join(line.text + line.ending for line in output)


def _split_source(content: str) -> list[_Line]:
    result: list[_Line] = []
    for value in content.splitlines(keepends=True):
        if value.endswith("\r\n"):
            result.append(_Line(value[:-2], "\r\n"))
        elif value.endswith(("\n", "\r")):
            result.append(_Line(value[:-1], value[-1]))
        else:
            result.append(_Line(value, ""))
    return result


def _atomic_replace(target: Path, data: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=".nexus-patch-", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        shutil.copymode(target, temporary)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_create(target: Path, data: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=".nexus-write-", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, target)
        except FileExistsError as exc:
            raise ToolExecutionError(
                "The target file already exists.", code="FILE_ALREADY_EXISTS"
            ) from exc
    finally:
        temporary.unlink(missing_ok=True)


def _success(
    invocation: ToolInvocation,
    output: dict[str, object],
    started: float,
) -> ToolResult:
    return ToolResult(
        invocation.invocation_id,
        invocation.tool_name,
        True,
        output,
        None,
        RiskLevel.WRITE,
        PolicyDecision.ALLOWED,
        None,
        _duration_ms(started),
    )


def _failure(invocation: ToolInvocation, error: NexusError, started: float) -> ToolResult:
    dangerous = error.code == "WORKSPACE_PATH_DENIED"
    return ToolResult(
        invocation.invocation_id,
        invocation.tool_name,
        False,
        None,
        ToolError(error.code, str(error), error.retryable),
        RiskLevel.DANGEROUS if dangerous else RiskLevel.WRITE,
        PolicyDecision.DENIED if dangerous else PolicyDecision.ALLOWED,
        None,
        _duration_ms(started),
    )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _duration_ms(started: float) -> int:
    return max(0, round((time.perf_counter() - started) * 1000))
