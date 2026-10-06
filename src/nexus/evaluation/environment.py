"""Snapshot preparation and Docker tools for the fixed local development set."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import posixpath
import tarfile
import time
import urllib.request
from dataclasses import replace
from pathlib import Path, PurePosixPath
from uuid import uuid4

from nexus.core.types import Emit, ExecutionContext, Json, Tool, ToolCancelled, ToolResult
from nexus.evaluation.cases import SUITE, EvalCase
from nexus.tools.execution import COMMAND_ARGUMENTS_DETAIL, OutputBuffer
from nexus.tools.registry import native_tools


class EnvironmentError(RuntimeError):
    pass


class CleanupError(EnvironmentError):
    pass


async def command(argv: list[str], seconds_budget: float = 120) -> tuple[int, bytes, bytes]:
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    pending = asyncio.create_task(process.communicate())
    try:
        out, err = await asyncio.wait_for(asyncio.shield(pending), seconds_budget)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await pending
        raise
    return process.returncode or 0, out, err


class Docker:
    def __init__(self, argv: list[str]) -> None:
        self.argv = argv

    @classmethod
    async def connect(cls, cache: Path) -> Docker:
        await asyncio.to_thread(cache.mkdir, parents=True, exist_ok=True)
        endpoint = os.environ.get("DOCKER_HOST")
        if not endpoint:
            code, out, _ = await command(
                ["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"], 30
            )
            if code:
                raise EnvironmentError("Cannot resolve Docker context")
            endpoint = out.decode().strip()
        config = cache / "docker-client"
        config.mkdir(exist_ok=True)
        (config / "config.json").write_text("{}", encoding="utf-8")
        # Do not inject client proxy configuration into evaluation containers.
        docker = cls(["docker", "--config", str(config), "--host", endpoint])
        await docker.checked(["info", "--format", "{{.OSType}}"], seconds_budget=30)
        return docker

    async def call(self, args: list[str], seconds_budget: float = 120) -> tuple[int, bytes, bytes]:
        return await command(self.argv + args, seconds_budget)

    async def checked(self, args: list[str], seconds_budget: float = 120) -> bytes:
        try:
            code, out, err = await self.call(args, seconds_budget)
        except TimeoutError:
            raise EnvironmentError(f"Docker {args[0]} exceeded {seconds_budget:g}s") from None
        if code:
            raise EnvironmentError(err.decode(errors="replace")[-2000:] or f"Docker exit {code}")
        return out


def _download(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "Nexus-Next-Evaluation-V0"})
    with urllib.request.urlopen(request, timeout=120) as response:
        return bytes(response.read())


def snapshot(case: EvalCase, cache: Path) -> tuple[Path, str]:
    """Verify every Git blob/mode; repair archive export-subst/export-ignore differences."""
    directory = cache / "sources" / case.id
    directory.mkdir(parents=True, exist_ok=True)
    tree_path, archive = directory / "tree.json", directory / "base.tar"
    if not tree_path.exists():
        tree_path.write_bytes(
            _download(
                f"https://api.github.com/repos/{case.repo}/git/trees/{case.base_commit}?recursive=1"
            )
        )
    tree = json.loads(tree_path.read_bytes())
    if tree.get("truncated") or any(e["type"] == "commit" for e in tree["tree"]):
        raise EnvironmentError("Incomplete or submodule source tree")
    entries = {e["path"]: e for e in tree["tree"] if e["type"] == "blob"}
    for name in entries:
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or ".git" in path.parts:
            raise EnvironmentError("Unsafe snapshot path")
    if not archive.exists():
        raw = _download(f"https://codeload.github.com/{case.repo}/tar.gz/{case.base_commit}")
        blobs: dict[str, bytes] = {}
        with tarfile.open(fileobj=io.BytesIO(raw)) as source:
            for member in source:
                name = member.name.partition("/")[2]
                if name not in entries:
                    continue
                file = source.extractfile(member) if member.isfile() else None
                blobs[name] = (
                    member.linkname.encode() if member.issym() else (file.read() if file else b"")
                )
        temporary = archive.with_suffix(".tmp")
        with tarfile.open(temporary, "w") as output:
            for name, entry in entries.items():
                data = blobs.get(name, b"")
                if blob_id(data) != entry["sha"]:
                    data = _download(
                        f"https://raw.githubusercontent.com/{case.repo}/{case.base_commit}/{name}"
                    )
                if blob_id(data) != entry["sha"]:
                    raise EnvironmentError(f"Snapshot blob mismatch: {name}")
                member = tarfile.TarInfo(name)
                member.mode = int(entry["mode"], 8) & 0o777
                if entry["mode"] == "120000":
                    member.type, member.linkname = tarfile.SYMTYPE, data.decode()
                    output.addfile(member)
                else:
                    member.size = len(data)
                    output.addfile(member, io.BytesIO(data))
        temporary.replace(archive)
    seen = set()
    with tarfile.open(archive) as source:
        for member in source:
            entry = entries.get(member.name)
            file = source.extractfile(member) if member.isfile() else None
            data = member.linkname.encode() if member.issym() else file.read() if file else b""
            mode = "120000" if member.issym() else f"100{member.mode & 0o777:03o}"
            if entry is None or blob_id(data) != entry["sha"] or mode != entry["mode"]:
                raise EnvironmentError(f"Cached snapshot mismatch: {member.name}")
            seen.add(member.name)
    if seen != set(entries):
        raise EnvironmentError("Cached snapshot missing files")
    return archive, tree_id(tree["tree"])


def tree_id(entries: list[Json]) -> str:
    # GitHub trees-by-commit responses may echo the requested commit in top-level sha.
    # Reconstruct the actual root tree from its direct children.
    children = [e for e in entries if "/" not in e["path"]]
    children.sort(key=lambda e: (e["path"] + ("/" if e["type"] == "tree" else "")).encode())
    body = b"".join(
        e["mode"].lstrip("0").encode() + b" " + e["path"].encode() + b"\0" + bytes.fromhex(e["sha"])
        for e in children
    )
    return hashlib.sha1(b"tree " + str(len(body)).encode() + b"\0" + body).hexdigest()


def blob_id(data: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()


# Run only during preparation/collection. Compatible with the oldest case's Python 3.6.
# Index the exact tar modes rather than NTFS bind-mount permission emulation.
BASE_INDEX = r"""
import json, os, subprocess, tarfile
os.environ.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null")
def git(*args, **kwargs):
    return subprocess.check_output(["git", *args], **kwargs)
git("init", "-q")
git("config", "core.filemode", "false")
git("config", "core.autocrlf", "false")
git("config", "user.name", "Nexus Evaluation")
git("config", "user.email", "eval@localhost")
process = subprocess.Popen(["git", "fast-import", "--quiet"], stdin=subprocess.PIPE)
records = []
with tarfile.open("/tmp/nexus-base.tar") as source:
    for number, m in enumerate(source, 1):
        if not (m.isfile() or m.issym()):
            continue
        data = m.linkname.encode() if m.issym() else source.extractfile(m).read()
        process.stdin.write(("blob\nmark :%d\ndata %d\n" % (number, len(data))).encode())
        process.stdin.write(data + b"\n")
        mode = "120000" if m.issym() else "100%03o" % (m.mode & 0o777)
        records.append(("M %s :%d %s\n" % (
            mode, number, json.dumps(m.name, ensure_ascii=False))).encode())
process.stdin.write(b"commit refs/heads/main\n"
    b"committer Nexus Evaluation <eval@localhost> 946684800 +0000\n"
    b"data 27\nEvaluation source snapshot\n\n")
for record in records:
    process.stdin.write(record)
process.stdin.write(b"\ndone\n")
process.stdin.close()
if process.wait():
    raise RuntimeError("Snapshot import failed")
git("symbolic-ref", "HEAD", "refs/heads/main")
git("reset", "--mixed", "-q", "HEAD")
print(git("rev-parse", "HEAD").decode().strip())
print(git("write-tree").decode().strip())
"""


class Environment:
    def __init__(self, docker: Docker, case: EvalCase, cache: Path) -> None:
        self.docker, self.case, self.cache = docker, case, cache
        self.name = "nexus-eval-" + uuid4().hex
        self.workspace = (cache / "workspaces" / self.name).resolve()
        self.archive: Path | None = None
        self.tree = self.base_commit = ""
        self.active = False
        self.broken = False

    async def prepare(self, mounts: list[tuple[Path, str, bool]] | None = None) -> None:
        code, _, _ = await self.docker.call(["image", "inspect", self.case.image], 30)
        if code:
            # Frozen local IDs cannot be pulled. Attempt the reviewed recipe, then require identity.
            build = self.cache / "build" / self.case.id
            build.mkdir(parents=True, exist_ok=True)
            recipe = SUITE / "environments" / self.case.key / "Dockerfile"
            (build / "Dockerfile").write_bytes(recipe.read_bytes())
            if self.case.key == "matplotlib":
                (build / "source.tar.gz").write_bytes(
                    await asyncio.to_thread(
                        _download,
                        f"https://codeload.github.com/{self.case.repo}/tar.gz/{self.case.base_commit}",
                    )
                )
            await self.docker.checked(["build", "-q", str(build)], seconds_budget=3600)
            await self.docker.checked(["image", "inspect", self.case.image], seconds_budget=30)
        self.archive, self.tree = await asyncio.to_thread(snapshot, self.case, self.cache)
        self.workspace.mkdir(parents=True)
        args = [
            "run",
            "-d",
            "--init",
            "--name",
            self.name,
            "--network",
            "none",
            "--memory",
            "1g",
            "--cpus",
            "1",
            "--pids-limit",
            "256",
            "--mount",
            f"type=bind,source={self.workspace},target=/workspace",
        ]
        for path, target, readonly in mounts or []:
            args += [
                "--mount",
                f"type=bind,source={path.resolve()},target={target}"
                + (",readonly" if readonly else ""),
            ]
        args += ["--entrypoint", "/bin/sh", self.case.image, "-c", "sleep infinity"]
        self.active = True  # Name is known even if Docker startup is interrupted.
        await self.docker.checked(args)
        await self.docker.checked(["cp", str(self.archive), self.name + ":/tmp/nexus-base.tar"])
        await self.checked(["tar", "-xf", "/tmp/nexus-base.tar", "-C", "/workspace"])
        lines = (await self.checked(["python", "-c", BASE_INDEX])).decode().splitlines()
        self.base_commit = lines[-2]
        if lines[-1] != self.tree:
            raise EnvironmentError("Synthetic base tree does not match upstream snapshot")
        await self.checked(["rm", "/tmp/nexus-base.tar"])
        if self.case.key == "matplotlib":
            await self.checked(["cp", "-r", "/opt/nexus-generated/.", "/workspace/"])
        # A venv/PYTHONPATH only chooses imports; the Agent can still read every other
        # environment, vendored package and build cache. Remove those copies first.
        isolation = (SUITE / "environments" / "isolate_target.py").read_text(encoding="utf-8")
        await self.checked(["python", "-c", isolation, "clean", self.case.key], 180)
        if self.case.key == "pytest":
            await self.checked(
                [
                    "env",
                    "SETUPTOOLS_SCM_PRETEND_VERSION=9.1.0.dev0",
                    "python",
                    "-m",
                    "pip",
                    "install",
                    "--no-build-isolation",
                    "--no-deps",
                    "-e",
                    ".",
                ]
            )
        await self.checked(["python", "-c", isolation, "scan", self.case.key], 180)

    async def checked(self, argv: list[str], seconds_budget: float = 120) -> bytes:
        return await self.docker.checked(
            ["exec", "-w", "/workspace", self.name, *argv], seconds_budget
        )

    def registry(self) -> dict[str, Tool]:
        registry = native_tools()
        spec = registry["exec_command"].spec
        spec = replace(
            spec,
            description=(
                spec.description + " Internet access is disabled in this command environment."
            ),
        )
        registry["exec_command"] = Tool(spec, self.execute)
        return registry

    async def _kill_group(self, pidfile: str) -> bool:
        script = (
            'if ! test -s "$1"; then exit 3; fi; p=$(cat "$1"); '
            'case "$p" in *[!0-9]*|"") exit 3;; esac; '
            'kill -TERM -- -"$p" 2>/dev/null || true; sleep 0.2; '
            'kill -KILL -- -"$p" 2>/dev/null || true; sleep 0.2; '
            'ps -eo pgid=,stat= | awk -v p="$p" '
            "'$1==p && $2 !~ /^Z/ {alive=1} END {exit alive ? 1 : 0}'"
        )
        try:
            code, _, _ = await self.docker.call(
                ["exec", self.name, "/bin/bash", "-c", script, "cleanup", pidfile], 10
            )
            if code == 0:
                return True
        except (OSError, TimeoutError):
            pass
        self.broken = True
        await self.docker.call(["kill", self.name], 10)
        return False

    async def execute(self, arguments: Json, context: ExecutionContext, emit: Emit) -> ToolResult:
        started = time.monotonic()
        text, cwd, timeout = (
            arguments.get("command"),
            arguments.get("workdir", "/workspace"),
            arguments.get("timeout_ms", 120000),
        )
        if (
            arguments.keys() - {"command", "workdir", "timeout_ms"}
            or not isinstance(text, str)
            or not text.strip()
            or not isinstance(cwd, str)
            or not cwd
            or "\0" in cwd
            or type(timeout) is not int
            or not 1 <= timeout <= 600000
        ):
            return ToolResult(
                context.call_id,
                False,
                {"detail": COMMAND_ARGUMENTS_DETAIL},
                "invalid_arguments",
            )
        cwd = posixpath.normpath(posixpath.join("/workspace", cwd))
        data: Json = dict(
            command=text,
            cwd=cwd,
            shell="/bin/sh",
            exit_code=None,
            timed_out=False,
            cancelled=False,
            cleanup_incomplete=False,
        )
        pidfile = "/tmp/nexus-command-" + uuid4().hex
        argv = self.docker.argv + [
            "exec",
            "-w",
            cwd,
            self.name,
            "setsid",
            "--wait",
            "/bin/sh",
            "-c",
            'echo $$ > "$1"; exec /bin/sh -c "$2"',
            "nexus",
            pidfile,
            text,
        ]
        output, streamed = OutputBuffer(context.output_limit_bytes), 0

        async def drain(pipe: asyncio.StreamReader, name: str) -> None:
            nonlocal streamed
            while block := await pipe.read(8192):
                output.add(name, block)
                visible = block[: max(0, context.output_limit_bytes - streamed)]
                streamed += len(visible)
                if visible:
                    await emit(
                        "tool_output_delta",
                        {
                            "call_id": context.call_id,
                            "stream": name,
                            "text": visible.decode(errors="replace"),
                        },
                    )

        spawn = asyncio.create_task(
            asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        )
        process = None
        readers: list[asyncio.Task[None]] = []
        error = None
        try:
            process = await asyncio.shield(spawn)
            assert process.stdout is not None and process.stderr is not None
            readers = [
                asyncio.create_task(drain(process.stdout, "stdout")),
                asyncio.create_task(drain(process.stderr, "stderr")),
            ]
            async with asyncio.timeout(timeout / 1000):
                await process.wait()
                await asyncio.gather(*readers)
            if process.returncode:
                error = "command_exit_nonzero"
        except (TimeoutError, asyncio.CancelledError) as exc:
            cancelled = isinstance(exc, asyncio.CancelledError)
            data["cancelled"], data["timed_out"] = cancelled, not cancelled
            error = "cancelled" if cancelled else "command_timeout"
            process = process or await spawn
            if not await self._kill_group(pidfile):
                data["cleanup_incomplete"], error = True, "cleanup_incomplete"
        except OSError:
            error = "command_start_failed"
        finally:
            if process is not None:
                if process.returncode is None:
                    process.kill()
                await process.wait()
                data["exit_code"] = process.returncode
            if readers:
                await asyncio.gather(*readers, return_exceptions=True)
        data.update(output.output())
        duration = int((time.monotonic() - started) * 1000)
        data["duration_ms"] = duration
        result = ToolResult(
            context.call_id, error is None, data, error, duration, data["truncated"]
        )
        if data["cancelled"]:
            raise ToolCancelled(result)
        return result

    async def collect_patch(self, destination: Path) -> None:
        # Quiesce candidate processes; never trust their Git index/config/HEAD.
        await self.docker.checked(["stop", "-t", "1", self.name], 15)
        collector = "nexus-collect-" + uuid4().hex
        assert self.archive is not None
        try:
            await self.docker.checked(
                [
                    "run",
                    "-d",
                    "--name",
                    collector,
                    "--network",
                    "none",
                    "--memory",
                    "1g",
                    "--mount",
                    f"type=bind,source={self.workspace},target=/candidate,readonly",
                    "--entrypoint",
                    "/bin/sh",
                    self.case.image,
                    "-c",
                    "sleep infinity",
                ]
            )
            await self.docker.checked(["cp", str(self.archive), collector + ":/tmp/nexus-base.tar"])
            await self.docker.checked(["exec", collector, "mkdir", "-p", "/tmp/base"])
            await self.docker.checked(
                ["exec", "-w", "/tmp/base", collector, "python", "-c", BASE_INDEX]
            )
            git = [
                "exec",
                "-w",
                "/candidate",
                collector,
                "git",
                "--git-dir=/tmp/base/.git",
                "--work-tree=/candidate",
                "-c",
                "core.hooksPath=/dev/null",
            ]
            await self.docker.checked(git + ["add", "-A", "--", "."])
            patch = await self.docker.checked(
                git + ["diff", "--cached", "--binary", "--no-ext-diff", "HEAD", "--"]
            )
            await asyncio.to_thread(destination.write_bytes, patch)
        finally:
            try:
                await self.docker.checked(["rm", "-f", collector], 30)
            except (EnvironmentError, OSError, TimeoutError) as exc:
                raise CleanupError(f"Collector cleanup failed: {exc}") from exc

    async def close(self) -> None:
        if self.active:
            await self.docker.checked(["rm", "-f", self.name], 30)
            self.active = False
        if self.workspace.exists():
            expected = (self.cache / "workspaces").resolve()
            if self.workspace.parent != expected or not self.workspace.name.startswith(
                "nexus-eval-"
            ):
                raise EnvironmentError("Refusing cleanup outside owned evaluation workspace")
            # Linux removes Linux-created symlinks correctly on Windows bind mounts.
            await self.docker.checked(
                [
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "--mount",
                    f"type=bind,source={self.workspace},target=/workspace",
                    "--entrypoint",
                    "/bin/sh",
                    self.case.image,
                    "-c",
                    "find /workspace -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +",
                ],
                120,
            )
            self.workspace.rmdir()
