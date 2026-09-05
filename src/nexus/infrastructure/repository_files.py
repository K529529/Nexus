"""Day 5 read-only index filesystem access, contained and gitignore-aware."""

import os
from dataclasses import dataclass
from pathlib import Path

from pathspec import GitIgnoreSpec

from nexus.context.chunking import normalize_text
from nexus.errors import NexusError
from nexus.security.workspace import WorkspaceGuard

EXCLUDED_DIRECTORIES = frozenset(
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
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
    }
)


@dataclass(frozen=True, slots=True)
class _Scan:
    files: tuple[tuple[str, str], ...]
    scanned: int
    skipped: int


class RepositoryFiles:
    def __init__(self, workspace: Path, max_file_size_bytes: int = 1048576) -> None:
        self.guard = WorkspaceGuard(workspace)
        self._maximum = max_file_size_bytes

    def read(self, path: str) -> str:
        resolved = self.guard.resolve_existing(path, require_file=True)
        if resolved.is_symlink() or resolved.is_junction():
            raise ValueError("Linked files are not indexable.")
        with resolved.open("rb") as stream:
            raw = stream.read(self._maximum + 1)
        if len(raw) > self._maximum or b"\0" in raw:
            raise ValueError("Unsupported or oversized source.")
        return normalize_text(raw.decode("utf-8-sig"))

    def allowed(self, relative: str) -> bool:
        path = Path(relative)
        if (
            path.is_absolute()
            or ".." in path.parts
            or any(part in EXCLUDED_DIRECTORIES for part in path.parts)
            or path.name == "AGENTS.md"
        ):
            return False
        try:
            self.guard.resolve_existing(relative, require_file=True)
            rules: list[str] = []
            current = Path()
            for part in (*path.parts[:-1], None):
                ignore = current / ".gitignore"
                try:
                    content = self.read(ignore.as_posix())
                    # Match relative to each ignore file's directory. A parent directory
                    # exclusion is final: Git cannot reinclude children of an excluded dir.
                    spec = GitIgnoreSpec.from_lines(content.splitlines())
                    suffix = path.relative_to(current).as_posix()
                    components = suffix.split("/")
                    for length in range(1, len(components)):
                        if spec.match_file("/".join(components[:length]) + "/"):
                            return False
                    result = spec.check_file(suffix)
                    if result.include is not None:
                        rules.append("ignored" if result.include else "included")
                except (OSError, ValueError, NexusError):
                    pass
                if part is not None:
                    current /= part
            return not rules or rules[-1] == "included"
        except (OSError, ValueError, NexusError):
            return False

    def scan(self) -> _Scan:
        files: list[tuple[str, str]] = []
        scanned = skipped = 0
        for current, directories, names in os.walk(self.guard.root, followlinks=False):
            folder = Path(current)
            directories[:] = sorted(
                name
                for name in directories
                if name not in EXCLUDED_DIRECTORIES
                and not (folder / name).is_symlink()
                and not (folder / name).is_junction()
            )
            for name in sorted(names):
                candidate = folder / name
                relative = candidate.relative_to(self.guard.root).as_posix()
                scanned += 1
                if candidate.is_symlink() or candidate.is_junction() or not self.allowed(relative):
                    skipped += 1
                    continue
                try:
                    content = self.read(relative)
                except (OSError, ValueError, NexusError):
                    skipped += 1
                    continue
                if not content:
                    skipped += 1
                    continue
                files.append((relative, content))
        return _Scan(tuple(files), scanned, skipped)
