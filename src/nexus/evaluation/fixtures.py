"""Immutable fixture-source copying for isolated evaluation workspaces."""

from __future__ import annotations

import os
import shutil
from pathlib import Path


class FixtureConstraintError(ValueError):
    """The immutable fixture violates a frozen evaluation constraint."""


def validate_fixture_source(source: Path) -> Path:
    root = source.resolve(strict=True)
    _validate_source_symlinks(root)
    return root


def copy_fixture_source(source: Path, destination: Path) -> None:
    root = validate_fixture_source(source)
    shutil.copytree(root, destination, symlinks=True)


def _validate_source_symlinks(root: Path) -> None:
    normalized_root = os.path.normcase(str(root))
    for path in root.rglob("*"):
        if not path.is_symlink():
            continue
        target = os.path.normcase(str(path.resolve(strict=False)))
        try:
            contained = os.path.commonpath([normalized_root, target]) == normalized_root
        except ValueError as exc:
            raise FixtureConstraintError("Fixture contains a workspace-escaping symlink.") from exc
        if not contained:
            raise FixtureConstraintError("Fixture contains a workspace-escaping symlink.")
