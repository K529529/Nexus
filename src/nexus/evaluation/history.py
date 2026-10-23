"""Rebuild an offline developer dashboard from immutable evaluation evidence.

Run independently with ``python -m nexus.evaluation.history``. No model or Docker calls.
"""

from __future__ import annotations

import argparse
import json
import math
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from nexus.core.types import Json
from nexus.evaluation.cases import CASE_IDS


def numeric(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def collect_history(home: Path) -> Json:
    runs, warnings = [], []
    for directory in sorted((home / "results").glob("*")):
        if not directory.is_dir() or directory.is_symlink():
            continue
        try:
            summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
            manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
            selected = summary["selected"]
            cases = summary["cases"]
            if not isinstance(selected, list) or not isinstance(cases, list):
                raise ValueError("Invalid cases/selected")
            if len(set(selected)) != len(selected):
                raise ValueError("Duplicate selected case")
            by_id = {case["case_id"]: case for case in cases}
            if len(by_id) != len(cases) or set(by_id) != set(selected):
                raise ValueError("Case identities do not match selection")
            statuses = {"PASS", "FAIL", "ERROR", "ABORTED", "NOT_RUN"}
            if any(case["status"] not in statuses for case in cases):
                raise ValueError("Unknown case status")
            full = selected == list(CASE_IDS)
            complete = full and all(c["status"] in {"PASS", "FAIL", "ERROR"} for c in cases)
            metrics: Json = {}
            partial: Json = {}
            for key in (
                "input_tokens",
                "output_tokens",
                "duration_ms",
                "model_calls",
                "tool_calls",
                "tool_failures",
                "peak_context",
            ):
                values = []
                covered = True
                for case in cases:
                    metric = case.get("metrics", {})
                    if key.endswith("tokens"):
                        usage = metric.get("usage", {}).get(key, {})
                        value = usage.get("reported")
                        covered &= (
                            numeric(usage.get("calls"))
                            and usage.get("coverage") == usage.get("calls")
                        )
                    else:
                        value = metric.get(key)
                    if metric.get("available") and numeric(value):
                        values.append(value)
                    else:
                        covered = False
                metrics[key] = (
                    (max(values) if key == "peak_context" else sum(values)) if values else None
                )
                partial[key] = not covered
            # Retain only public, comparison-relevant fields; never embed arbitrary config/secrets.
            model = manifest.get("model", {})
            config = {
                f"model.{key}": model.get(key)
                for key in (
                    "name",
                    "base_url",
                    "reasoning_effort",
                    "context_window",
                    "max_output_tokens",
                    "request_timeout_seconds",
                    "include_usage",
                    "enable_thinking",
                    "output_token_parameter",
                )
            }
            config["limits"] = manifest.get("limits")
            config["context_policy"] = manifest.get("context_policy")
            config["conversation_projection"] = manifest.get("conversation_projection")
            config["compaction"] = manifest.get("compaction")
            config["compaction_model"] = {
                k: (manifest.get("compaction_model") or {}).get(k)
                for k in (
                    "name",
                    "base_url",
                    "reasoning_effort",
                    "max_output_tokens",
                    "context_window",
                    "request_timeout_seconds",
                )
            }
            config["case_identity"] = [
                {
                    k: c.get("provenance", {}).get(k)
                    for k in (
                        "case_id",
                        "base_commit",
                        "base_tree",
                        "image",
                        "task_hash",
                        "validator_hash",
                        "reporter_hash",
                    )
                }
                for c in cases
            ]
            runs.append(
                dict(
                    id=directory.name,
                    date=manifest.get("started_at", directory.name),
                    commit=(manifest.get("nexus") or {}).get("commit"),
                    runtime_digest=(manifest.get("nexus") or {}).get("runtime_digest"),
                    model=model.get("name", "unknown"),
                    config=config,
                    full=full,
                    complete=complete,
                    selected=selected,
                    passed=sum(c["status"] == "PASS" for c in cases),
                    metrics=metrics,
                    partial=partial,
                    cases=[
                        dict(id=c["case_id"], status=c["status"], metrics=c.get("metrics", {}))
                        for c in cases
                    ],
                    report=f"results/{quote(directory.name, safe='')}/report.html",
                )
            )
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            warnings.append(f"{directory.name}: {exc}")
    runs.sort(key=lambda run: (run["date"], run["id"]))
    return dict(
        runs=runs, warnings=warnings, cases=list(CASE_IDS), generated=datetime.now(UTC).isoformat()
    )


def build_history(home: Path) -> Path:
    data = collect_history(home)
    payload = json.dumps(data, ensure_ascii=False, allow_nan=False).replace("<", "\\u003c")
    template = Path(__file__).with_name("history.html").read_text(encoding="utf-8")
    html = template.replace("__HISTORY_DATA__", payload)
    home.mkdir(parents=True, exist_ok=True)
    target = home / "history.html"
    # Unique temporary file also permits simultaneous independent report rebuilds.
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=home, suffix=".tmp", delete=False
    ) as stream:
        temporary = Path(stream.name)
        stream.write(html)
    try:
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description="Rebuild the offline 8-case history dashboard")
    parser.add_argument("--home", type=Path, default=Path.home() / ".nexus" / "evaluation")
    args = parser.parse_args()
    print(build_history(args.home))


if __name__ == "__main__":
    main()
