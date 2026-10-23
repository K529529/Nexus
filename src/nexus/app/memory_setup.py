"""Bounded, user-local dependency bootstrap; never runs at package installation time."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import platform
import re
import shutil
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from nexus.core.types import Emit, Json
from nexus.tools.execution import _cleanup

QMD_VERSION = "2.8.3"
NODE_VERSION = "22.23.4"
QMD_URL = "https://registry.npmjs.org/@tobilu/qmd/-/qmd-2.8.3.tgz"
QMD_INTEGRITY = (
    "zjfVwrObPB618B6x8SdhlGv/tX9OxRHsbQnr5DUtBvqPK6HGQ27lM+9/BAY5okpjrHVnW56hLyDkqoTcsrVLzA=="
)
# Pinned from https://nodejs.org/dist/v22.23.4/SHASUMS256.txt (official HTTPS).
NODE_HASHES = {
    "win-x64.zip": "cbc74b5e0982c9dbb41525fd8fb5dcd32fbee237863f7829072f4639a1ce5f20",
    "win-arm64.zip": "a8b8cd79cbbb3062435f449591d98dd63730bbbf30ce2d7cc3a6376c236cb111",
    "linux-x64.tar.xz": "1805bcd35e6815559bd2eb97db438d3fdb0eb3a5e6a977b7c1badca76337e9ce",
    "linux-arm64.tar.xz": "6287aa46b6c375d816e63ae66d024fbbfd70404825aabdfdf898a970871d2f4b",
    "darwin-x64.tar.gz": "efcc2b0db6f94bf53eddb038fe2b370277a042bd29e4f19b8f8e23598795b819",
    "darwin-arm64.tar.gz": "c9e1dbc748b33ed2d14f42445191ecaf14c56259c7a9a26d0042c49ec2b66428",
}


def managed_root() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "Nexus"
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Nexus"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "nexus"


def archive_name(system: str, machine: str) -> str:
    target = {"win32": "win", "linux": "linux", "darwin": "darwin"}.get(system)
    arch = {"amd64": "x64", "x86_64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(
        machine.lower()
    )
    suffix = "zip" if target == "win" else "tar.xz" if target == "linux" else "tar.gz"
    key = f"{target}-{arch}.{suffix}"
    if key not in NODE_HASHES:
        raise RuntimeError(f"Automatic Node setup unsupported on {system}/{machine}")
    return key


def download(url: str, destination: Path, expected: str, algorithm: str = "sha256") -> None:
    hasher = hashlib.new(algorithm)
    size = 0
    with urllib.request.urlopen(url, timeout=30) as response, destination.open("xb") as stream:
        if not response.url.startswith("https://"):
            raise RuntimeError("Dependency download redirected away from HTTPS")
        while chunk := response.read(1024 * 1024):
            size += len(chunk)
            if size > 512 * 1024 * 1024:
                raise RuntimeError("Dependency download exceeds size limit")
            hasher.update(chunk)
            stream.write(chunk)
    actual = (
        hasher.hexdigest() if algorithm == "sha256" else base64.b64encode(hasher.digest()).decode()
    )
    if actual != expected:
        raise RuntimeError("Dependency integrity check failed: " + destination.name)


def unpack(archive: Path, target: Path) -> None:
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as source:
            for item in source.infolist():
                resolved = (target / item.filename).resolve()
                if not resolved.is_relative_to(target.resolve()) or item.filename.startswith("/"):
                    raise RuntimeError("Unsafe Node archive member")
            source.extractall(target)
    else:
        with tarfile.open(archive) as source:
            source.extractall(target, filter="data")


def npm_cli(node: Path, path: str) -> Path | None:
    relative = (
        "node_modules/npm/bin/npm-cli.js"
        if os.name == "nt"
        else "../lib/node_modules/npm/bin/npm-cli.js"
    )
    candidate = node.parent / relative
    if candidate.is_file():
        return candidate
    found = shutil.which("npm", path=path)
    if found and not found.lower().endswith((".cmd", ".bat", ".ps1")):
        candidate = Path(found).resolve()
        if candidate.is_file():
            return candidate
    return None


async def command(args: tuple[str, ...], env: dict[str, str], deadline: int = 30) -> str:
    options: Json = {"start_new_session": True} if os.name != "nt" else {}
    process = await asyncio.create_subprocess_exec(
        *args, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, **options
    )
    output = bytearray()

    async def consume() -> None:
        assert process.stdout
        while chunk := await process.stdout.read(8192):
            output.extend(chunk)
            if len(output) > 16000:
                del output[:-16000]
        await process.wait()

    try:
        async with asyncio.timeout(deadline):
            await consume()
    except BaseException:
        await _cleanup(process)
        raise
    text = output.decode("utf-8", errors="replace")
    if process.returncode:
        raise RuntimeError(f"Dependency command exit {process.returncode}: {text[-2000:]}")
    return text


class DependencyBootstrap:
    def __init__(self, root: Path | None = None, *, env: dict[str, str] | None = None) -> None:
        self.root = root or managed_root()
        self.env = dict(os.environ if env is None else env)
        self.resolved: tuple[str, ...] | None = None

    async def node_ready(self, node: Path) -> bool:
        try:
            value = await command((str(node), "--version"), self.env)
            match = re.fullmatch(r"v(\d+)\.\d+\.\d+\s*", value)
            return bool(match and int(match[1]) >= 22)
        except (OSError, RuntimeError, TimeoutError):
            return False

    async def qmd_ready(self, args: tuple[str, ...]) -> bool:
        try:
            value = await command((*args, "--version"), self.env)
            return bool(re.search(r"\b" + re.escape(QMD_VERSION) + r"\b", value))
        except (OSError, RuntimeError, TimeoutError):
            return False

    async def ensure(self, emit: Emit) -> tuple[str, ...]:
        if self.resolved is not None:
            return self.resolved
        path = self.env.get("PATH", "")
        node = Path(found) if (found := shutil.which("node", path=path)) else None
        if node is not None and not await self.node_ready(node):
            node = None
        # Reuse an existing compatible QMD; never replace a global installation.
        qmd = shutil.which("qmd", path=path)
        if node is not None and qmd:
            entry = await asyncio.to_thread(Path(qmd).resolve)
            if entry.suffix.lower() in {".cmd", ".bat"}:
                entry = entry.parent / "node_modules/@tobilu/qmd/dist/cli/qmd.js"
            args = (str(node), str(entry))
            if await self.qmd_ready(args):
                self.resolved = args
                return args
        managed_node = self.root / f"node-v{NODE_VERSION}"
        node_relative = "node.exe" if os.name == "nt" else "bin/node"
        if node is None and await self.node_ready(managed_node / node_relative):
            node = managed_node / node_relative
        if node is not None:
            for folder in (self.root / f"qmd-{QMD_VERSION}", self.root / "qmd"):
                entry = folder / "node_modules/@tobilu/qmd/dist/cli/qmd.js"
                if entry.is_file() and await self.qmd_ready((str(node), str(entry))):
                    self.resolved = (str(node), str(entry))
                    return self.resolved
        if node is not None and npm_cli(node, path) is None:
            node = None  # The managed official distribution includes npm.
        self.root.mkdir(parents=True, exist_ok=True)
        # Never publish an incomplete directory. Interrupted stages remain inert for diagnosis.
        stage = Path(tempfile.mkdtemp(prefix=".setup-", dir=self.root))
        await emit("memory_setup", {"stage": "starting", "detail": "Nexus Memory Setup"})
        if node is None:
            key = archive_name(sys.platform, platform.machine())
            name = f"node-v{NODE_VERSION}-{key}"
            archive = stage / name
            await emit(
                "memory_setup", {"stage": "node", "detail": "Downloading verified Node.js runtime"}
            )
            await asyncio.to_thread(
                download,
                f"https://nodejs.org/dist/v{NODE_VERSION}/{name}",
                archive,
                NODE_HASHES[key],
            )
            await asyncio.to_thread(unpack, archive, stage)
            extracted = stage / f"node-v{NODE_VERSION}-{key.split('.')[0]}"
            if not await self.node_ready(extracted / node_relative):
                raise RuntimeError("Downloaded Node runtime cannot execute on this platform")
            if managed_node.exists():
                managed_node.rename(self.root / (".invalid-node-" + stage.name))
            extracted.rename(managed_node)
            node = managed_node / node_relative
        await emit("memory_setup", {"stage": "node_ready", "detail": "Node.js ready"})
        # Invoke npm via node directly, including Windows; no shell quoting/shims required.
        npm = npm_cli(node, path)
        if npm is None:
            raise RuntimeError("Managed Node distribution is missing npm")
        self.env["PATH"] = str(node.parent) + os.pathsep + path
        archive = stage / "qmd.tgz"
        await emit(
            "memory_setup",
            {
                "stage": "qmd",
                "detail": "Installing verified QMD 2.8.3 (npm dependencies may download)",
            },
        )
        await asyncio.to_thread(download, QMD_URL, archive, QMD_INTEGRITY, "sha512")
        package = stage / "package"
        user_config, global_config = stage / "npmrc", stage / "global-npmrc"
        user_config.write_text("", encoding="utf-8")
        global_config.write_text("", encoding="utf-8")
        await command(
            (
                str(node),
                str(npm),
                "install",
                "--prefix",
                str(package),
                "--registry=https://registry.npmjs.org",
                "--userconfig",
                str(user_config),
                "--globalconfig",
                str(global_config),
                "--no-audit",
                "--no-fund",
                "--fetch-retries=1",
                str(archive),
            ),
            self.env,
            900,
        )
        entry_relative = Path("node_modules/@tobilu/qmd/dist/cli/qmd.js")
        if not await self.qmd_ready((str(node), str(package / entry_relative))):
            raise RuntimeError("Installed QMD failed its executable version check")
        final = self.root / f"qmd-{QMD_VERSION}"
        if final.exists():
            final.rename(self.root / (".invalid-qmd-" + stage.name))
        package.rename(final)
        self.resolved = (str(node), str(final / entry_relative))
        await emit("memory_setup", {"stage": "qmd_ready", "detail": "QMD 2.8.3 installed"})
        # Small verified downloads only; retain a failed npm stage for diagnostics.
        await asyncio.to_thread(shutil.rmtree, stage)
        return self.resolved
