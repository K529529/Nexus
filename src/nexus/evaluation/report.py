"""Stable JSON serialization and report writing."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from types import MappingProxyType

from nexus.evaluation.models import EvalSuiteReport


def write_report(
    report: EvalSuiteReport, reports_root: Path, *, write_baseline: bool = False
) -> tuple[Path, Path | None]:
    reports_root.mkdir(parents=True, exist_ok=True)
    stamp = report.generated_at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = reports_root / f"eval-report-{stamp}.json"
    payload = json.dumps(_json_value(report), ensure_ascii=False, indent=2) + "\n"
    target.write_text(payload, encoding="utf-8")
    baseline = reports_root / "baseline-v1.json"
    baseline_written = None
    if write_baseline and not baseline.exists():
        baseline.write_text(payload, encoding="utf-8")
        baseline_written = baseline
    return target, baseline_written


def _json_value(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: _json_value(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, (Mapping, MappingProxyType)):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value
