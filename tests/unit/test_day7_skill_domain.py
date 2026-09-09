from pathlib import PurePosixPath

import pytest

from nexus.domain.skills import (
    SelectedSkill,
    SkillLocation,
    SkillMetadata,
    SkillSelectionResult,
    SkillSource,
)


def metadata(**changes: object) -> SkillMetadata:
    values: dict[str, object] = {
        "skill_id": "debug-python",
        "name": " Debug Python ",
        "description": " Diagnose failures. ",
        "usage_scenario": " Python debugging. ",
        "source": SkillSource.REPOSITORY,
        "priority": 60,
        "version": "1.0.0",
        "location": SkillLocation(SkillSource.REPOSITORY, "debug-python/SKILL.md"),
    }
    values.update(changes)
    return SkillMetadata(**values)  # type: ignore[arg-type]


def test_frozen_skill_values_normalize_safe_text_and_sequences() -> None:
    value = metadata()
    assert (value.name, value.description, value.usage_scenario) == (
        "Debug Python",
        "Diagnose failures.",
        "Python debugging.",
    )
    selection = SkillSelectionResult(("debug-python",), " Relevant debugging guidance. ")
    selected = SelectedSkill(
        value,
        "## Execution Principles\nEvidence.\n",
        selection.selection_reason_summary,
    )
    assert selection.selected_skill_ids == ("debug-python",)
    assert selected.selected_reason == "Relevant debugging guidance."
    assert PurePosixPath(value.location.relative_path).parts == ("debug-python", "SKILL.md")


@pytest.mark.parametrize(
    "relative_path",
    (
        "/debug-python/SKILL.md",
        "../debug-python/SKILL.md",
        "debug-python/other.md",
        "Debug-Python/SKILL.md",
        "nested/debug-python/SKILL.md",
    ),
)
def test_skill_location_rejects_noncanonical_paths(relative_path: str) -> None:
    with pytest.raises(ValueError):
        SkillLocation(SkillSource.REPOSITORY, relative_path)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("skill_id", "Debug-Python"),
        ("name", ""),
        ("description", "x" * 501),
        ("usage_scenario", " "),
        ("priority", -1),
        ("priority", 101),
        ("priority", True),
        ("priority", "1"),
        ("version", "1.0"),
        ("version", 1),
        ("version", "01.0.0"),
        ("version", "1.0.0-beta"),
    ),
)
def test_skill_metadata_rejects_invalid_fields(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        metadata(**{field: value})


def test_skill_metadata_requires_source_and_directory_identity() -> None:
    with pytest.raises(ValueError):
        metadata(
            source=SkillSource.BUILTIN,
            location=SkillLocation(SkillSource.REPOSITORY, "debug-python/SKILL.md"),
        )
    with pytest.raises(ValueError):
        metadata(
            location=SkillLocation(SkillSource.REPOSITORY, "write-tests/SKILL.md"),
        )


def test_selection_rejects_duplicate_invalid_ids_and_unsafe_summary() -> None:
    with pytest.raises(ValueError):
        SkillSelectionResult(("debug-python", "debug-python"), "duplicate")
    with pytest.raises(ValueError):
        SkillSelectionResult(("INVALID",), "invalid")
    with pytest.raises(ValueError):
        SkillSelectionResult((), "")
    with pytest.raises(ValueError):
        SkillSelectionResult((), "x" * 1001)
    with pytest.raises(ValueError):
        SkillSelectionResult(("one", "two", "three"), "too many")


def test_selected_skill_requires_normalized_body() -> None:
    for body in ("", "body without newline", "body\r\n", "body\n\n"):
        with pytest.raises(ValueError):
            SelectedSkill(metadata(), body, "selected")
