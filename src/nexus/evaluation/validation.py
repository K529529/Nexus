"""Fresh base + candidate + fixed hidden checks, independent of Agent outcome."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path, PurePosixPath

from nexus.core.types import Json
from nexus.evaluation.cases import SUITE, EvalCase, validator_spec
from nexus.evaluation.environment import CleanupError, Docker, Environment, EnvironmentError


def grade(targets: Json, observed: Json, exit_code: int) -> tuple[str, str]:
    required = targets["fail_to_pass"] + targets["pass_to_pass"]
    if not required or any(name not in observed for name in required):
        return "error", "Required test results missing"
    if any(observed[name] in {"skipped", "error"} for name in required):
        return "error", "Required test skipped or fixture/setup failed"
    if any(observed[name] not in {"passed", "failed"} for name in required):
        return "error", "Unrecognized test result"
    if exit_code not in (0, 1):
        return "error", f"Test runner exited {exit_code}"
    if exit_code or any(observed[name] != "passed" for name in required):
        return "failed", "Fixed checks failed"
    return "passed", "All required tests and retention checks passed"


def hidden_paths(patch: str) -> list[str]:
    paths = []
    for line in patch.splitlines():
        if line.startswith("+++ b/") or line.startswith("--- a/"):
            name = line[6:].split("\t", 1)[0]
            path = PurePosixPath(name)
            if (
                path.is_absolute()
                or ".." in path.parts
                or not name.startswith(
                    ("tests/", "testing/", "pvlib/tests/", "lib/matplotlib/tests/")
                )
            ):
                raise ValueError(f"Hidden patch must only change declared test paths: {name}")
            if name not in paths:
                paths.append(name)
    if not paths:
        raise ValueError("No hidden test paths")
    return paths


async def validate(case: EvalCase, patch: Path, output: Path, docker: Docker, cache: Path) -> Json:
    started = time.monotonic()
    environment = Environment(docker, case, cache)
    checks: list[Json] = []
    status, reason = "error", "Validation did not finish"
    log = output / "validator.log"
    spec = validator_spec(case)
    try:
        await environment.prepare(
            [
                (case.validator.parent, "/validator", True),
                (SUITE / "support", "/validator-support", True),
                (output, "/output", False),
            ]
        )
        if (await asyncio.to_thread(patch.stat)).st_size:
            await environment.checked(["git", "apply", "--binary", "/output/patch.diff"])
        # Candidate test edits cannot remove or overwrite fixed hidden checks.
        test_patch = case.validator.parent / spec["test_patch"]
        for path in hidden_paths(test_patch.read_text(encoding="utf-8")):
            code, _, _ = await docker.call(
                [
                    "exec",
                    "-w",
                    "/workspace",
                    environment.name,
                    "git",
                    "ls-files",
                    "--error-unmatch",
                    "--",
                    path,
                ]
            )
            if code == 0:
                await environment.checked(
                    ["git", "restore", "--source=HEAD", "--worktree", "--", path]
                )
            else:
                await environment.checked(["rm", "-f", "--", path])
        await environment.checked(["git", "apply", "/validator/" + spec["test_patch"]])
        with log.open("wb") as stream:
            for check in spec["checks"]:
                begin = time.monotonic()
                result_path = output / (check["name"] + ".json")
                args = [
                    "exec",
                    "-w",
                    "/workspace",
                    "-e",
                    "PYTHONPATH=/validator-support:/workspace/lib:/workspace/src:/workspace",
                    "-e",
                    "MYPYPATH=/workspace/src",
                    "-e",
                    "NEXUS_EVAL_TEST_RESULTS=/output/" + result_path.name,
                    environment.name,
                    *check["argv"],
                ]
                code, stdout, stderr = await docker.call(args, spec["timeout_seconds"])
                stream.write(("\nCHECK " + check["name"] + "\n").encode() + stdout + stderr)
                stream.flush()
                if check["kind"] == "tests":
                    if not result_path.is_file():
                        current, detail = "error", "Test result file missing"
                    else:
                        raw = json.loads(result_path.read_bytes())
                        current, detail = grade(spec["targets"], raw["tests"], code)
                        if raw["exit_code"] != code:
                            current, detail = "error", "Result/runner exit code mismatch"
                else:
                    current, detail = (
                        ("passed", "Check passed")
                        if code == 0
                        else ("failed", "Fixed type assertions failed")
                    )
                checks.append(
                    dict(
                        name=check["name"],
                        status=current,
                        reason=detail,
                        exit_code=code,
                        duration_s=time.monotonic() - begin,
                    )
                )
        statuses = {check["status"] for check in checks}
        status = "error" if "error" in statuses else "failed" if "failed" in statuses else "passed"
        reason = next((c["reason"] for c in checks if c["status"] != "passed"), "All checks passed")
    except (EnvironmentError, OSError, ValueError, KeyError, TimeoutError) as exc:
        status, reason = "error", f"{type(exc).__name__}: {exc}"
        with log.open("ab") as stream:
            stream.write(("\n" + reason).encode("utf-8"))
    finally:
        try:
            await environment.close()
        except (EnvironmentError, OSError, TimeoutError) as exc:
            raise CleanupError(f"Validator cleanup failed: {exc}") from exc
    return dict(status=status, reason=reason, checks=checks, duration_s=time.monotonic() - started)
