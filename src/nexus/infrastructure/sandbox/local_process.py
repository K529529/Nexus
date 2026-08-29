"""Standard-library local process isolation behind the SandboxExecutor port."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import time
from asyncio.subprocess import Process
from collections.abc import Callable
from pathlib import Path
from typing import cast

from nexus.domain.ports.tooling import CommandPolicy
from nexus.domain.tooling import (
    PolicyDecision,
    RiskLevel,
    SandboxRequest,
    SandboxResult,
    ToolError,
)
from nexus.errors import NexusError
from nexus.security.executables import TrustedExecutables
from nexus.security.workspace import WorkspaceGuard

_OUTPUT_LIMIT_BYTES = 1_048_576


class LocalProcessSandbox:
    def __init__(
        self,
        workspace_guard: WorkspaceGuard,
        command_policy: CommandPolicy,
        executables: TrustedExecutables,
    ) -> None:
        self._workspace_guard = workspace_guard
        self._command_policy = command_policy
        self._executables = executables

    async def execute(self, request: SandboxRequest) -> SandboxResult:
        started = time.perf_counter()
        normalized_argv = self._executables.normalize_argv(request.argv)
        arguments: dict[str, object] = {
            "argv": normalized_argv,
            "cwd": request.cwd,
            "timeout_seconds": request.timeout_seconds,
        }
        proposed_risk = self._command_policy.classify(
            operation=request.operation,
            arguments=arguments,
        )
        if proposed_risk is RiskLevel.DANGEROUS:
            return _denied_result(request, normalized_argv, started)
        try:
            cwd = self._workspace_guard.resolve_existing(
                request.cwd,
                require_directory=True,
            )
        except NexusError as exc:
            return SandboxResult(
                argv=normalized_argv,
                cwd=request.cwd,
                risk_level=RiskLevel.DANGEROUS,
                policy_decision=PolicyDecision.DENIED,
                exit_code=None,
                stdout="",
                stderr="",
                duration_ms=_duration_ms(started),
                timed_out=False,
                output_truncated=False,
                error=_tool_error(exc),
            )

        if os.name == "nt" and isinstance(
            asyncio.get_running_loop(), asyncio.SelectorEventLoop
        ):
            return await asyncio.to_thread(
                self._execute_on_windows_proactor,
                request,
                normalized_argv,
                cwd,
                proposed_risk,
                started,
            )
        return await self._execute_allowed(
            request,
            normalized_argv,
            cwd,
            proposed_risk,
            started,
        )

    def _execute_on_windows_proactor(
        self,
        request: SandboxRequest,
        normalized_argv: list[str],
        cwd: Path,
        proposed_risk: RiskLevel,
        started: float,
    ) -> SandboxResult:
        """Isolate Windows subprocess support from Day 2's selector loop."""

        loop_factory = cast(
            Callable[[], asyncio.AbstractEventLoop],
            asyncio.ProactorEventLoop,
        )
        loop = loop_factory()
        try:
            return loop.run_until_complete(
                self._execute_allowed(
                    request,
                    normalized_argv,
                    cwd,
                    proposed_risk,
                    started,
                )
            )
        finally:
            loop.close()

    async def _execute_allowed(
        self,
        request: SandboxRequest,
        normalized_argv: list[str],
        cwd: Path,
        proposed_risk: RiskLevel,
        started: float,
    ) -> SandboxResult:

        process: Process | None = None
        stdout_task: asyncio.Task[tuple[bytes, bool]] | None = None
        stderr_task: asyncio.Task[tuple[bytes, bool]] | None = None
        try:
            process = await self._start_process(normalized_argv, cwd)
            assert process.stdout is not None
            assert process.stderr is not None
            stdout_task = asyncio.create_task(_read_bounded(process.stdout))
            stderr_task = asyncio.create_task(_read_bounded(process.stderr))
            try:
                await asyncio.wait_for(process.wait(), timeout=request.timeout_seconds)
            except TimeoutError:
                await _terminate(process)
                stdout, stdout_truncated = await _finish_reader(stdout_task)
                stderr, stderr_truncated = await _finish_reader(stderr_task)
                return SandboxResult(
                    argv=normalized_argv,
                    cwd=self._workspace_guard.relative_display(cwd),
                    risk_level=proposed_risk,
                    policy_decision=PolicyDecision.ALLOWED,
                    exit_code=None,
                    stdout=_decode(stdout),
                    stderr=_decode(stderr),
                    duration_ms=_duration_ms(started),
                    timed_out=True,
                    output_truncated=stdout_truncated or stderr_truncated,
                    error=ToolError(
                        "SANDBOX_TIMEOUT",
                        "The allowed command exceeded its timeout.",
                        True,
                    ),
                )
            stdout, stdout_truncated = await _finish_reader(stdout_task)
            stderr, stderr_truncated = await _finish_reader(stderr_task)
            exit_code = process.returncode
            error = (
                None
                if exit_code == 0
                else ToolError(
                    "COMMAND_EXIT_NONZERO",
                    "The allowed command exited with a non-zero status.",
                    False,
                )
            )
            return SandboxResult(
                argv=normalized_argv,
                cwd=self._workspace_guard.relative_display(cwd),
                risk_level=proposed_risk,
                policy_decision=PolicyDecision.ALLOWED,
                exit_code=exit_code,
                stdout=_decode(stdout),
                stderr=_decode(stderr),
                duration_ms=_duration_ms(started),
                timed_out=False,
                output_truncated=stdout_truncated or stderr_truncated,
                error=error,
            )
        except asyncio.CancelledError:
            if process is not None and process.returncode is None:
                await _terminate(process)
            raise
        except Exception:
            if process is not None and process.returncode is None:
                await _terminate(process)
            _cancel_reader(stdout_task)
            _cancel_reader(stderr_task)
            return SandboxResult(
                argv=normalized_argv,
                cwd=self._workspace_guard.relative_display(cwd),
                risk_level=proposed_risk,
                policy_decision=PolicyDecision.ALLOWED,
                exit_code=None,
                stdout="",
                stderr="",
                duration_ms=_duration_ms(started),
                timed_out=False,
                output_truncated=False,
                error=ToolError(
                    "SANDBOX_EXECUTION_ERROR",
                    "The allowed command could not be safely executed.",
                    False,
                ),
            )

    async def _start_process(self, argv: list[str], cwd: Path) -> Process:
        environment = _minimum_environment(self._workspace_guard.root)
        if os.name == "nt":
            return await asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        return await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )


async def _read_bounded(stream: asyncio.StreamReader) -> tuple[bytes, bool]:
    retained = bytearray()
    truncated = False
    while chunk := await stream.read(65_536):
        remaining = _OUTPUT_LIMIT_BYTES - len(retained)
        if remaining > 0:
            retained.extend(chunk[:remaining])
        if len(chunk) > remaining:
            truncated = True
    return bytes(retained), truncated


async def _finish_reader(task: asyncio.Task[tuple[bytes, bool]]) -> tuple[bytes, bool]:
    try:
        return await asyncio.wait_for(task, timeout=2.0)
    except TimeoutError:
        task.cancel()
        return b"", True


def _cancel_reader(task: asyncio.Task[tuple[bytes, bool]] | None) -> None:
    if task is not None and not task.done():
        task.cancel()


async def _terminate(process: Process) -> None:
    if process.returncode is not None:
        return
    try:
        if os.name != "nt":
            kill_process_group = getattr(os, "killpg")  # noqa: B009
            kill_process_group(process.pid, getattr(signal, "SIGKILL"))  # noqa: B009
        else:
            process.kill()
    except ProcessLookupError:
        pass
    await process.wait()


def _minimum_environment(workspace: Path) -> dict[str, str]:
    keys = ["PATH"]
    keys.extend(
        ["SystemRoot", "WINDIR", "PATHEXT", "TEMP", "TMP"]
        if os.name == "nt"
        else ["LANG", "LC_ALL", "TMPDIR"]
    )
    environment = {key: os.environ[key] for key in keys if key in os.environ}
    if "PATH" in environment:
        entries = []
        for value in environment["PATH"].split(os.pathsep):
            if not value or not Path(value).is_absolute():
                continue
            resolved = Path(value).resolve(strict=False)
            if not _is_within(resolved, workspace):
                entries.append(str(resolved))
        environment["PATH"] = os.pathsep.join(entries)
    environment.update(
        {
            "PYTHONIOENCODING": "utf-8",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_CONFIG_GLOBAL": os.devnull,
        }
    )
    return environment


def _denied_result(
    request: SandboxRequest,
    argv: list[str],
    started: float,
) -> SandboxResult:
    return SandboxResult(
        argv=argv,
        cwd=request.cwd,
        risk_level=RiskLevel.DANGEROUS,
        policy_decision=PolicyDecision.DENIED,
        exit_code=None,
        stdout="",
        stderr="",
        duration_ms=_duration_ms(started),
        timed_out=False,
        output_truncated=False,
        error=ToolError(
            "COMMAND_DENIED",
            "The command is not allowed by the Day 3 security policy.",
            False,
        ),
    )


def _tool_error(error: NexusError) -> ToolError:
    return ToolError(error.code, str(error), error.retryable)


def _duration_ms(started: float) -> int:
    return max(0, round((time.perf_counter() - started) * 1000))


def _decode(value: bytes) -> str:
    return value.decode("utf-8", errors="replace")


def _is_within(path: Path, workspace: Path) -> bool:
    normalized_path = os.path.normcase(str(path))
    normalized_workspace = os.path.normcase(str(workspace))
    try:
        return os.path.commonpath([normalized_path, normalized_workspace]) == (
            normalized_workspace
        )
    except ValueError:
        return False
