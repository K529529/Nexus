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


def direct_pytest_report(command: str, report: Path, cwd: str) -> bool:
    """Recognize only one direct pytest invocation; ambiguous shell programs remain partial."""
    command = command.strip()
    if command.startswith("& "):
        command = command[2:]
    if any(c in command for c in "\n\r;|&<>`$"):
        return False
    try:
        argv = shlex.split(command)
        executable = argv.pop(0).replace("\\", "/").rsplit("/", 1)[-1]
        if re.fullmatch(r"python(?:[0-9.]+)?(?:\.exe)?", executable):
            if argv[:2] != ["-m", "pytest"]:
                return False
            argv = argv[2:]
        elif executable not in {"pytest", "pytest.exe", "py.test"}:
            return False
        reports = []
        for i, arg in enumerate(argv):
            if arg in {"--junitxml", "--junit-xml"}:
                reports.append(argv[i + 1])
            elif arg.startswith(("--junitxml=", "--junit-xml=")):
                reports.append(arg.split("=", 1)[1])
        return len(reports) == 1 and (Path(cwd) / reports[0]).resolve() == report
    except (ValueError, IndexError, OSError):
        return False


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
        "evidence": "No fresh supported test report; exit zero alone is not behavioral validation.",
    }
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
