"""Centralized repository-relative path containment."""

from __future__ import annotations

import os
from pathlib import Path

from nexus.errors import PermissionDeniedError, ToolExecutionError


class WorkspaceGuard:
    def __init__(self, workspace: Path) -> None:
        try:
            self._workspace = workspace.resolve(strict=True)
        except OSError as exc:
            raise ToolExecutionError("The configured workspace does not exist.") from exc
        if not self._workspace.is_dir():
            raise ToolExecutionError("The configured workspace is not a directory.")
        self._normalized_workspace = os.path.normcase(str(self._workspace))

    @property
    def root(self) -> Path:
        return self._workspace

    def resolve_existing(
        self,
        relative_path: str,
        *,
        require_file: bool = False,
        require_directory: bool = False,
    ) -> Path:
        requested = Path(relative_path)
        if requested.is_absolute():
            raise self._denied()
        try:
            resolved = (self._workspace / requested).resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            if ".." in requested.parts:
                raise self._denied() from exc
            raise ToolExecutionError(
                "The requested workspace path does not exist.",
                code="TOOL_EXECUTION_ERROR",
            ) from exc
        if not self._contains(resolved):
            raise self._denied()
        if require_file and not resolved.is_file():
            raise ToolExecutionError("The requested path is not a regular file.")
        if require_directory and not resolved.is_dir():
            raise ToolExecutionError("The requested path is not a directory.")
        return resolved

    def relative_display(self, path: Path) -> str:
        return path.relative_to(self._workspace).as_posix() or "."

    def _contains(self, path: Path) -> bool:
        normalized = os.path.normcase(str(path))
        try:
            return os.path.commonpath([self._normalized_workspace, normalized]) == (
                self._normalized_workspace
            )
        except ValueError:
            return False

    @staticmethod
    def _denied() -> PermissionDeniedError:
        return PermissionDeniedError(
            "The requested path is outside the configured workspace.",
            code="WORKSPACE_PATH_DENIED",
        )
