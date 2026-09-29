"""Resolve control executables and a separately verified repository environment."""

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
        if requested_path.parent == Path(".") and normalized_name in {"python", "pytest"}:
            return normalized_name
        candidates = {
            "uv": self.uv,
            "git": self.git,
            "ruff": self.ruff,
            "mypy": self.mypy,
        }
        candidate = candidates.get(normalized_name)
        if candidate is None:
            return None
        if not requested_path.is_absolute() and requested_path.parent != Path("."):
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


@dataclass(frozen=True, slots=True)
class RepositoryExecutionEnvironment:
    """Resolve only conventional Python tools in a repository-owned venv."""

    workspace: Path
    trusted_python: str

    def executable(self, name: str) -> str:
        if name not in {"python", "pytest"}:
            raise ValueError("Only repository Python and pytest are supported.")
        root = self.workspace.resolve(strict=True)
        scripts = "Scripts" if os.name == "nt" else "bin"
        filename = f"{name}.exe" if os.name == "nt" else name
        for directory in (".venv", "venv"):
            candidate = root / directory
            try:
                environment = candidate.resolve(strict=True)
                scripts_directory = (candidate / scripts).resolve(strict=True)
                configuration = (candidate / "pyvenv.cfg").resolve(strict=True)
                python_entry = candidate / scripts / (
                    "python.exe" if os.name == "nt" else "python"
                )
                python_target = python_entry.resolve(strict=True)
            except (OSError, RuntimeError):
                continue
            if not (
                environment.is_dir()
                and _is_within(environment, root)
                and scripts_directory.is_dir()
                and _is_within(scripts_directory, environment)
                and _valid_venv_config(configuration, environment)
                and _valid_python_entry(
                    python_entry, python_target, environment, root, self.trusted_python
                )
            ):
                continue
            entry = candidate / scripts / filename
            try:
                target = entry.resolve(strict=True)
            except (OSError, RuntimeError):
                continue
            if name == "python" or (
                target.is_file()
                and _is_within(target, environment)
                and (os.name == "nt" or os.access(entry, os.X_OK))
            ):
                return str(entry)
            continue
        raise ToolExecutionError(
            "Repository execution environment is unavailable for "
            f"{name}: no usable repository .venv or venv executable.",
            code="REPOSITORY_ENVIRONMENT_UNAVAILABLE",
        )


def _valid_venv_config(configuration: Path, environment: Path) -> bool:
    if not configuration.is_file() or not _is_within(configuration, environment):
        return False
    try:
        if configuration.stat().st_size > 16_384:
            return False
        lines = configuration.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return False
    homes: list[str] = []
    for line in lines:
        key, separator, value = line.partition("=")
        if separator and key.strip().casefold() == "home":
            homes.append(value.strip())
    if len(homes) != 1 or not homes[0] or not Path(homes[0]).is_absolute():
        return False
    try:
        return Path(homes[0]).resolve(strict=True).is_dir()
    except (OSError, RuntimeError):
        return False


def _valid_python_entry(
    entry: Path,
    target: Path,
    environment: Path,
    workspace: Path,
    trusted_python: str,
) -> bool:
    if not target.is_file() or (os.name != "nt" and not os.access(entry, os.X_OK)):
        return False
    if _is_within(target, environment):
        return True
    if os.name == "nt" or not entry.is_symlink() or _is_within(target, workspace):
        return False
    try:
        trusted = Path(trusted_python).resolve(strict=True)
    except (OSError, RuntimeError):
        return False
    return os.path.normcase(str(target)) == os.path.normcase(str(trusted))


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
