"""Resolve a small trusted executable set without trusting the workspace."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from nexus.errors import ToolExecutionError


@dataclass(frozen=True, slots=True)
class TrustedExecutables:
    python: str
    uv: str | None
    git: str | None
    pytest: str | None
    ruff: str | None
    mypy: str | None

    @classmethod
    def resolve(cls, workspace: Path) -> TrustedExecutables:
        sanitized_path = _sanitized_path(workspace)
        python = _which_outside_workspace("python", workspace, sanitized_path)
        if python is None:
            raise ToolExecutionError("A trusted Python executable is unavailable.")
        return cls(
            python=python,
            uv=_which_outside_workspace("uv", workspace, sanitized_path),
            git=_which_outside_workspace("git", workspace, sanitized_path),
            pytest=_which_outside_workspace("pytest", workspace, sanitized_path),
            ruff=_which_outside_workspace("ruff", workspace, sanitized_path),
            mypy=_which_outside_workspace("mypy", workspace, sanitized_path),
        )

    def normalize_argv(self, argv: list[str]) -> list[str]:
        if not argv:
            return []
        resolved = self._resolve_requested(argv[0])
        return [resolved or argv[0], *argv[1:]]

    def _resolve_requested(self, requested: str) -> str | None:
        requested_path = Path(requested)
        normalized_name = requested_path.name.casefold()
        if normalized_name.endswith(".exe"):
            normalized_name = normalized_name[:-4]
        candidates = {
            "python": self.python,
            "uv": self.uv,
            "git": self.git,
            "pytest": self.pytest,
            "ruff": self.ruff,
            "mypy": self.mypy,
        }
        candidate = candidates.get(normalized_name)
        if candidate is None:
            return None
        if requested_path.is_absolute():
            try:
                if os.path.normcase(str(requested_path.resolve(strict=True))) != os.path.normcase(
                    candidate
                ):
                    return None
            except OSError:
                return None
        return candidate


def _which_outside_workspace(
    name: str,
    workspace: Path,
    search_path: str,
) -> str | None:
    located = shutil.which(name, path=search_path)
    if located is None:
        return None
    resolved = Path(located).resolve(strict=True)
    return None if _is_within(resolved, workspace) else str(resolved)


def _sanitized_path(workspace: Path) -> str:
    entries: list[str] = []
    for value in os.environ.get("PATH", "").split(os.pathsep):
        if not value:
            continue
        candidate = Path(value)
        if not candidate.is_absolute():
            continue
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if resolved.is_dir() and not _is_within(resolved, workspace):
            entries.append(str(resolved))
    return os.pathsep.join(entries)


def _is_within(path: Path, workspace: Path) -> bool:
    normalized_path = os.path.normcase(str(path))
    normalized_workspace = os.path.normcase(str(workspace.resolve(strict=True)))
    try:
        return os.path.commonpath([normalized_path, normalized_workspace]) == (
            normalized_workspace
        )
    except ValueError:
        return False
