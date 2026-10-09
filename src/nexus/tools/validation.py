"""Small opt-in evidence reader; no test runner, command rewriting or coverage inference."""

from __future__ import annotations

import hashlib
import re
import shlex
import xml.etree.ElementTree as ET
from pathlib import Path, PureWindowsPath

from nexus.core.types import Json

PURPOSES = {"inspect", "validate", "mutate", "other"}
MAX_SCOPE = 100


def scope_paths(value: object) -> list[str]:
    if not isinstance(value, list) or len(value) > MAX_SCOPE:
        raise ValueError("scope must be an array of at most 100 workspace-relative file paths")
    result = []
    for path in value:
        if (
            not isinstance(path, str)
            or not path
            or len(path) > 512
            or Path(path).is_absolute()
            or PureWindowsPath(path).drive
            or ".." in path.replace("\\", "/").split("/")
            or any(c in path for c in "*?\x00")
        ):
            raise ValueError("scope requires explicit workspace-relative files, not globs")
        result.append(path.replace("\\", "/"))
    return list(dict.fromkeys(result))


def fingerprints(workspace: Path, paths: list[str]) -> dict[str, str | None]:
    result: dict[str, str | None] = {}
    root = workspace.resolve()
    for name in paths:
        path = root / name
        try:
            if not path.resolve().is_relative_to(root) or path.is_symlink():
                result[name] = None
            elif not path.exists():
                result[name] = "absent"
            elif path.is_file() and path.stat().st_size <= 1024 * 1024:
                result[name] = hashlib.sha256(path.read_bytes()).hexdigest()
            else:
                result[name] = None
        except OSError:
            result[name] = None
    return result


def report_identity(path: Path | None) -> tuple[int, int, str] | None:
    if path is None:
        return None
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
            return None
        stat = path.stat()
        return stat.st_mtime_ns, stat.st_size, hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def pytest_invocation(command: str, cwd: str) -> tuple[list[str], Path] | None:
    """One pytest command, optionally preceded by a single cd; never a shell interpreter."""
    command = command.strip()
    if command.startswith("& "):
        command = command[2:]
    try:
        argv = shlex.split(command)
        directory = Path(cwd)
        if len(argv) >= 4 and argv[0] == "cd" and argv[2] == "&&":
            directory = (directory / argv[1]).resolve()
            argv = argv[3:]
        if any(c in arg for arg in argv for c in "\n\r;|&<>`$"):
            return None
        executable = argv.pop(0).replace("\\", "/").rsplit("/", 1)[-1]
        if re.fullmatch(r"python(?:[0-9.]+)?(?:\.exe)?", executable):
            if argv[:2] != ["-m", "pytest"]:
                return None
            argv = argv[2:]
        elif executable not in {"pytest", "pytest.exe", "py.test"}:
            return None
        return argv, directory
    except (ValueError, IndexError, OSError):
        return None


def direct_pytest_report(command: str, report: Path, cwd: str) -> bool:
    invocation = pytest_invocation(command, cwd)
    if invocation is None:
        return False
    argv, directory = invocation
    try:
        reports = []
        for i, arg in enumerate(argv):
            if arg in {"--junitxml", "--junit-xml"}:
                reports.append(argv[i + 1])
            elif arg.startswith(("--junitxml=", "--junit-xml=")):
                reports.append(arg.split("=", 1)[1])
        return len(reports) == 1 and (directory / reports[0]).resolve() == report
    except (IndexError, OSError):
        return False


def pytest_summary(data: Json) -> Json | None:
    if (
        pytest_invocation(data["command"], data["cwd"]) is None
        or data.get("truncated")
        or data.get("decode_replaced")
        or data["timed_out"]
        or data["cancelled"]
        or data["cleanup_incomplete"]
    ):
        return None
    output = re.sub(r"\x1b\[[0-9;]*m", "", data.get("stdout", "")).strip()
    if not output:
        return None
    summary = output.splitlines()[-1].strip("= \t")
    match = re.fullmatch(r"(.+) in [0-9.]+s(?: \([0-9:]+\))?", summary)
    if not match:
        return None
    counts = {}
    for item in match[1].split(", "):
        part = re.fullmatch(
            r"(\d+) (passed|failed|errors?|skipped|xfailed|xpassed|deselected|warnings?)", item
        )
        if not part or part[2] in counts:
            return None
        counts[part[2]] = int(part[1])
    passed = counts.get("passed", 0) + counts.get("xpassed", 0)
    failed = counts.get("failed", 0)
    errors = counts.get("error", 0) + counts.get("errors", 0)
    skipped = counts.get("skipped", 0) + counts.get("xfailed", 0)
    ok = data["exit_code"] == 0 and passed > 0 and not failed and not errors
    return {
        "level": "behavioral",
        "result": "passed" if ok else "failed_or_no_tests",
        "tests": {
            "total": passed + failed + errors + skipped,
            "passed": passed,
            "failed": failed,
            "errors": errors,
            "skipped": skipped,
        },
        "behavioral_pass": ok,
        "scope_source": "agent_declared",
        "evidence": "pytest_result_summary",
    }


def validation_evidence(
    data: Json,
    report: Path | None,
    before: tuple[int, int, str] | None,
) -> Json:
    result: Json = {
        "level": "partial",
        "result": "command_succeeded" if data["exit_code"] == 0 else "failed",
        "tests": None,
        "behavioral_pass": False,
        "scope_source": "agent_declared",
        "evidence": "No reliable test evidence; exit zero alone is not behavioral validation.",
    }
    result = pytest_summary(data) or result
    identity = report_identity(report)
    if (
        report is None
        or identity is None
        or identity == before
        or not direct_pytest_report(data["command"], report, data["cwd"])
    ):
        return result
    try:
        raw = report.read_bytes()
        if b"<!DOCTYPE" in raw or b"<!ENTITY" in raw:
            return result
        root = ET.fromstring(raw)
        if root.tag not in {"testsuite", "testsuites"}:
            return result
        cases = list(root.iter("testcase"))
        if not cases:
            return result
        failures = sum(c.find("failure") is not None for c in cases)
        errors = sum(c.find("error") is not None for c in cases)
        skipped = sum(c.find("skipped") is not None for c in cases)
        # Check leaf suite counts so a partial/inconsistent document cannot clear debt.
        suites = [s for s in root.iter("testsuite") if s.find("testsuite") is None]
        if not suites or sum(int(s.attrib["tests"]) for s in suites) != len(cases):
            return result
        for suite in suites:
            members = list(suite.iter("testcase"))
            for key, tag in (("failures", "failure"), ("errors", "error"), ("skipped", "skipped")):
                if int(suite.attrib.get(key, "0")) != sum(c.find(tag) is not None for c in members):
                    return result
    except (OSError, ET.ParseError, ValueError, KeyError):
        return result
    passed = sum(
        not any(c.find(t) is not None for t in ("failure", "error", "skipped")) for c in cases
    )
    ok = (
        data["exit_code"] == 0
        and not data["timed_out"]
        and not data["cancelled"]
        and not data["cleanup_incomplete"]
        and passed > 0
        and not failures
        and not errors
    )
    return {
        "level": "behavioral",
        "result": "passed" if ok else "failed_or_no_tests",
        "tests": {
            "total": len(cases),
            "passed": passed,
            "failed": failures,
            "errors": errors,
            "skipped": skipped,
        },
        "behavioral_pass": ok,
        "scope_source": "agent_declared",
        "evidence": "fresh_pytest_junit_xml",
        "report_sha256": identity[2],
    }
