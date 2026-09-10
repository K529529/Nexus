"""Frozen Day 7 Skill values."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath

_SKILL_ID = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_SEMVER_CORE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


class SkillSource(StrEnum):
    REPOSITORY = "repository"
    USER_GLOBAL = "user_global"
    BUILTIN = "builtin"


@dataclass(frozen=True, slots=True)
class SkillLocation:
    source: SkillSource
    relative_path: str

    def __post_init__(self) -> None:
        if not isinstance(self.source, SkillSource) or not isinstance(self.relative_path, str):
            raise ValueError("Skill location fields are invalid.")
        path = PurePosixPath(self.relative_path)
        if (
            path.is_absolute()
            or len(path.parts) != 2
            or path.parts[-1] != "SKILL.md"
            or not _SKILL_ID.fullmatch(path.parts[0])
            or self.relative_path != path.as_posix()
        ):
            raise ValueError("Skill location must use <skill-id>/SKILL.md.")


@dataclass(frozen=True, slots=True)
class SkillMetadata:
    skill_id: str
    name: str
    description: str
    usage_scenario: str
    source: SkillSource
    priority: int
    version: str
    location: SkillLocation

    def __post_init__(self) -> None:
        if not isinstance(self.skill_id, str) or not _SKILL_ID.fullmatch(self.skill_id):
            raise ValueError("Skill ID is invalid.")
        for field_name, value, maximum in (
            ("name", self.name, 100),
            ("description", self.description, 500),
            ("usage_scenario", self.usage_scenario, 500),
        ):
            if not isinstance(value, str):
                raise ValueError(f"Skill {field_name} is invalid.")
            normalized = value.strip()
            if not normalized or len(normalized) > maximum:
                raise ValueError(f"Skill {field_name} is invalid.")
            object.__setattr__(self, field_name, normalized)
        if (
            isinstance(self.priority, bool)
            or not isinstance(self.priority, int)
            or not 0 <= self.priority <= 100
        ):
            raise ValueError("Skill priority must be an integer from 0 through 100.")
        if not isinstance(self.version, str) or not _SEMVER_CORE.fullmatch(self.version):
            raise ValueError("Skill version must use SemVer core form.")
        if not isinstance(self.location, SkillLocation) or not isinstance(
            self.source, SkillSource
        ):
            raise ValueError("Skill source and location are invalid.")
        if self.location.source is not self.source:
            raise ValueError("Skill source and location source must match.")
        if PurePosixPath(self.location.relative_path).parts[0] != self.skill_id:
            raise ValueError("Skill directory and ID must match.")


@dataclass(frozen=True, slots=True)
class SelectedSkill:
    metadata: SkillMetadata
    body: str
    selected_reason: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.body, str)
            or not self.body.strip()
            or "\r" in self.body
            or not self.body.endswith("\n")
            or self.body.endswith("\n\n")
        ):
            raise ValueError("Selected Skill body is not normalized.")
        normalized_reason = _selection_summary(self.selected_reason)
        object.__setattr__(self, "selected_reason", normalized_reason)


@dataclass(frozen=True, slots=True)
class SkillSelectionResult:
    selected_skill_ids: tuple[str, ...]
    selection_reason_summary: str

    def __post_init__(self) -> None:
        if isinstance(self.selected_skill_ids, str):
            raise ValueError("Selected Skill IDs must be a sequence of identities.")
        skill_ids = tuple(self.selected_skill_ids)
        if len(skill_ids) > 2 or len(skill_ids) != len(set(skill_ids)) or any(
            not isinstance(skill_id, str) or not _SKILL_ID.fullmatch(skill_id)
            for skill_id in skill_ids
        ):
            raise ValueError("Selected Skill IDs must be valid and unique.")
        object.__setattr__(self, "selected_skill_ids", skill_ids)
        object.__setattr__(
            self,
            "selection_reason_summary",
            _selection_summary(self.selection_reason_summary),
        )


def _selection_summary(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Skill selection summary is invalid.")
    normalized = value.strip()
    if not normalized or len(normalized) > 1000:
        raise ValueError("Skill selection summary is invalid.")
    return normalized
