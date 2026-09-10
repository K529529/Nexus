"""Nexus-owned Day 7 Skill boundaries."""

from collections.abc import Sequence
from typing import Protocol

from nexus.domain.skills import (
    SelectedSkill,
    SkillLocation,
    SkillMetadata,
    SkillSelectionResult,
)


class SkillLoader(Protocol):
    async def load_metadata(self, location: SkillLocation) -> SkillMetadata: ...

    async def load_body(self, metadata: SkillMetadata) -> str: ...


class SkillRegistry(Protocol):
    async def scan_metadata(self) -> tuple[SkillMetadata, ...]: ...

    def resolve_selected(
        self,
        selection: SkillSelectionResult,
    ) -> tuple[SkillMetadata, ...]: ...

    async def load_selected(
        self,
        selection: SkillSelectionResult,
    ) -> tuple[SelectedSkill, ...]: ...


class SkillSelector(Protocol):
    async def select(
        self,
        *,
        run_id: str,
        task: str,
        available_skills: Sequence[SkillMetadata],
    ) -> SkillSelectionResult: ...
