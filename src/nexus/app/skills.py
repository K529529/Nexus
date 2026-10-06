"""Local user-installed SKILL.md discovery. V0.1 loads Markdown, never scripts."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import yaml

from nexus.core.skills import MAX_SKILLS, skill_info, snapshot
from nexus.core.types import SkillInfo, SkillSnapshot

MAX_FILE_BYTES = 20480


class SkillCatalog:
    def __init__(self, root: Path, clean: Callable[[str], str] = lambda text: text) -> None:
        self.root, self.clean = root.resolve(), clean
        self.items: tuple[SkillInfo, ...] = ()
        self.warnings: list[str] = []
        self.refresh()

    def read(self, name: str) -> SkillSnapshot:
        # Validate before joining paths; never accept a path from model arguments.
        skill_info(name, "lookup")
        path = self.root / name / "SKILL.md"
        try:
            if not path.resolve().is_relative_to(self.root):
                raise ValueError("Skill path escapes the configured skills directory")
            with path.open("rb") as stream:
                raw = stream.read(MAX_FILE_BYTES + 1)
            if len(raw) > MAX_FILE_BYTES:
                raise ValueError("SKILL.md exceeds 20 KiB")
            lines = raw.decode("utf-8-sig").splitlines(keepends=True)
            if not lines or lines[0].strip() != "---":
                raise ValueError("SKILL.md requires YAML front matter")
            end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
            if end is None:
                raise ValueError("SKILL.md has unclosed front matter")
            header = yaml.safe_load("".join(lines[1:end]))
            if not isinstance(header, dict):
                raise ValueError("Skill metadata must be a mapping")
            info = skill_info(header.get("name"), header.get("description"))
            if info.name != name:
                raise ValueError("Skill name must match its directory")
            # Optional metadata is inert; allowed-tools cannot grant execution rights.
            return snapshot(
                info.name, self.clean(info.description), self.clean("".join(lines[end + 1 :]))
            )
        except (OSError, UnicodeError, yaml.YAMLError, RecursionError):
            raise ValueError("Cannot read Skill as bounded UTF-8 Markdown with safe YAML") from None

    def refresh(self) -> None:
        items: list[SkillInfo] = []
        self.warnings = []
        try:
            folders = sorted(self.root.iterdir()) if self.root.exists() else []
        except OSError:
            self.warnings.append("Cannot read local skills directory")
            folders = []
        for folder in folders:
            if not folder.is_dir() or not (folder / "SKILL.md").exists():
                continue
            if len(items) == MAX_SKILLS:
                self.warnings.append(
                    "Skill directory exceeds 32 valid entries; remaining entries omitted"
                )
                break
            try:
                value = self.read(folder.name)
                items.append(SkillInfo(value.name, value.description))
            except ValueError as exc:
                self.warnings.append(f"Skill {folder.name}: {exc}")
        self.items = tuple(items)

    def load(self, name: str) -> SkillSnapshot:
        if name not in {s.name for s in self.items}:
            raise ValueError("Unknown Skill; use /skills to inspect or refresh the directory")
        return self.read(name)
