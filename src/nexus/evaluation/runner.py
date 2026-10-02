"""Sequential evaluation around the existing Conversation; no second Agent loop."""

from __future__ import annotations

import asyncio
import time
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from nexus.app.bootstrap import Conversation
from nexus.app.config import Config
from nexus.app.events import redact
from nexus.app.profile import RunProfiler, profile_metrics
from nexus.core.types import Json, RuntimeEvent
from nexus.evaluation.cases import SUITE, EvalCase, load_case
from nexus.evaluation.environment import CleanupError, Docker, Environment, EnvironmentError
from nexus.evaluation.report import implementation_identity, public_url, save_report, write_json
from nexus.evaluation.validation import validate


def empty_result(case: EvalCase, run_id: str) -> Json:
    return dict(
        schema_version=1,
        evaluation_run_id=run_id,
        case_id=case.id,
        status="NOT_RUN",
        agent=None,
        validation=dict(status="not_run", reason="Not started", checks=[]),
        metrics=dict(available=False, unavailable_reason="Agent did not start"),
        timing=dict(prepare=None, agent=None, validation=None, total=None),
        provenance={**case.identity(), "manifest": "../manifest.json"},
        artifacts={},
        diagnostics=[],
    )


async def run_case(
    case: EvalCase, run_id: str, directory: Path, config: Config, docker: Docker, cache: Path
) -> tuple[Json, bool]:
    result = empty_result(case, run_id)
    output = directory / case.id
    output.mkdir()
    env = Environment(docker, case, cache)
    profiler = RunProfiler()
    profile_error = False
    conversation: Conversation | None = None
    phase, start, phase_start = "prepare", time.monotonic(), time.monotonic()
    stop_suite = False

    async def consume(event: RuntimeEvent) -> None:
        nonlocal profile_error
        try:
            profiler.consume(event)
        except Exception:
            profile_error = True

    try:
        await env.prepare()
        result["timing"]["prepare"] = time.monotonic() - phase_start
        result["provenance"].update(base_tree=env.tree, workspace_base_commit=env.base_commit)
        phase, phase_start = "agent", time.monotonic()
        evaluation_config = replace(
            config, limits=replace(config.limits, shell="/bin/sh"), mcp_servers={}
        )
        conversation = Conversation(
            env.workspace,
            evaluation_config,
            consume,
            home=output / "session",
            registry=env.registry(),
            environment_display=("Linux", "/workspace"),
        )
        outcome = await conversation.turn(case.task.read_text(encoding="utf-8"))
        result["agent"] = dict(
            outcome=outcome.outcome,
            reason=outcome.reason,
            session_id=conversation.session.session_id,
            run_id=conversation.session.run_id,
        )
        result["timing"]["agent"] = (
            profiler.profile.duration_ms / 1000
            if profiler.profile.duration_ms is not None
            else None
        )
        if outcome.outcome != "completed":
            category = {
                "limited": "agent_limited",
                "aborted": "agent_aborted",
                "failed": "agent_runtime_error",
            }[outcome.outcome]
            result["diagnostics"].append(
                dict(phase=phase, category=category, reason=outcome.reason)
            )
        if outcome.outcome == "aborted":
            result["status"], stop_suite = "ABORTED", True
        elif env.broken or outcome.reason and "cleanup_incomplete" in outcome.reason:
            result["status"], stop_suite = "ERROR", True
        else:
            phase, phase_start = "collection", time.monotonic()
            patch = output / "patch.diff"
            await env.collect_patch(patch)
            result["artifacts"]["patch"] = patch.name
            phase, phase_start = "validation", time.monotonic()
            validation = await validate(case, patch, output, docker, cache)
            result["validation"] = validation
            result["timing"]["validation"] = validation["duration_s"]
            result["status"] = {"passed": "PASS", "failed": "FAIL", "error": "ERROR"}[
                validation["status"]
            ]
            if validation["status"] != "passed":
                result["diagnostics"].append(
                    dict(
                        phase=phase,
                        category="validation_" + validation["status"],
                        reason=validation["reason"],
                    )
                )
    except asyncio.CancelledError:
        result["status"], stop_suite = "ABORTED", True
        result["diagnostics"].append(
            dict(phase=phase, category="agent_aborted", reason="User interrupted evaluation")
        )
    except Exception as exc:
        result["status"] = "ERROR"
        stop_suite = isinstance(exc, CleanupError)
        category = (
            "setup_error"
            if phase == "prepare"
            else "validation_error"
            if phase in {"collection", "validation"}
            else "agent_runtime_error"
        )
        result["diagnostics"].append(
            dict(
                phase=phase,
                category=category,
                reason=redact(f"{type(exc).__name__}: {exc}", (config.model.key(),)),
            )
        )
        # Stop only if shared Docker connectivity has failed, not an ordinary case failure.
        try:
            await docker.checked(["info", "--format", "{{.OSType}}"], 15)
        except (EnvironmentError, OSError, TimeoutError):
            stop_suite = True
    finally:
        if conversation:
            try:
                await conversation.close()
            except Exception as exc:
                result["status"] = "ERROR"
                result["diagnostics"].append(
                    dict(
                        phase="cleanup",
                        category="agent_runtime_error",
                        reason=f"Conversation cleanup failed: {type(exc).__name__}",
                    )
                )
            if conversation.writer:
                result["artifacts"]["session"] = conversation.writer.path.relative_to(
                    output
                ).as_posix()
        result["metrics"] = profile_metrics(profiler.profile)
        if profile_error:
            result["metrics"] = dict(
                available=False, unavailable_reason="Profile collection failed"
            )
        if (output / "validator.log").exists():
            result["artifacts"]["validator_log"] = "validator.log"
        try:
            await env.close()
        except (EnvironmentError, OSError, TimeoutError) as exc:
            stop_suite = True
            result["status"] = "ERROR" if result["status"] != "ABORTED" else "ABORTED"
            result["diagnostics"].append(
                dict(
                    phase="cleanup",
                    category="setup_error",
                    reason=f"Owned container/workspace cleanup failed: {exc}",
                )
            )
        result["timing"]["total"] = time.monotonic() - start
        if phase in {"prepare", "validation"} and result["timing"][phase] is None:
            result["timing"][phase] = time.monotonic() - phase_start
        write_json(output / "result.json", result)
    return result, stop_suite


async def evaluate(selected: list[str], config: Config, home: Path | None = None) -> int:
    cases = [load_case(case_id) for case_id in selected]
    home = home or Path.home() / ".nexus" / "evaluation"
    cache = home / "cache"
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ-") + uuid4().hex[:8]
    directory = home / "results" / run_id
    directory.mkdir(parents=True)
    model = asdict(config.model)
    model["base_url"] = public_url(model["base_url"])
    manifest: Json = dict(
        schema_version=1,
        evaluation_run_id=run_id,
        started_at=datetime.now(UTC).isoformat(),
        selected=selected,
        nexus=implementation_identity(SUITE.parents[1]),
        model=model,
        limits=asdict(replace(config.limits, shell="/bin/sh")),
        context_policy="Context Runtime V0.1.1",
        cases=[case.identity() for case in cases],
    )
    write_json(directory / "manifest.json", manifest)
    results = [empty_result(case, run_id) for case in cases]
    save_report(directory, results, selected)
    print(f"Evaluation {run_id} · {len(cases)} case(s) · Context V0.1.1", flush=True)
    stop = False
    try:
        docker = await Docker.connect(cache)
    except (EnvironmentError, OSError, TimeoutError) as exc:
        for result in results:
            result["diagnostics"].append(
                dict(phase="prepare", category="setup_error", reason=str(exc))
            )
        stop = True
        docker = None
    for index, case in enumerate(cases):
        if not stop:
            print(f"[{index + 1}/{len(cases)}] {case.id} · running", flush=True)
            assert docker is not None
            results[index], stop = await run_case(case, run_id, directory, config, docker, cache)
            manifest["cases"][index] = results[index]["provenance"]
            write_json(directory / "manifest.json", manifest)
            print(f"[{index + 1}/{len(cases)}] {case.id} · {results[index]['status']}", flush=True)
        elif not results[index]["diagnostics"]:
            results[index]["diagnostics"].append(
                dict(
                    phase="prepare", category="setup_error", reason="Suite stopped before this case"
                )
            )
        write_json(directory / case.id / "result.json", results[index])
        save_report(directory, results, selected)
    print(save_report(directory, results, selected), end="", flush=True)
    statuses = {r["status"] for r in results}
    return (
        130
        if "ABORTED" in statuses
        else 2
        if statuses & {"ERROR", "NOT_RUN"}
        else 1
        if "FAIL" in statuses
        else 0
    )
