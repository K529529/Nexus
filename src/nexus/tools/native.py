"""Day 3 native repository inspection, shell, and read-only Git tools."""

from __future__ import annotations

import fnmatch
import os
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict
from pathlib import Path

from nexus.domain.ports.tooling import SandboxExecutor, Tool
from nexus.domain.tooling import (
    JsonObject,
    PolicyDecision,
    RiskLevel,
    SandboxRequest,
    SandboxResult,
    ToolError,
    ToolInvocation,
    ToolResult,
)
from nexus.errors import NexusError, PermissionDeniedError, ToolExecutionError
from nexus.security.executables import TrustedExecutables
from nexus.security.git_commands import diff_argv, log_argv, status_argv
from nexus.security.workspace import WorkspaceGuard
from nexus.tools.editing import PatchTool, WriteFileTool

_MAX_FILE_BYTES = 1_048_576
_MAX_TEXT_OUTPUT_BYTES = 1_048_576
_MAX_PATH_RESULTS = 200
_MAX_LEXICAL_RESULTS = 200
_MAX_READ_LINES = 400
_EXCLUDED_SCAN_DIRECTORIES = frozenset(
    {
        ".git",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        "dist",
        "build",
        "target",
        "coverage",
        "htmlcov",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".uv-cache",
        ".pytest-tmp",
        ".tmp-docker-config",
    }
)


class ListFilesTool:
    name = "list_files"

    def __init__(self, guard: WorkspaceGuard) -> None:
        self._guard = guard

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        started = time.perf_counter()
        try:
            path, recursive = _list_arguments(invocation.arguments)
            root = self._guard.resolve_existing(path, require_directory=True)
            paths, truncated = _list_paths(self._guard, root, recursive=recursive)
            return _success(invocation, {"paths": paths, "truncated": truncated}, started)
        except NexusError as exc:
            return _nexus_failure(invocation, exc, started)
        except (TypeError, ValueError):
            return _invalid_arguments(invocation, started)


class SearchFilesTool:
    name = "search_files"

    def __init__(self, guard: WorkspaceGuard) -> None:
        self._guard = guard

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        started = time.perf_counter()
        try:
            _require_keys(invocation.arguments, required={"pattern"}, optional={"path"})
            pattern = _required_string(invocation.arguments, "pattern")
            path = _optional_string(invocation.arguments, "path", ".")
            root = self._guard.resolve_existing(path, require_directory=True)
            matches, truncated = _search_paths(self._guard, root, pattern)
            return _success(
                invocation,
                {"paths": matches, "truncated": truncated},
                started,
            )
        except NexusError as exc:
            return _nexus_failure(invocation, exc, started)
        except (TypeError, ValueError):
            return _invalid_arguments(invocation, started)


class ReadFileTool:
    name = "read_file"

    def __init__(self, guard: WorkspaceGuard) -> None:
        self._guard = guard

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        started = time.perf_counter()
        try:
            _require_keys(
                invocation.arguments,
                required={"path"},
                optional={"start_line", "max_lines"},
            )
            path = _required_string(invocation.arguments, "path")
            start_line = _bounded_int(invocation.arguments, "start_line", 1, 1, 2**31 - 1)
            max_lines = _bounded_int(
                invocation.arguments,
                "max_lines",
                _MAX_READ_LINES,
                1,
                _MAX_READ_LINES,
            )
            resolved = self._guard.resolve_existing(path)
            if not resolved.is_file():
                raise ToolExecutionError(
                    "The requested path is not supported text.",
                    code="UNSUPPORTED_FILE",
                )
            content = _read_text_file(resolved)
            lines = content.splitlines(keepends=True)
            selected = lines[start_line - 1 : start_line - 1 + max_lines]
            rendered = "".join(selected)
            rendered_bytes = rendered.encode("utf-8")
            if len(rendered_bytes) > _MAX_TEXT_OUTPUT_BYTES:
                rendered = rendered_bytes[:_MAX_TEXT_OUTPUT_BYTES].decode(
                    "utf-8", errors="ignore"
                )
            end_line = start_line + len(selected) - 1
            truncated = start_line - 1 + len(selected) < len(lines)
            return _success(
                invocation,
                {
                    "path": self._guard.relative_display(resolved),
                    "start_line": start_line,
                    "end_line": end_line,
                    "content": rendered,
                    "truncated": truncated,
                },
                started,
            )
        except NexusError as exc:
            return _nexus_failure(invocation, exc, started)
        except (TypeError, ValueError):
            return _invalid_arguments(invocation, started)


class LexicalSearchTool:
    name = "lexical_search"

    def __init__(
        self,
        guard: WorkspaceGuard,
        allow_path: Callable[[str], bool] | None = None,
    ) -> None:
        self._guard = guard
        self._allow_path = allow_path

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        started = time.perf_counter()
        try:
            _require_keys(
                invocation.arguments,
                required={"pattern"},
                optional={"path", "case_sensitive"},
            )
            pattern = _required_string(invocation.arguments, "pattern")
            path = _optional_string(invocation.arguments, "path", ".")
            case_sensitive = _optional_bool(invocation.arguments, "case_sensitive", True)
            root = self._guard.resolve_existing(path)
            matches, truncated = _lexical_matches(
                self._guard,
                root,
                pattern,
                case_sensitive=case_sensitive,
                allow_path=self._allow_path,
            )
            return _success(
                invocation,
                {"matches": matches, "truncated": truncated},
                started,
            )
        except NexusError as exc:
            return _nexus_failure(invocation, exc, started)
        except (TypeError, ValueError):
            return _invalid_arguments(invocation, started)


class ShellTool:
    name = "shell"

    def __init__(
        self,
        guard: WorkspaceGuard,
        sandbox: SandboxExecutor,
        executables: TrustedExecutables,
    ) -> None:
        self._guard = guard
        self._sandbox = sandbox
        self._executables = executables

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        started = time.perf_counter()
        try:
            _require_keys(
                invocation.arguments,
                required={"argv"},
                optional={"cwd", "timeout_seconds"},
            )
            argv = _string_list(invocation.arguments.get("argv"))
            cwd = _optional_string(invocation.arguments, "cwd", ".")
            timeout = _bounded_float(
                invocation.arguments,
                "timeout_seconds",
                30.0,
                0.0,
                300.0,
            )
            self._guard.resolve_existing(cwd, require_directory=True)
            request = SandboxRequest(
                operation=self.name,
                argv=self._executables.normalize_argv(argv),
                cwd=cwd,
                timeout_seconds=timeout,
            )
            return _from_sandbox(
                invocation,
                await self._sandbox.execute(request),
                started,
            )
        except NexusError as exc:
            return _nexus_failure(invocation, exc, started)
        except (TypeError, ValueError):
            return _invalid_arguments(invocation, started)


class GitStatusTool:
    name = "git_status"

    def __init__(
        self,
        sandbox: SandboxExecutor,
        executables: TrustedExecutables,
    ) -> None:
        self._sandbox = sandbox
        self._executables = executables

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        started = time.perf_counter()
        try:
            _require_keys(invocation.arguments, required=set(), optional=set())
            git = _require_git(self._executables)
            request = SandboxRequest(self.name, status_argv(git), ".", 30.0)
            return _from_git_sandbox(
                invocation,
                await self._sandbox.execute(request),
                started,
            )
        except NexusError as exc:
            return _nexus_failure(invocation, exc, started)
        except (TypeError, ValueError):
            return _invalid_arguments(invocation, started)


class GitDiffTool:
    name = "git_diff"

    def __init__(
        self,
        sandbox: SandboxExecutor,
        executables: TrustedExecutables,
    ) -> None:
        self._sandbox = sandbox
        self._executables = executables

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        started = time.perf_counter()
        try:
            _require_keys(invocation.arguments, required=set(), optional={"staged"})
            staged = _optional_bool(invocation.arguments, "staged", False)
            git = _require_git(self._executables)
            request = SandboxRequest(self.name, diff_argv(git, staged=staged), ".", 30.0)
            return _from_git_sandbox(
                invocation,
                await self._sandbox.execute(request),
                started,
            )
        except NexusError as exc:
            return _nexus_failure(invocation, exc, started)
        except (TypeError, ValueError):
            return _invalid_arguments(invocation, started)


class GitLogTool:
    name = "git_log"

    def __init__(
        self,
        sandbox: SandboxExecutor,
        executables: TrustedExecutables,
    ) -> None:
        self._sandbox = sandbox
        self._executables = executables

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        started = time.perf_counter()
        try:
            _require_keys(invocation.arguments, required=set(), optional={"max_entries"})
            max_entries = _bounded_int(
                invocation.arguments,
                "max_entries",
                20,
                1,
                100,
            )
            git = _require_git(self._executables)
            request = SandboxRequest(
                self.name,
                log_argv(git, max_entries=max_entries),
                ".",
                30.0,
            )
            return _from_git_sandbox(
                invocation,
                await self._sandbox.execute(request),
                started,
            )
        except NexusError as exc:
            return _nexus_failure(invocation, exc, started)
        except (TypeError, ValueError):
            return _invalid_arguments(invocation, started)


def build_native_tools(
    guard: WorkspaceGuard,
    sandbox: SandboxExecutor,
    executables: TrustedExecutables,
    *,
    allow_lexical_path: Callable[[str], bool] | None = None,
) -> list[Tool]:
    return [
        ListFilesTool(guard),
        SearchFilesTool(guard),
        ReadFileTool(guard),
        LexicalSearchTool(guard, allow_lexical_path),
        PatchTool(guard),
        WriteFileTool(guard),
        ShellTool(guard, sandbox, executables),
        GitStatusTool(sandbox, executables),
        GitDiffTool(sandbox, executables),
        GitLogTool(sandbox, executables),
    ]


def _list_arguments(arguments: JsonObject) -> tuple[str, bool]:
    _require_keys(arguments, required=set(), optional={"path", "recursive"})
    return (
        _optional_string(arguments, "path", "."),
        _optional_bool(arguments, "recursive", True),
    )


def _list_paths(
    guard: WorkspaceGuard,
    root: Path,
    *,
    recursive: bool,
) -> tuple[list[str], bool]:
    if _is_excluded_scan_path(guard, root):
        return [], False
    results = sorted(
        guard.relative_display(file_path)
        for file_path in _iter_files(root, recursive=recursive)
    )
    return results[:_MAX_PATH_RESULTS], len(results) > _MAX_PATH_RESULTS


def _search_paths(
    guard: WorkspaceGuard,
    root: Path,
    pattern: str,
) -> tuple[list[str], bool]:
    if _is_excluded_scan_path(guard, root):
        return [], False
    matches = sorted(
        relative
        for file_path in _iter_files(root, recursive=True)
        if fnmatch.fnmatchcase(
            relative := guard.relative_display(file_path),
            pattern,
        )
    )
    return matches[:_MAX_PATH_RESULTS], len(matches) > _MAX_PATH_RESULTS


def _iter_files(root: Path, *, recursive: bool) -> Iterator[Path]:
    if root.is_file():
        yield root
        return
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        directories[:] = sorted(
            name
            for name in directories
            if not _is_excluded_scan_directory(name)
            and not _is_link_or_junction(current_path / name)
        )
        for name in sorted(files):
            candidate = current_path / name
            if not _is_link_or_junction(candidate):
                yield candidate
        if not recursive:
            break


def _lexical_matches(
    guard: WorkspaceGuard,
    root: Path,
    pattern: str,
    *,
    case_sensitive: bool,
    allow_path: Callable[[str], bool] | None = None,
) -> tuple[list[JsonObject], bool]:
    if _is_excluded_scan_path(guard, root):
        return [], False
    needle = pattern if case_sensitive else pattern.casefold()
    matches: list[JsonObject] = []
    output_bytes = 0
    candidates = sorted(
        _iter_files(root, recursive=True),
        key=guard.relative_display,
    )
    for candidate in candidates:
        if allow_path is not None and not allow_path(guard.relative_display(candidate)):
            continue
        try:
            resolved = guard.resolve_existing(
                guard.relative_display(candidate),
                require_file=True,
            )
            content = _read_text_file(resolved)
        except NexusError:
            continue
        for line_number, line in enumerate(content.splitlines(), start=1):
            haystack = line if case_sensitive else line.casefold()
            column = haystack.find(needle)
            if column < 0:
                continue
            match: JsonObject = {
                "path": guard.relative_display(resolved),
                "line": line_number,
                "column": column + 1,
                "text": line,
            }
            encoded = str(match).encode("utf-8")
            if len(matches) >= _MAX_LEXICAL_RESULTS or (
                output_bytes + len(encoded) > _MAX_TEXT_OUTPUT_BYTES
            ):
                return matches, True
            matches.append(match)
            output_bytes += len(encoded)
    return matches, False


def _read_text_file(path: Path) -> str:
    try:
        size = path.stat().st_size
        if size > _MAX_FILE_BYTES:
            raise ToolExecutionError(
                "The requested file exceeds the Day 3 size limit.",
                code="UNSUPPORTED_FILE",
            )
        content = path.read_bytes()
        if b"\x00" in content:
            raise ToolExecutionError(
                "The requested file is not supported text.",
                code="UNSUPPORTED_FILE",
            )
        return content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ToolExecutionError(
            "The requested file is not valid UTF-8 text.",
            code="UNSUPPORTED_FILE",
        ) from exc
    except OSError as exc:
        raise ToolExecutionError("The requested file could not be read.") from exc


def _from_sandbox(
    invocation: ToolInvocation,
    result: SandboxResult,
    started: float,
) -> ToolResult:
    output = asdict(result)
    output["risk_level"] = result.risk_level.value
    output["policy_decision"] = result.policy_decision.value
    if result.error is not None:
        output["error"] = asdict(result.error)
    return ToolResult(
        invocation.invocation_id,
        invocation.tool_name,
        result.error is None and result.exit_code == 0,
        output,
        result.error,
        result.risk_level,
        result.policy_decision,
        None,
        _duration_ms(started),
    )


def _from_git_sandbox(
    invocation: ToolInvocation,
    result: SandboxResult,
    started: float,
) -> ToolResult:
    output: JsonObject = {
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "truncated": result.output_truncated,
    }
    return ToolResult(
        invocation.invocation_id,
        invocation.tool_name,
        result.error is None and result.exit_code == 0,
        output,
        result.error,
        result.risk_level,
        result.policy_decision,
        None,
        _duration_ms(started),
    )


def _success(
    invocation: ToolInvocation,
    output: JsonObject,
    started: float,
) -> ToolResult:
    return ToolResult(
        invocation.invocation_id,
        invocation.tool_name,
        True,
        output,
        None,
        RiskLevel.SAFE,
        PolicyDecision.ALLOWED,
        None,
        _duration_ms(started),
    )


def _nexus_failure(
    invocation: ToolInvocation,
    error: NexusError,
    started: float,
) -> ToolResult:
    dangerous = isinstance(error, PermissionDeniedError) or error.code == "WORKSPACE_PATH_DENIED"
    return ToolResult(
        invocation.invocation_id,
        invocation.tool_name,
        False,
        None,
        ToolError(error.code, str(error), error.retryable),
        RiskLevel.DANGEROUS if dangerous else RiskLevel.SAFE,
        PolicyDecision.DENIED if dangerous else PolicyDecision.ALLOWED,
        None,
        _duration_ms(started),
    )


def _invalid_arguments(invocation: ToolInvocation, started: float) -> ToolResult:
    return ToolResult(
        invocation.invocation_id,
        invocation.tool_name,
        False,
        None,
        ToolError("INVALID_TOOL_ARGUMENTS", "The Tool arguments are invalid.", False),
        RiskLevel.SAFE,
        PolicyDecision.ALLOWED,
        None,
        _duration_ms(started),
    )


def _require_keys(arguments: JsonObject, *, required: set[str], optional: set[str]) -> None:
    keys = set(arguments)
    if not required <= keys or not keys <= required | optional:
        raise ValueError("Invalid Tool argument keys.")


def _required_string(arguments: JsonObject, key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string.")
    return value


def _optional_string(arguments: JsonObject, key: str, default: str) -> str:
    value = arguments.get(key, default)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string.")
    return value


def _optional_bool(arguments: JsonObject, key: str, default: bool) -> bool:
    value = arguments.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be a bool.")
    return value


def _bounded_int(
    arguments: JsonObject,
    key: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    value = arguments.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{key} is outside its allowed range.")
    return value


def _bounded_float(
    arguments: JsonObject,
    key: str,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    value = arguments.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{key} must be numeric.")
    normalized = float(value)
    if not minimum < normalized <= maximum:
        raise ValueError(f"{key} is outside its allowed range.")
    return normalized


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list) or not value or not all(
        isinstance(item, str) and item for item in value
    ):
        raise ValueError("argv must be a non-empty list of strings.")
    return list(value)


def _require_git(executables: TrustedExecutables) -> str:
    if executables.git is None:
        raise ToolExecutionError("Git is unavailable to the native Git Tool.")
    return executables.git


def _is_link_or_junction(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def _is_excluded_scan_directory(name: str) -> bool:
    return name in _EXCLUDED_SCAN_DIRECTORIES or name.startswith(".pytest-tmp-")


def _is_excluded_scan_path(guard: WorkspaceGuard, path: Path) -> bool:
    return any(
        _is_excluded_scan_directory(part)
        for part in guard.relative_display(path).split("/")
    )


def _duration_ms(started: float) -> int:
    return max(0, round((time.perf_counter() - started) * 1000))
