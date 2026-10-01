"""Thin predict/collect integration. Only the official harness grades tasks."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import subprocess
import tempfile
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from nexus.app.bootstrap import Conversation
from nexus.app.config import load_config
from nexus.core.types import Json, RunResult, RuntimeEvent, json_text


def git(workspace: Path, *args: str, env: dict[str, str] | None = None) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(workspace), *args], capture_output=True, check=False, env=env
    )
    if result.returncode:
        raise RuntimeError(f"git {args[0]} failed (exit {result.returncode})")
    return result.stdout


def verify_base(workspace: Path, base: str) -> str:
    if not re.fullmatch(r"[0-9a-fA-F]{7,64}", base):
        raise ValueError("base_commit must be a commit object id")
    resolved = git(workspace, "rev-parse", "--verify", f"{base}^{{commit}}").decode().strip()
    if git(workspace, "rev-parse", "HEAD").decode().strip() != resolved:
        raise ValueError("Prepared workspace HEAD differs from original base_commit")
    if git(workspace, "status", "--porcelain", "--untracked-files=all"):
        raise ValueError("Prepared inference workspace is not clean")
    return resolved


def collect_patch(workspace: Path, original_base: str) -> str:
    git(workspace, "cat-file", "-e", f"{original_base}^{{commit}}")
    with tempfile.TemporaryDirectory(prefix="nexus-index-") as temp:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(temp) / "index")}
        git(workspace, "read-tree", original_base, env=env)
        git(workspace, "add", "-A", "--", ".", env=env)
        return git(
            workspace, "diff", "--cached", "--binary", "--no-ext-diff", original_base, "--", env=env
        ).decode("utf-8")


def read_jsonl(path: Path) -> list[Json]:
    with path.open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def append_json(path: Path, value: Json) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json_text(value) + "\n")


async def predict(tasks: Path, output: Path, environment_record: Path) -> None:
    config = load_config()
    cases = read_jsonl(tasks)
    environment = json.loads(
        await asyncio.to_thread(environment_record.read_text, encoding="utf-8")
    )
    for field in ("dataset", "dataset_revision", "network", "contamination"):
        if not environment.get(field):
            raise ValueError(f"Environment record requires {field}")
    output = await asyncio.to_thread(output.resolve)
    output.mkdir(parents=True, exist_ok=True)
    if any((output / name).exists() for name in ("predictions.jsonl", "runs.jsonl")):
        raise ValueError("Output already contains a run; use a fresh directory")
    seen: set[str] = set()
    for case in cases:
        instance = case["instance_id"]
        if instance in seen or not re.fullmatch(r"[A-Za-z0-9_.-]+", instance):
            raise ValueError("Invalid/duplicate instance_id")
        seen.add(instance)
        for required in ("repo", "base_commit", "problem_statement", "workspace"):
            if not isinstance(case.get(required), str) or not case[required]:
                raise ValueError(f"Task requires {required}")
        workspace = await asyncio.to_thread(Path(case["workspace"]).resolve)
        if output.is_relative_to(workspace):
            raise ValueError("Evaluation artifacts must be outside the inference workspace")
        base = verify_base(workspace, case["base_commit"])
        run_id = uuid4().hex
        trajectory = output / f"{instance}-{run_id}.jsonl"

        async def consumer(event: RuntimeEvent, target: Path = trajectory) -> None:
            if event.kind not in {"assistant_delta", "tool_output_delta"}:
                append_json(target, asdict(event))

        conversation = Conversation(workspace, config, consumer)
        result = RunResult("failed", reason="runtime_not_started")
        error: str | None = None
        patch: str | None = None
        try:
            result = await conversation.turn(case["problem_statement"])
        except Exception as exc:
            error = type(exc).__name__
        finally:
            try:
                patch = collect_patch(workspace, base)
                secrets = (
                    conversation.events.secrets
                    if conversation.events is not None
                    else (config.model.key(),)
                )
                if any(secret and secret in patch for secret in secrets):
                    patch, error = None, "patch_contains_configured_secret"
            except Exception as exc:
                error = f"patch_collection:{type(exc).__name__}"
            conversation.close()
            if patch is not None:
                append_json(
                    output / "predictions.jsonl",
                    {
                        "instance_id": instance,
                        "model_name_or_path": config.model.name,
                        "model_patch": patch,
                    },
                )
            runtime = asdict(result)
            # Canonical final text already lives in the redacted public trajectory.
            runtime.pop("final_text")
            append_json(
                output / "runs.jsonl",
                {
                    "instance_id": instance,
                    "run_id": conversation.session.run_id or run_id,
                    "trajectory": str(trajectory),
                    "base_commit": base,
                    "model": config.model.name,
                    "patch_sha256": hashlib.sha256(patch.encode()).hexdigest()
                    if patch is not None
                    else None,
                    "environment": environment,
                    "runtime": runtime,
                    "error": error,
                },
            )


def collect(runs_path: Path, harness_root: Path, harness_version: str, output: Path) -> None:
    if output.exists():
        raise ValueError("Collected results already exist; use a new output file")
    summary_path = harness_root / "results.json"
    summary = json.loads(summary_path.read_text()) if summary_path.exists() else {}
    for run in read_jsonl(runs_path):
        instance = run["instance_id"]
        folder = harness_root / run["model"].replace("/", "__") / instance
        report_path, patch_path = folder / "report.json", folder / "patch.diff"
        state = "unknown"
        error = None
        report_hash = None
        if report_path.exists():
            try:
                raw = report_path.read_bytes()
                report_hash = hashlib.sha256(raw).hexdigest()
                report = json.loads(raw)[instance]
                if type(report.get("resolved")) is not bool:
                    raise ValueError("Unsupported official report schema")
                applied_hash = hashlib.sha256(patch_path.read_bytes()).hexdigest()
                if applied_hash != run["patch_sha256"]:
                    raise ValueError(
                        "Official patch differs from prediction; stale report rejected"
                    )
                state = "resolved" if report["resolved"] else "unresolved"
            except (OSError, KeyError, ValueError) as exc:
                state, error = "error", str(exc)
        elif instance in summary.get("error_ids", []):
            state = "error"
        append_json(
            output,
            {
                "instance_id": instance,
                "run_id": run["run_id"],
                "patch_sha256": run["patch_sha256"],
                "trajectory": run["trajectory"],
                "official_state": state,
                "error": error,
                "harness_run_id": harness_root.name,
                "harness_version": harness_version,
                "report_path": str(report_path),
                "report_sha256": report_hash,
                "summary_path": str(summary_path),
                "environment": run["environment"],
            },
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prediction = commands.add_parser("predict")
    prediction.add_argument("--tasks", type=Path, required=True)
    prediction.add_argument("--output", type=Path, required=True)
    prediction.add_argument("--environment-record", type=Path, required=True)
    collection = commands.add_parser("collect")
    collection.add_argument("--runs", type=Path, required=True)
    collection.add_argument("--harness-root", type=Path, required=True)
    collection.add_argument("--harness-version", required=True)
    collection.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "predict":
        asyncio.run(predict(args.tasks, args.output, args.environment_record))
    else:
        collect(args.runs, args.harness_root, args.harness_version, args.output)


if __name__ == "__main__":
    main()
