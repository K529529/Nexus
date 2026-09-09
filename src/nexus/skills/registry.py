"""Deterministic metadata catalog and post-selection Skill resolution."""

from __future__ import annotations

from contextvars import ContextVar
from importlib.resources import files
from pathlib import Path

from nexus.domain.ports.skills import SkillLoader
from nexus.domain.skills import (
    SelectedSkill,
    SkillLocation,
    SkillMetadata,
    SkillSelectionResult,
    SkillSource,
)
from nexus.errors import ContextError

_PRECEDENCE = {
    SkillSource.REPOSITORY: 0,
    SkillSource.USER_GLOBAL: 1,
    SkillSource.BUILTIN: 2,
}


class DefaultSkillRegistry:
    def __init__(
        self,
        loader: SkillLoader,
        repository_root: Path,
        user_root: Path,
        *,
        builtin_package: str = "nexus.skills.builtin",
    ) -> None:
        self._loader = loader
        self._roots = {
            SkillSource.REPOSITORY: repository_root,
            SkillSource.USER_GLOBAL: user_root,
        }
        self._builtin_package = builtin_package
        self._metadata: ContextVar[tuple[SkillMetadata, ...]] = ContextVar(
            "skill_registry_metadata",
            default=(),
        )

    async def scan_metadata(self) -> tuple[SkillMetadata, ...]:
        self._reset_loader_snapshots()
        result: list[SkillMetadata] = []
        for source in (
            SkillSource.REPOSITORY,
            SkillSource.USER_GLOBAL,
            SkillSource.BUILTIN,
        ):
            for location in self._locations(source):
                result.append(await self._loader.load_metadata(location))
        seen: set[tuple[SkillSource, str]] = set()
        for metadata in result:
            identity = (metadata.source, metadata.skill_id)
            if identity in seen:
                raise ContextError(
                    f"Duplicate Skill identity in {metadata.source.value}:"
                    f"{metadata.skill_id}.",
                    code="SKILL_ID_DUPLICATE",
                )
            seen.add(identity)
        catalog = tuple(result)
        self._metadata.set(catalog)
        return catalog

    def resolve_selected(
        self,
        selection: SkillSelectionResult,
    ) -> tuple[SkillMetadata, ...]:
        grouped: dict[str, list[SkillMetadata]] = {}
        for metadata in self._metadata.get():
            grouped.setdefault(metadata.skill_id, []).append(metadata)
        resolved: list[SkillMetadata] = []
        for skill_id in selection.selected_skill_ids:
            variants = grouped.get(skill_id)
            if not variants:
                raise ContextError(
                    "Skill selection referenced an unknown identity.",
                    code="SKILL_SELECTION_FAILED",
                    retryable=True,
                )
            resolved.append(min(variants, key=lambda value: _PRECEDENCE[value.source]))
        return tuple(resolved)

    async def load_selected(
        self,
        selection: SkillSelectionResult,
    ) -> tuple[SelectedSkill, ...]:
        try:
            selected: list[SelectedSkill] = []
            for metadata in self.resolve_selected(selection):
                selected.append(
                    SelectedSkill(
                        metadata,
                        await self._loader.load_body(metadata),
                        selection.selection_reason_summary,
                    )
                )
            return tuple(selected)
        finally:
            self._reset_loader_snapshots()

    def _reset_loader_snapshots(self) -> None:
        reset = getattr(self._loader, "_reset_snapshots", None)
        if callable(reset):
            reset()

    def _locations(self, source: SkillSource) -> tuple[SkillLocation, ...]:
        if source is SkillSource.BUILTIN:
            try:
                root = files(self._builtin_package)
                names = sorted(
                    item.name
                    for item in root.iterdir()
                    if item.is_dir() and item.name != "__pycache__"
                )
            except (ModuleNotFoundError, OSError) as exc:
                raise ContextError(
                    "Builtin Skill root is unreadable.",
                    code="SKILL_PATH_UNSAFE",
                ) from exc
        else:
            root_path = self._roots[source]
            if not root_path.exists():
                return ()
            try:
                canonical_root = root_path.resolve(strict=True)
                if not canonical_root.is_dir():
                    raise OSError("Skill root is not a directory.")
                names = sorted(item.name for item in canonical_root.iterdir() if item.is_dir())
            except OSError as exc:
                raise ContextError(
                    f"Skill root is unsafe for {source.value}.",
                    code="SKILL_PATH_UNSAFE",
                ) from exc
        try:
            return tuple(SkillLocation(source, f"{name}/SKILL.md") for name in names)
        except ValueError as exc:
            raise ContextError(
                f"Skill directory name is invalid for {source.value}.",
                code="SKILL_METADATA_INVALID",
            ) from exc
