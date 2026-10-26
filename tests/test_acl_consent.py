from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import subprocess
import sys
import tomllib
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from nexus.app import acl_consent, cli
from nexus.app.bootstrap import Conversation
from nexus.app.config import Config, ConfigError, ModelConfig, load_config, validate_acl_authority
from nexus.app.session import _lock
from nexus.app.tui import confirm_workspace_acl
from nexus.core.security import Mode, SecurityPolicy
from nexus.core.types import Limits
from nexus.tools.sandbox import BridgeError
from nexus.tools.windows_native import WindowsNativeBackend, validate_roots


@pytest.fixture
def consent_fixture(tmp_path: Path) -> tuple[Path, Path, SecurityPolicy]:
    work = tmp_path / "work"
    work.mkdir()
    path = tmp_path / "user" / "config.toml"
    path.parent.mkdir()
    path.write_text('# retained comment\n[model]\nname="fake"\n[memory]\nenabled=false\n')
    return work, path, SecurityPolicy("workspace-write")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows consent")
@pytest.mark.parametrize("answer", [False, True])
async def test_consent_precise_save_and_reuse(
    consent_fixture: tuple[Path, Path, SecurityPolicy], answer: bool
) -> None:
    work, path, policy = consent_fixture
    original = path.read_bytes()
    asked = []

    async def confirm(root: Path) -> bool:
        asked.append(root)
        return answer

    result = await acl_consent.ensure_workspace_consent(work, policy, confirm=confirm, path=path)
    assert asked == [work.resolve()]
    if not answer:
        assert result is None and path.read_bytes() == original
        assert not path.with_suffix(".toml.lock").exists()
        return
    assert result is not None
    document = tomllib.loads(path.read_text())
    assert document["security"]["windows_acl_roots"] == [str(work.resolve())]
    assert document["model"] == {"name": "fake"} and document["memory"] == {"enabled": False}
    assert "# retained comment" in path.read_text()
    assert load_config(path, require_key=False).security.windows_acl_roots == (str(work.resolve()),)
    assert (
        await acl_consent.ensure_workspace_consent(work, result, confirm=confirm, path=path)
        == result
    )
    assert len(asked) == 1


@pytest.mark.skipif(sys.platform != "win32", reason="Windows consent")
async def test_noninteractive_no_command_or_file_change(
    consent_fixture: tuple[Path, Path, SecurityPolicy],
) -> None:
    work, path, policy = consent_fixture
    before = path.read_bytes()
    with pytest.raises(ConfigError, match="interactive Nexus"):
        await acl_consent.ensure_workspace_consent(work, policy, path=path)
    assert path.read_bytes() == before


@pytest.mark.parametrize("mode", ["local", "read-only"])
async def test_ro_local_skip_consent_and_runtime(
    monkeypatch: pytest.MonkeyPatch, mode: Mode, tmp_path: Path
) -> None:
    async def forbidden(*args: Any, **kw: Any) -> bool:
        pytest.fail("Must not ask for workspace ACL consent")

    monkeypatch.setattr(cli, "confirm_workspace_acl", forbidden)
    monkeypatch.setattr(
        acl_consent, "validate_roots", lambda *a, **k: pytest.fail("Native accessed")
    )
    cfg = Config(ModelConfig("fake", 32768), Limits(), security=SecurityPolicy(mode))
    assert await cli.authorize_workspace(tmp_path, cfg, interactive=True) is cfg


def test_atomic_save_failure_preserves_original(
    consent_fixture: tuple[Path, Path, SecurityPolicy], monkeypatch: pytest.MonkeyPatch
) -> None:
    work, path, policy = consent_fixture
    before = path.read_bytes()

    def fail(*args: Any) -> None:
        raise OSError("replace fixture failed")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError, match="replace fixture"):
        acl_consent.save_workspace_consent(work, policy, path)
    assert path.read_bytes() == before
    assert not list(path.parent.glob(".memory-*"))


def test_busy_writer_preserves_config(consent_fixture: tuple[Path, Path, SecurityPolicy]) -> None:
    work, path, policy = consent_fixture
    before = path.read_bytes()
    with path.with_suffix(".toml.lock").open("a+b") as lock:
        _lock(lock)
        with pytest.raises(ConfigError, match="Cannot save"):
            acl_consent.save_workspace_consent(work, policy, path)
    assert path.read_bytes() == before


def test_save_merges_concurrent_approved_roots(
    consent_fixture: tuple[Path, Path, SecurityPolicy],
) -> None:
    work, path, policy = consent_fixture
    other = work.parent / "other"
    other.mkdir()
    acl_consent.save_workspace_consent(other, policy, path)
    result = acl_consent.save_workspace_consent(work, policy, path)
    assert result.windows_acl_roots == (str(other), str(work))


@pytest.mark.parametrize("content", ["[security", '[security]\nwindows_acl_roots="bad"'])
def test_invalid_config_not_repaired(
    consent_fixture: tuple[Path, Path, SecurityPolicy], content: str
) -> None:
    work, path, policy = consent_fixture
    path.write_text(content)
    with pytest.raises(ConfigError):
        acl_consent.save_workspace_consent(work, policy, path)
    assert path.read_text() == content


@pytest.mark.parametrize(
    "key,expected", [("a", True), ("d", False), ("\r", False), ("\x1b", False), ("\x03", False)]
)
async def test_actual_tui_bindings(key: str, expected: bool, tmp_path: Path) -> None:
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        task = asyncio.create_task(confirm_workspace_acl(tmp_path))
        await asyncio.sleep(0.05)
        pipe.send_text(key)
        assert await asyncio.wait_for(task, 3) is expected


@pytest.mark.skipif(sys.platform != "win32", reason="Windows junctions")
def test_saved_consent_does_not_follow_new_junction(
    consent_fixture: tuple[Path, Path, SecurityPolicy],
) -> None:
    work, _, policy = consent_fixture
    old = work.parent / "old-approved"
    subprocess.run(
        [
            "node",
            "-e",
            "require('fs').symlinkSync(process.argv[1],process.argv[2],'junction')",
            str(work),
            str(old),
        ],
        check=True,
    )
    policy = replace(policy, windows_acl_roots=(str(old),))
    try:
        with pytest.raises(BridgeError, match="Standing ACL"):
            validate_roots(work, policy)
        with pytest.raises(BridgeError, match="Linked path"):
            validate_roots(old, policy)
    finally:
        old.rmdir()  # Remove this junction only, never the target tree.


def test_removed_other_approval_does_not_break_active_workspace(
    consent_fixture: tuple[Path, Path, SecurityPolicy],
) -> None:
    work, _, policy = consent_fixture
    validate_roots(
        work, replace(policy, windows_acl_roots=(str(work), str(work.parent / "removed")))
    )


@pytest.mark.skipif(sys.platform != "win32", reason="Windows consent")
def test_workspace_config_cannot_establish_authority(
    consent_fixture: tuple[Path, Path, SecurityPolicy], monkeypatch: pytest.MonkeyPatch
) -> None:
    work, _, policy = consent_fixture
    path = work / "config.toml"
    path.write_text(
        '[model]\nname="fake"\n[security]\nmode="read-only"\nwindows_acl_roots='
        + json.dumps([str(work)])
    )
    monkeypatch.chdir(work)
    with pytest.raises(ConfigError, match="outside workspace"):
        load_config(path, require_key=False)
    with pytest.raises(BridgeError, match="outside workspace"):
        acl_consent.save_workspace_consent(work, policy, path)
    # Local configuration validation does not import/require any runtime.
    validate_acl_authority(replace(policy, mode="local", windows_acl_roots=(str(work),)), path)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows consent")
async def test_cli_start_denied_before_conversation(
    consent_fixture: tuple[Path, Path, SecurityPolicy], monkeypatch: pytest.MonkeyPatch
) -> None:
    work, path, policy = consent_fixture
    monkeypatch.chdir(work)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    cfg = Config(ModelConfig("fake", 32768), Limits(), security=policy)
    monkeypatch.setattr(cli, "load_config", lambda **kw: cfg)
    monkeypatch.setattr(cli, "config_path", lambda: path)
    monkeypatch.setattr(acl_consent, "config_path", lambda: path)
    monkeypatch.setattr(
        cli, "Conversation", lambda *a, **kw: pytest.fail("Conversation before consent")
    )

    async def deny(root: Path) -> bool:
        assert root == work
        return False

    monkeypatch.setattr(cli, "confirm_workspace_acl", deny)
    output = io.StringIO()
    monkeypatch.setattr(cli, "Console", lambda **kw: Console(file=output))
    with create_app_session(output=DummyOutput()):
        assert await cli.application(argparse.Namespace(command=None)) == 0
    assert "No command executed" in output.getvalue()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows consent")
@pytest.mark.parametrize("answer", [False, True])
async def test_permissions_transition_preserves_session_until_consent(
    consent_fixture: tuple[Path, Path, SecurityPolicy],
    monkeypatch: pytest.MonkeyPatch,
    answer: bool,
) -> None:
    from types import SimpleNamespace

    from prompt_toolkit.input import DummyInput

    work, path, _ = consent_fixture
    monkeypatch.chdir(work)
    monkeypatch.setattr(Path, "home", lambda: path.parent)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    cfg = Config(ModelConfig("fake", 32768), Limits(), memory_enabled=False)
    monkeypatch.setattr(cli, "load_config", lambda **kw: cfg)
    monkeypatch.setattr(cli, "config_path", lambda: path)
    monkeypatch.setattr(acl_consent, "config_path", lambda: path)
    conversations = []
    original = Conversation

    def conversation(*args: Any, **kwargs: Any) -> Any:
        value = original(*args, **kwargs)
        if value.config.security.mode == "workspace-write":
            assert isinstance(value.gateway.backend, WindowsNativeBackend)
            assert value.gateway.backend.status == "not_initialized"
        conversations.append(value)
        return value

    async def select(*args: Any) -> str:
        return "workspace-write"

    async def confirm(root: Path) -> bool:
        assert root == work and not conversations[0].closed
        assert conversations[0].config.security.mode == "local"
        return answer

    inputs = iter(["/permissions", "/exit"])

    class Prompt:
        def __init__(self, **kwargs: Any) -> None:
            self.app = SimpleNamespace(input=DummyInput())

        async def prompt_async(self, *args: Any, **kwargs: Any) -> str:
            return next(inputs)

    output = io.StringIO()
    monkeypatch.setattr(cli, "Conversation", conversation)
    monkeypatch.setattr(cli, "PromptSession", Prompt)
    monkeypatch.setattr(cli, "Console", lambda **kw: Console(file=output, width=180))
    monkeypatch.setattr(cli, "select_permissions", select)
    monkeypatch.setattr(cli, "confirm_workspace_acl", confirm)
    with create_app_session(output=DummyOutput()):
        assert await cli.application(argparse.Namespace(command=None)) == 0
    assert len(conversations) == (2 if answer else 1)
    assert all(c.closed and c.model is None for c in conversations)
    if answer:
        assert conversations[1].config.security.windows_acl_roots == (str(work),)
        backend = conversations[1].gateway.backend
        assert isinstance(backend, WindowsNativeBackend) and backend.status == "closed"
        assert tomllib.loads(path.read_text())["security"]["windows_acl_roots"] == [str(work)]
    else:
        assert "Mode unchanged" in output.getvalue()
        assert "security" not in tomllib.loads(path.read_text())
