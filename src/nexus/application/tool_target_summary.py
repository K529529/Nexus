"""Bounded, structural Tool targets for the local RuntimeEvent/CLI profile lane."""

from __future__ import annotations

import re
from pathlib import PurePosixPath, PureWindowsPath

from nexus.domain.tooling import JsonObject
from nexus.tools.native import _MAX_READ_LINES

_MAX_PATH_LENGTH = 120
_MAX_SUMMARY_LENGTH = 180
_SAFE_EXECUTABLE = re.compile(r"[\w .+-]{1,64}\Z")
_READ_SUMMARY = re.compile(
    r"path=(.+) start_line=([1-9][0-9]*) max_lines=([1-9][0-9]*)\Z"
)
_SHELL_SUMMARY = re.compile(
    r"executable=([\w .+-]{1,64}) argv_count=([1-9][0-9]*)\Z"
)
_PATH_TOOLS = frozenset({"apply_patch", "write_file", "edit_file"})


def target_summary(tool_name: str, arguments: JsonObject) -> str | None:
    """Project only approved target fields; never include content or argv values."""

    if tool_name == "read_file":
        path = _relative_path(arguments.get("path"))
        if path is None:
            return None
        start = arguments.get("start_line", 1)
        count = arguments.get("max_lines", _MAX_READ_LINES)
        if not _valid_int(start, 1, 2**31 - 1) or not _valid_int(
            count, 1, _MAX_READ_LINES
        ):
            return f"path={path}"
        return f"path={path} start_line={start} max_lines={count}"
    if tool_name in _PATH_TOOLS:
        path = _relative_path(arguments.get("path"))
        return None if path is None else f"path={path}"
    if tool_name == "shell":
        argv = arguments.get("argv")
        if not isinstance(argv, list) or not argv or not all(
            isinstance(item, str) and item for item in argv
        ):
            return None
        executable = re.split(r"[/\\]", argv[0])[-1]
        if not _SAFE_EXECUTABLE.fullmatch(executable):
            return None
        return f"executable={executable} argv_count={len(argv)}"
    return None


def safe_target_summary(tool_name: str, summary: str | None) -> str | None:
    """Recheck a RuntimeEvent before its target reaches terminal output."""

    if not isinstance(summary, str) or len(summary) > _MAX_SUMMARY_LENGTH:
        return None
    if tool_name == "read_file":
        matched = _READ_SUMMARY.fullmatch(summary)
        if matched is not None:
            path, start, count = matched.groups()
            if (
                _relative_path(path) == path
                and _valid_int(int(start), 1, 2**31 - 1)
                and _valid_int(int(count), 1, _MAX_READ_LINES)
            ):
                return summary
        if summary.startswith("path=") and _relative_path(summary[5:]) == summary[5:]:
            return summary
        return None
    if tool_name in _PATH_TOOLS:
        if summary.startswith("path=") and _relative_path(summary[5:]) == summary[5:]:
            return summary
        return None
    if tool_name == "shell":
        matched = _SHELL_SUMMARY.fullmatch(summary)
        return summary if matched is not None else None
    return None


def _relative_path(value: object) -> str | None:
    if not isinstance(value, str) or not 0 < len(value) <= _MAX_PATH_LENGTH:
        return None
    if not value.isprintable() or any(char in '<>:"|?*' for char in value):
        return None
    windows = PureWindowsPath(value)
    posix = PurePosixPath(value)
    if (
        windows.anchor
        or posix.is_absolute()
        or ".." in windows.parts
        or ".." in posix.parts
        or not windows.parts
    ):
        return None
    return "/".join(windows.parts)


def _valid_int(value: object, minimum: int, maximum: int) -> bool:
    return type(value) is int and minimum <= value <= maximum
