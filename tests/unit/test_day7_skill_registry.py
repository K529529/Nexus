from __future__ import annotations

from pathlib import Path

import pytest

from nexus.domain.skills import (
    SkillLocation,
    SkillMetadata,
    SkillSelectionResult,
    SkillSource,
)
from nexus.errors import ContextError
from nexus.skills.registry import DefaultSkillRegistry


class Loader:
    def __init__(self) -> None:
        self.metadata_calls: list[SkillLocation] = []
        self.body_calls: list[SkillMetadata] = []

    async def load_metadata(self, location: SkillLocation) -> SkillMetadata:
        self.metadata_calls.append(location)
        skill_id = location.relative_path.split("/", 1)[0]
        priorities = {
            SkillSource.REPOSITORY: 1,
            SkillSource.USER_GLOBAL: 90,
            SkillSource.BUILTIN: 100,
        }
        versions = {
            SkillSource.REPOSITORY: "1.0.0",
            SkillSource.USER_GLOBAL: "9.0.0",
            SkillSource.BUILTIN: "99.0.0",
        }
        return SkillMetadata(
            skill_id,
            f"{skill_id} {location.source.value}",
            f"Description for {skill_id}.",
            f"Use {skill_id} for matching tasks.",
            location.source,
            priorities[location.source],
            versions[location.source],
            location,
        )

    async def load_body(self, metadata: SkillMetadata) -> str:
        self.body_calls.append(metadata)
        return (
            "## Execution Principles\n\nprinciples\n"
            "## Recommended Tools\n\ntools\n"
            "## Workflow\n\nworkflow\n"
            "## Constraints\n\nconstraints\n"
        )


def skill_dir(root: Path, *skill_ids: str) -> None:
    for skill_id in skill_ids:
        folder = root / skill_id
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text("unused", encoding="utf-8")


async def test_scan_order_is_source_then_normalized_relative_path(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    user = tmp_path / "user"
    skill_dir(repository, "write-tests", "debug-python")
    skill_dir(user, "review-repository", "debug-python")
    loader = Loader()

    metadata = await DefaultSkillRegistry(loader, repository, user).scan_metadata()

    calls = [(item.source, item.relative_path) for item in loader.metadata_calls]
    assert calls[:4] == [
        (SkillSource.REPOSITORY, "debug-python/SKILL.md"),
        (SkillSource.REPOSITORY, "write-tests/SKILL.md"),
        (SkillSource.USER_GLOBAL, "debug-python/SKILL.md"),
        (SkillSource.USER_GLOBAL, "review-repository/SKILL.md"),
    ]
    assert [item.relative_path for item in loader.metadata_calls[4:]] == [
        "debug-python/SKILL.md",
        "review-repository/SKILL.md",
        "write-tests/SKILL.md",
    ]
    assert tuple(item.source for item in metadata[:4]) == (
        SkillSource.REPOSITORY,
        SkillSource.REPOSITORY,
        SkillSource.USER_GLOBAL,
        SkillSource.USER_GLOBAL,
    )


async def test_precedence_is_resolved_after_selection_and_only_winner_loads(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    user = tmp_path / "user"
    skill_dir(repository, "debug-python")
    skill_dir(user, "debug-python", "write-tests")
    loader = Loader()
    registry = DefaultSkillRegistry(loader, repository, user)
    catalog = await registry.scan_metadata()

    debug_variants = [item for item in catalog if item.skill_id == "debug-python"]
    assert [item.source for item in debug_variants] == [
        SkillSource.REPOSITORY,
        SkillSource.USER_GLOBAL,
        SkillSource.BUILTIN,
    ]
    selected = await registry.load_selected(
        SkillSelectionResult(
            ("write-tests", "debug-python"),
            "The task matches testing and debugging guidance.",
        )
    )

    assert [item.metadata.skill_id for item in selected] == ["write-tests", "debug-python"]
    assert [item.metadata.source for item in selected] == [
        SkillSource.USER_GLOBAL,
        SkillSource.REPOSITORY,
    ]
    assert loader.body_calls == [item.metadata for item in selected]
    assert all(item.source is not SkillSource.BUILTIN for item in loader.body_calls)


async def test_user_overrides_builtin_when_repository_variant_is_absent(
    tmp_path: Path,
) -> None:
    user = tmp_path / "user"
    skill_dir(user, "debug-python")
    loader = Loader()
    registry = DefaultSkillRegistry(loader, tmp_path / "missing", user)
    await registry.scan_metadata()
    resolved = registry.resolve_selected(
        SkillSelectionResult(("debug-python",), "Debugging guidance matches.")
    )
    assert resolved[0].source is SkillSource.USER_GLOBAL
    assert resolved[0].priority < 100
    assert resolved[0].version == "9.0.0"


async def test_same_source_duplicate_is_fatal_before_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loader = Loader()
    registry = DefaultSkillRegistry(loader, tmp_path / "repository", tmp_path / "user")
    duplicate = SkillLocation(SkillSource.REPOSITORY, "debug-python/SKILL.md")

    def locations(source: SkillSource) -> tuple[SkillLocation, ...]:
        return (duplicate, duplicate) if source is SkillSource.REPOSITORY else ()

    monkeypatch.setattr(registry, "_locations", locations)
    with pytest.raises(ContextError) as caught:
        await registry.scan_metadata()
    assert caught.value.code == "SKILL_ID_DUPLICATE"
    assert loader.body_calls == []


async def test_missing_root_is_nonfatal_and_unknown_selection_fails(tmp_path: Path) -> None:
    loader = Loader()
    registry = DefaultSkillRegistry(loader, tmp_path / "missing", tmp_path / "also-missing")
    catalog = await registry.scan_metadata()
    assert catalog
    assert all(item.source is SkillSource.BUILTIN for item in catalog)
    with pytest.raises(ContextError) as caught:
        registry.resolve_selected(SkillSelectionResult(("unknown",), "Unknown selection."))
    assert caught.value.code == "SKILL_SELECTION_FAILED"
