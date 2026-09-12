"""Sandbox-backed evaluation harness with a closed command grammar."""

from __future__ import annotations

import hashlib
import os
import stat
import sys
import unicodedata
from pathlib import Path, PurePosixPath, PureWindowsPath

from nexus.domain.tooling import PolicyDecision, RiskLevel, SandboxRequest
from nexus.evaluation.fixtures import copy_fixture_source
from nexus.evaluation.models import (
    EvalDiscoveryResult,
    EvalDiscoveryStatus,
    EvalForbiddenPathEvidence,
    EvalHarnessCommand,
    EvalHarnessCommandResult,
    EvalHarnessOperation,
    EvalPytestNodeOutcome,
    EvalPytestNodeResult,
    EvalRepositoryEvidence,
    EvalValidationResult,
    EvalValidationStatus,
    FileFingerprint,
    ForbiddenPathSnapshot,
    RepositoryState,
)
from nexus.infrastructure.sandbox.local_process import LocalProcessSandbox
from nexus.security.executables import TrustedExecutables
from nexus.security.workspace import WorkspaceGuard

DEFAULT_EVAL_VALIDATION_TIMEOUT_SECONDS = 120
_SUMMARY_LIMIT = 16_384
_IGNORED_DIRS = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".nexus-eval-runtime",
}
_IGNORED_FILES = {".coverage"}
_GIT_FORMS = {
    EvalHarnessOperation.GIT_INIT: ("git", "init"),
    EvalHarnessOperation.GIT_ADD_BASELINE: ("git", "add", "--all"),
    EvalHarnessOperation.GIT_COMMIT_BASELINE: (
        "git",
        "-c",
        "user.name=Nexus Eval",
        "-c",
        "user.email=nexus-eval@local.invalid",
        "commit",
        "-m",
        "nexus-eval-baseline",
    ),
    EvalHarnessOperation.GIT_STATUS: (
        "git",
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    ),
    EvalHarnessOperation.GIT_DIFF: ("git", "diff", "--no-ext-diff", "--binary"),
    EvalHarnessOperation.GIT_LS_FILES: ("git", "ls-files"),
}


class EvalHarnessCommandPolicy:
    """Allow only the exact v0.8 Harness grammar."""

    def __init__(self, executables: TrustedExecutables) -> None:
        self._executables = executables

    def classify(self, *, operation: str, arguments: dict[str, object]) -> RiskLevel:
        raw = arguments.get("argv")
        if (
            not isinstance(raw, (list, tuple))
            or not raw
            or not all(isinstance(item, str) for item in raw)
        ):
            return RiskLevel.DANGEROUS
        argv = tuple(raw)
        try:
            harness_operation = EvalHarnessOperation(operation)
        except ValueError:
            return RiskLevel.DANGEROUS
        family = self._family(argv[0])
        if family is None:
            return RiskLevel.DANGEROUS
        if harness_operation in _GIT_FORMS:
            expected = _GIT_FORMS[harness_operation]
            allowed = family == "git" and argv[1:] == expected[1:]
        elif harness_operation is EvalHarnessOperation.TEST_DISCOVERY:
            allowed = family == "pytest" and self._valid_discovery(argv[1:])
        elif harness_operation is EvalHarnessOperation.VALIDATION:
            allowed = self._valid_validation(family, argv[1:])
        else:
            allowed = False
        return RiskLevel.SAFE if allowed else RiskLevel.DANGEROUS

    def _family(self, executable: str) -> str | None:
        configured = {
            "git": self._executables.git,
            "pytest": self._executables.pytest,
            "ruff": self._executables.ruff,
            "mypy": self._executables.mypy,
        }
        candidate = Path(executable)
        if candidate.is_absolute():
            normalized = os.path.normcase(str(candidate.resolve(strict=False)))
            for name, trusted in configured.items():
                if trusted and normalized == os.path.normcase(trusted):
                    return name
            return None
        name = candidate.name.casefold().removesuffix(".exe")
        return name if name in configured and configured[name] is not None else None

    @staticmethod
    def _valid_discovery(args: tuple[str, ...]) -> bool:
        return (
            len(args) >= 3
            and args[:2] == ("--collect-only", "-q")
            and all(_safe_path(item, node=False) for item in args[2:])
        )

    @staticmethod
    def _valid_validation(family: str, args: tuple[str, ...]) -> bool:
        if family == "ruff":
            return (
                len(args) >= 2
                and args[0] == "check"
                and all(_safe_path(item, node=False) for item in args[1:])
            )
        if family == "mypy":
            return bool(args) and all(_safe_path(item, node=False) for item in args)
        if family != "pytest":
            return False
        targets: list[str] = []
        index = 0
        while index < len(args):
            argument = args[index]
            if argument in {"-q", "-v", "-vv", "--disable-warnings"}:
                index += 1
            elif argument == "--maxfail":
                if index + 1 >= len(args) or not _bounded_int(args[index + 1]):
                    return False
                index += 2
            elif argument.startswith("--maxfail="):
                if not _bounded_int(argument.partition("=")[2]):
                    return False
                index += 1
            elif argument.startswith("-"):
                return False
            else:
                targets.append(argument)
                index += 1
        return bool(targets) and all(_safe_path(item, node=True) for item in targets)


class EvalHarnessExecutor:
    """Adapter from typed Harness commands to the existing sandbox."""

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace.resolve(strict=True)
        self._executables = _resolve_eval_executables(self._workspace)
        policy = EvalHarnessCommandPolicy(self._executables)
        self._workspace_guard = WorkspaceGuard(self._workspace)
        self._sandbox = LocalProcessSandbox(self._workspace_guard, policy, self._executables)

    async def execute(self, command: EvalHarnessCommand) -> EvalHarnessCommandResult:
        if command.cwd.resolve(strict=True) != self._workspace:
            return _denied(command, "WORKSPACE_PATH_DENIED")
        if not _command_paths_are_contained(command, self._workspace):
            return _denied(command, "WORKSPACE_PATH_DENIED")
        result = await self._sandbox.execute(
            SandboxRequest(
                operation=command.operation.value,
                argv=list(command.argv),
                cwd=".",
                timeout_seconds=command.timeout_seconds,
            )
        )
        allowed = result.policy_decision is PolicyDecision.ALLOWED
        denial_code = None
        infrastructure_error = None
        if not allowed:
            denial_code = result.error.code if result.error else "COMMAND_DENIED"
        elif result.error and result.error.code == "SANDBOX_EXECUTION_ERROR":
            infrastructure_error = result.error.code
        return EvalHarnessCommandResult(
            operation=command.operation,
            allowed=allowed,
            denial_code=denial_code,
            exit_code=result.exit_code,
            stdout_summary=_summary(result.stdout),
            stderr_summary=_summary(result.stderr),
            duration_ms=result.duration_ms,
            timed_out=result.timed_out,
            infrastructure_error=infrastructure_error,
        )


class EvaluationHarness:
    """Own isolated fixture setup, checks, and authoritative snapshots."""

    @staticmethod
    def copy_fixture(source: Path, destination: Path) -> None:

        copy_fixture_source(source, destination)

    @staticmethod
    async def initialize_git(workspace: Path) -> tuple[EvalHarnessCommandResult, ...]:
        executor = EvalHarnessExecutor(workspace)
        results: list[EvalHarnessCommandResult] = []
        for operation in (
            EvalHarnessOperation.GIT_INIT,
            EvalHarnessOperation.GIT_ADD_BASELINE,
            EvalHarnessOperation.GIT_COMMIT_BASELINE,
        ):
            result = await executor.execute(
                EvalHarnessCommand(operation, _GIT_FORMS[operation], workspace, 30)
            )
            results.append(result)
            if (
                not result.allowed
                or result.timed_out
                or result.infrastructure_error
                or result.exit_code != 0
            ):
                break
        return tuple(results)

    @staticmethod
    async def validation(
        workspace: Path,
        command: tuple[str, ...],
        timeout_seconds: int,
        expected_node_ids: tuple[str, ...] = (),
    ) -> EvalValidationResult:
        if not command:
            return _not_run_validation()
        result = await EvalHarnessExecutor(workspace).execute(
            EvalHarnessCommand(EvalHarnessOperation.VALIDATION, command, workspace, timeout_seconds)
        )
        status = _validation_status(result)
        nodes = _parse_node_results(result.stdout_summary, expected_node_ids)
        return EvalValidationResult(
            status=status,
            command=command,
            exit_code=result.exit_code,
            stdout_summary=result.stdout_summary,
            stderr_summary=result.stderr_summary,
            duration_ms=result.duration_ms,
            timeout_seconds=timeout_seconds,
            infrastructure_error=result.infrastructure_error,
            pytest_node_results=nodes,
        )

    @staticmethod
    async def discovery(
        workspace: Path, command: tuple[str, ...], timeout_seconds: int
    ) -> EvalDiscoveryResult:
        if not command:
            return _not_run_discovery()
        result = await EvalHarnessExecutor(workspace).execute(
            EvalHarnessCommand(
                EvalHarnessOperation.TEST_DISCOVERY, command, workspace, timeout_seconds
            )
        )
        status = _discovery_status(result)
        collected = (
            _parse_collected_nodes(result.stdout_summary)
            if status is EvalDiscoveryStatus.PASSED
            else ()
        )
        return EvalDiscoveryResult(
            status=status,
            command=command,
            exit_code=result.exit_code,
            collected_node_ids=collected,
            stdout_summary=result.stdout_summary,
            stderr_summary=result.stderr_summary,
            duration_ms=result.duration_ms,
            timeout_seconds=timeout_seconds,
            infrastructure_error=result.infrastructure_error,
        )


def snapshot_repository(root: Path, forbidden_files: tuple[str, ...]) -> RepositoryState:
    root = root.resolve(strict=True)
    files: dict[str, FileFingerprint] = {}
    for current, directory_names, file_names in os.walk(root, followlinks=False):
        current_path = Path(current)
        relative_dir = current_path.relative_to(root)
        directory_names[:] = sorted(
            name for name in directory_names if name != ".git" and name not in _IGNORED_DIRS
        )
        for name in tuple(directory_names):
            path = current_path / name
            relative = (relative_dir / name).as_posix()
            files[relative] = _fingerprint(path, relative)
            if path.is_symlink():
                directory_names.remove(name)
        for name in sorted(file_names):
            if name in _IGNORED_FILES:
                continue
            path = current_path / name
            relative = (relative_dir / name).as_posix()
            files[relative] = _fingerprint(path, relative)
    forbidden = {path: _forbidden_snapshot(root, path) for path in sorted(forbidden_files)}
    return RepositoryState(files=dict(sorted(files.items())), forbidden_snapshots=forbidden)


def compare_repository_states(
    before: RepositoryState,
    after: RepositoryState,
    text_paths: tuple[str, ...],
    root: Path,
) -> EvalRepositoryEvidence:
    before_paths = set(before.files)
    after_paths = set(after.files)
    created = tuple(sorted(after_paths - before_paths))
    deleted = tuple(sorted(before_paths - after_paths))
    changed = tuple(
        sorted(
            path for path in before_paths & after_paths if before.files[path] != after.files[path]
        )
    )
    forbidden_evidence = {
        path: EvalForbiddenPathEvidence(
            path=path,
            before=before.forbidden_snapshots[path],
            after=after.forbidden_snapshots[path],
            changed=before.forbidden_snapshots[path] != after.forbidden_snapshots[path],
        )
        for path in sorted(before.forbidden_snapshots)
    }
    for forbidden_path in _workspace_escaping_symlinks(after, root):
        if forbidden_path in forbidden_evidence:
            continue
        before_snapshot = _snapshot_from_fingerprint(
            forbidden_path, before.files.get(forbidden_path)
        )
        after_snapshot = _snapshot_from_fingerprint(forbidden_path, after.files[forbidden_path])
        forbidden_evidence[forbidden_path] = EvalForbiddenPathEvidence(
            path=forbidden_path,
            before=before_snapshot,
            after=after_snapshot,
            changed=before_snapshot != after_snapshot,
        )
    forbidden = tuple(forbidden_evidence[path] for path in sorted(forbidden_evidence))
    texts: dict[str, str] = {}
    for relative in sorted(set(text_paths)):
        path = root / PurePosixPath(relative)
        try:
            if path.is_file() and not path.is_symlink():
                value = path.read_bytes().decode("utf-8", errors="strict")
                texts[relative] = unicodedata.normalize(
                    "NFC", value.replace("\r\n", "\n").replace("\r", "\n")
                )
        except (OSError, UnicodeDecodeError):
            continue
    return EvalRepositoryEvidence(changed, created, deleted, forbidden, texts)


def _safe_path(value: str, *, node: bool) -> bool:
    file_part = value.split("::", 1)[0] if node else value
    path = PurePosixPath(file_part)
    if (
        not value
        or not file_part
        or path.is_absolute()
        or PureWindowsPath(file_part).is_absolute()
        or ".." in path.parts
        or _contains_shell_operator(value)
    ):
        return False
    if "\\" in file_part or file_part.startswith("-"):
        return False
    return node or "::" not in value


def _contains_shell_operator(value: str) -> bool:
    return (
        any(token in value for token in ("&&", "||", "|", ";", "`", "$("))
        or value.startswith((">", "<", "&"))
        or value.startswith(("1>", "2>"))
    )


def _bounded_int(value: str) -> bool:
    try:
        return 1 <= int(value) <= 10
    except ValueError:
        return False


def _command_paths_are_contained(command: EvalHarnessCommand, root: Path) -> bool:
    for value in _command_path_arguments(command):
        file_part = value.split("::", 1)[0]
        candidate = root / PurePosixPath(file_part)
        if not candidate.exists() and not candidate.is_symlink():
            continue
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            return False
        if not _is_within_root(resolved, root):
            return False
    return True


def _command_path_arguments(command: EvalHarnessCommand) -> tuple[str, ...]:
    if command.operation is EvalHarnessOperation.TEST_DISCOVERY:
        return command.argv[3:]
    if command.operation is not EvalHarnessOperation.VALIDATION:
        return ()
    family = Path(command.argv[0]).name.casefold().removesuffix(".exe")
    arguments = command.argv[1:]
    if family == "ruff":
        return arguments[1:]
    if family == "mypy":
        return arguments
    if family != "pytest":
        return ()
    paths: list[str] = []
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--maxfail":
            index += 2
        elif argument.startswith("-"):
            index += 1
        else:
            paths.append(argument)
            index += 1
    return tuple(paths)


def _workspace_escaping_symlinks(state: RepositoryState, root: Path) -> tuple[str, ...]:
    escaping: list[str] = []
    for relative, fingerprint in state.files.items():
        if fingerprint.kind != "symlink" or fingerprint.symlink_target is None:
            continue
        link_parent = root / PurePosixPath(relative).parent
        target = Path(fingerprint.symlink_target)
        resolved = (target if target.is_absolute() else link_parent / target).resolve(strict=False)
        if not _is_within_root(resolved, root):
            escaping.append(relative)
    return tuple(sorted(escaping))


def _snapshot_from_fingerprint(
    path: str, fingerprint: FileFingerprint | None
) -> ForbiddenPathSnapshot:
    if fingerprint is None:
        return ForbiddenPathSnapshot(path, False, None, None, None, None)
    return ForbiddenPathSnapshot(
        path,
        True,
        fingerprint.kind,
        fingerprint.content_sha256,
        fingerprint.executable,
        fingerprint.symlink_target,
    )


def _is_within_root(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath(
            [os.path.normcase(str(path)), os.path.normcase(str(root))]
        ) == os.path.normcase(str(root))
    except ValueError:
        return False


def _resolve_eval_executables(workspace: Path) -> TrustedExecutables:
    resolved = TrustedExecutables.resolve(workspace)
    script_directory = Path(sys.executable).resolve(strict=True).parent

    def adjacent(name: str, current: str | None) -> str | None:
        if current is not None:
            return current
        candidates = (
            script_directory / f"{name}.exe",
            script_directory / name,
        )
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate.resolve(strict=True))
        return None

    return TrustedExecutables(
        python=resolved.python,
        uv=resolved.uv,
        git=resolved.git,
        pytest=adjacent("pytest", resolved.pytest),
        ruff=adjacent("ruff", resolved.ruff),
        mypy=adjacent("mypy", resolved.mypy),
    )


def _fingerprint(path: Path, relative: str) -> FileFingerprint:
    mode = path.lstat().st_mode
    if path.is_symlink():
        return FileFingerprint(relative, "symlink", None, False, os.readlink(path))
    if stat.S_ISREG(mode):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return FileFingerprint(relative, "file", digest, bool(mode & stat.S_IXUSR), None)
    if stat.S_ISDIR(mode):
        return FileFingerprint(relative, "directory", None, bool(mode & stat.S_IXUSR), None)
    return FileFingerprint(relative, "other", None, bool(mode & stat.S_IXUSR), None)


def _forbidden_snapshot(root: Path, relative: str) -> ForbiddenPathSnapshot:
    path = root / PurePosixPath(relative)
    if not path.exists() and not path.is_symlink():
        return ForbiddenPathSnapshot(relative, False, None, None, None, None)
    fingerprint = _fingerprint(path, relative)
    return ForbiddenPathSnapshot(
        relative,
        True,
        fingerprint.kind,
        fingerprint.content_sha256,
        fingerprint.executable,
        fingerprint.symlink_target,
    )


def _summary(value: str) -> str:
    return value if len(value) <= _SUMMARY_LIMIT else value[:_SUMMARY_LIMIT]


def _denied(command: EvalHarnessCommand, code: str) -> EvalHarnessCommandResult:
    return EvalHarnessCommandResult(command.operation, False, code, None, "", "", 0, False, None)


def _validation_status(result: EvalHarnessCommandResult) -> EvalValidationStatus:
    if result.infrastructure_error:
        return EvalValidationStatus.INFRASTRUCTURE_ERROR
    if not result.allowed:
        return EvalValidationStatus.FAILED
    if result.timed_out:
        return EvalValidationStatus.TIMED_OUT
    return EvalValidationStatus.PASSED if result.exit_code == 0 else EvalValidationStatus.FAILED


def _discovery_status(result: EvalHarnessCommandResult) -> EvalDiscoveryStatus:
    if result.infrastructure_error:
        return EvalDiscoveryStatus.INFRASTRUCTURE_ERROR
    if not result.allowed:
        return EvalDiscoveryStatus.FAILED
    if result.timed_out:
        return EvalDiscoveryStatus.TIMED_OUT
    return EvalDiscoveryStatus.PASSED if result.exit_code == 0 else EvalDiscoveryStatus.FAILED


def _not_run_validation() -> EvalValidationResult:
    return EvalValidationResult(EvalValidationStatus.NOT_RUN, (), None, "", "", 0, None, None)


def _not_run_discovery() -> EvalDiscoveryResult:
    return EvalDiscoveryResult(EvalDiscoveryStatus.NOT_RUN, (), None, (), "", "", 0, None, None)


def _parse_collected_nodes(output: str) -> tuple[str, ...]:
    nodes = {
        line.strip()
        for line in output.splitlines()
        if "::" in line and not line.lstrip().startswith(("<", "="))
    }
    return tuple(sorted(nodes))


def _parse_node_results(output: str, expected: tuple[str, ...]) -> tuple[EvalPytestNodeResult, ...]:
    tokens = {
        "PASSED": EvalPytestNodeOutcome.PASSED,
        "FAILED": EvalPytestNodeOutcome.FAILED,
        "SKIPPED": EvalPytestNodeOutcome.SKIPPED,
        "XFAIL": EvalPytestNodeOutcome.XFAILED,
        "XPASS": EvalPytestNodeOutcome.XPASSED,
        "ERROR": EvalPytestNodeOutcome.ERROR,
    }
    found: dict[str, EvalPytestNodeOutcome] = {}
    for line in output.splitlines():
        normalized = line.strip()
        for node in expected:
            prefix = f"{node} "
            if normalized.startswith(prefix):
                token = normalized[len(prefix) :].split(maxsplit=1)[0]
                if token in tokens:
                    found[node] = tokens[token]
    return tuple(
        EvalPytestNodeResult(node, found.get(node, EvalPytestNodeOutcome.NOT_RUN))
        for node in expected
    )
