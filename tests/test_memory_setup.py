# ruff: noqa: ASYNC240 -- local fixture files, no real asynchronous disk workload
from __future__ import annotations

import io
import os
import shutil
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from nexus.app import memory_setup as setup
from tests.conftest import Recorder


@pytest.mark.parametrize(
    "system,machine,key",
    [
        ("win32", "AMD64", "win-x64.zip"),
        ("win32", "ARM64", "win-arm64.zip"),
        ("linux", "x86_64", "linux-x64.tar.xz"),
        ("linux", "aarch64", "linux-arm64.tar.xz"),
        ("darwin", "arm64", "darwin-arm64.tar.gz"),
        ("darwin", "x86_64", "darwin-x64.tar.gz"),
    ],
)
def test_platform_archives(system: str, machine: str, key: str) -> None:
    assert setup.archive_name(system, machine) == key


def test_unsupported_platform_and_integrity(tmp_path: Path, monkeypatch: Any) -> None:
    with pytest.raises(RuntimeError, match="unsupported"):
        setup.archive_name("freebsd", "i386")

    class Response(io.BytesIO):
        url = "https://nodejs.org/dist/test"

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: Response(b"corrupted"))
    with pytest.raises(RuntimeError, match="integrity"):
        setup.download("https://nodejs.org/dist/test", tmp_path / "download", "0" * 64)


class FixtureBootstrap(setup.DependencyBootstrap):
    async def node_ready(self, node: Path) -> bool:
        return node.is_file()

    async def qmd_ready(self, args: tuple[str, ...]) -> bool:
        path = Path(args[-1])
        return path.is_file() and path.read_text() == "ready"


def fixture_installer(tmp_path: Path, monkeypatch: Any) -> list[str]:
    operations: list[str] = []
    monkeypatch.setattr(shutil, "which", lambda *a, **k: None)

    def download(url: str, path: Path, *args: Any) -> None:
        operations.append("download-node" if "nodejs.org" in url else "download-qmd")
        path.write_text("archive")

    def unpack(archive: Path, target: Path) -> None:
        root = target / archive.name.split(".zip")[0].split(".tar")[0]
        node = root / ("node.exe" if os.name == "nt" else "bin/node")
        npm = root / (
            "node_modules/npm/bin/npm-cli.js"
            if os.name == "nt"
            else "lib/node_modules/npm/bin/npm-cli.js"
        )
        for path in (node, npm):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture")

    async def command(args: tuple[str, ...], *unused: Any) -> str:
        operations.append("npm-install")
        target = Path(args[args.index("--prefix") + 1]) / "node_modules/@tobilu/qmd/dist/cli/qmd.js"
        target.parent.mkdir(parents=True)
        target.write_text("ready")
        return "installed"

    monkeypatch.setattr(setup, "download", download)
    monkeypatch.setattr(setup, "unpack", unpack)
    monkeypatch.setattr(setup, "command", command)
    return operations


async def test_clean_install_then_reuse_without_download(tmp_path: Path, monkeypatch: Any) -> None:
    operations = fixture_installer(tmp_path, monkeypatch)
    installer = FixtureBootstrap(tmp_path, env={"PATH": ""})
    first = await installer.ensure(Recorder())
    assert operations == ["download-node", "download-qmd", "npm-install"]
    assert await installer.ensure(Recorder()) == first
    assert await FixtureBootstrap(tmp_path, env={"PATH": ""}).ensure(Recorder()) == first
    assert operations == ["download-node", "download-qmd", "npm-install"]
    assert not list(tmp_path.glob(".setup-*"))


async def test_failed_install_not_published_and_next_start_recovers(
    tmp_path: Path, monkeypatch: Any
) -> None:
    operations = fixture_installer(tmp_path, monkeypatch)
    command = setup.command

    async def fail(*args: Any, **kwargs: Any) -> str:
        raise RuntimeError("network unavailable")

    monkeypatch.setattr(setup, "command", fail)
    with pytest.raises(RuntimeError, match="network"):
        await FixtureBootstrap(tmp_path, env={"PATH": ""}).ensure(Recorder())
    assert not (tmp_path / "qmd-2.8.3").exists()
    assert list(tmp_path.glob(".setup-*"))
    monkeypatch.setattr(setup, "command", command)
    await FixtureBootstrap(tmp_path, env={"PATH": ""}).ensure(Recorder())
    assert operations.count("download-node") == 1


async def test_existing_node_only_does_not_download_node(tmp_path: Path, monkeypatch: Any) -> None:
    operations = fixture_installer(tmp_path, monkeypatch)
    node = tmp_path / ("node.exe" if os.name == "nt" else "bin/node")
    npm = tmp_path / (
        "node_modules/npm/bin/npm-cli.js"
        if os.name == "nt"
        else "lib/node_modules/npm/bin/npm-cli.js"
    )
    for path in (node, npm):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture")
    monkeypatch.setattr(shutil, "which", lambda name, **k: str(node) if name == "node" else None)
    await FixtureBootstrap(tmp_path / "managed", env={"PATH": ""}).ensure(Recorder())
    assert operations == ["download-qmd", "npm-install"]
