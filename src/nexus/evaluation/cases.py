"""Eight reviewed cases and their control-side validation assets."""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from nexus.core.types import Json

CASE_IDS = (
    "pvlib__pvlib-python-1707",
    "matplotlib__matplotlib-22835",
    "django__django-11734",
    "psf__requests-6028",
    "sphinx-doc__sphinx-10323",
    "click__path-generic-type",
    "pytest__doctest-optionflag-leak",
    "rich__double-width-wrap",
)
SUITE = Path(__file__).resolve().parents[3] / "evaluation" / "next-dev-v0"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def assets_digest(directory: Path) -> str:
    hasher = hashlib.sha256()
    for path in sorted(directory.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            hasher.update(path.relative_to(directory).as_posix().encode() + b"\0")
            hasher.update(path.read_bytes())
    return hasher.hexdigest()


@dataclass(frozen=True)
class EvalCase:
    id: str
    source: str
    repo: str
    base_commit: str
    image: str
    task: Path
    validator: Path

    @property
    def key(self) -> str:
        return {"psf": "requests", "sphinx-doc": "sphinx"}.get(
            self.id.split("__")[0], self.id.split("__")[0]
        )

    def identity(self) -> Json:
        return {
            "case_id": self.id,
            "source": self.source,
            "repo": self.repo,
            "base_commit": self.base_commit,
            "image": self.image,
            "task_hash": digest(self.task),
            "validator_hash": assets_digest(self.validator.parent),
            "reporter_hash": assets_digest(SUITE / "support"),
        }


def load_case(case_id: str) -> EvalCase:
    if case_id not in CASE_IDS:
        raise ValueError(f"Unknown eval case: {case_id}; choose: {', '.join(CASE_IDS)}")
    path = SUITE / "cases" / case_id / "case.toml"
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    if data["id"] != case_id or not re.fullmatch(r"sha256:[0-9a-f]{64}", data["image"]):
        raise ValueError(f"Invalid frozen case identity: {case_id}")
    if not re.fullmatch(r"[0-9a-f]{40}", data["base_commit"]):
        raise ValueError("Invalid base commit")
    case = EvalCase(
        case_id,
        data["source"],
        data["repo"],
        data["base_commit"],
        data["image"],
        (path.parent / data["task_file"]).resolve(),
        (path.parent / data["validator"]).resolve(),
    )
    for asset in (case.task, case.validator):
        if not asset.is_relative_to(SUITE) or not asset.is_file():
            raise ValueError(f"Missing fixed evaluation asset: {asset}")
    return case


def validator_spec(case: EvalCase) -> Json:
    spec = tomllib.loads(case.validator.read_text(encoding="utf-8"))
    spec["targets"] = json.loads(
        (case.validator.parent / spec["targets_file"]).read_text(encoding="utf-8")
    )
    return spec
