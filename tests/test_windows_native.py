"""Native host-policy/transport regressions; real isolation uses acceptance script."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from nexus.app.config import ConfigError, load_config
from nexus.core.security import SecurityPolicy
from nexus.core.types import ExecutionContext, ToolResult
from nexus.tools.sandbox import BridgeError
from nexus.tools.windows_native import (
    UnavailableBackend,
    WindowsNativeBackend,
    platform_backend,
    validate_roots,
)
from tests.conftest import Recorder


@pytest.mark.parametrize("key", ["allowed_domains", "denied_domains"])
@pytest.mark.parametrize("value", ["[]", '["example.com"]'])
def test_windows_legacy_network_config_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str, value: str
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    path = tmp_path / "config.toml"
    path.write_text(
        f'[model]\nname="fake"\n[security]\nmode="read-only"\n{key}={value}\n', encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="Network: unrestricted"):
        load_config(path, require_key=False)


def test_each_standing_acl_root_needs_exact_consent(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    extra = tmp_path / "extra"
    extra.mkdir()
    with pytest.raises(BridgeError, match="Standing ACL consent"):
        validate_roots(
            work,
            SecurityPolicy(
                "workspace-write",
                writable_roots=(str(extra),),
                windows_acl_roots=(str(tmp_path), str(work)),
            ),
        )
    assert validate_roots(
        work,
        SecurityPolicy(
            "workspace-write",
            writable_roots=(str(extra),),
            windows_acl_roots=(str(work), str(extra)),
        ),
    ) == (work, extra)


@pytest.mark.parametrize("kind", ["system", "program", "home", "temp", "ancestor", "secret"])
def test_protected_roots_rejected_before_acl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    system = tmp_path / "Windows"
    system.mkdir()
    program = tmp_path / "ProgramFiles"
    program.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    temp = home / "temp"
    temp.mkdir()
    secret = home / ".nexus"
    secret.mkdir()
    monkeypatch.setenv("SystemRoot", str(system))
    monkeypatch.setenv("ProgramFiles", str(program))
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr("nexus.tools.windows_native.tempfile.gettempdir", lambda: str(temp))
    root = dict(
        system=system, program=program, home=home, temp=temp, ancestor=tmp_path, secret=secret
    )[kind]
    with pytest.raises(BridgeError, match="Protected"):
        validate_roots(root, SecurityPolicy("workspace-write", windows_acl_roots=(str(root),)))


@pytest.mark.skipif(os.name != "nt", reason="System PowerShell installation")
def test_system_powershell_never_a_read_or_acl_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    install = tmp_path / "install"
    install.mkdir()
    koffi = install / "koffi"
    koffi.mkdir()
    (koffi / "package.json").write_text('{"name":"koffi","version":"3.3.3"}')
    node = install / "node.exe"
    node.touch()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    shell = str(Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe")
    backend = WindowsNativeBackend(
        SecurityPolicy("read-only", node_path=str(node), windows_koffi=str(koffi))
    )
    argv, params = backend.installation(work, shell)
    assert params["read_roots"] == [] and params["writable_roots"] == []
    assert params["workspace"] == str(work)
    assert backend.shell == str(Path(shell).resolve())
    assert "srt" not in argv[1] and shell not in argv[-1]


async def test_unavailable_platform_and_dependency_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    backend = platform_backend(SecurityPolicy("workspace-write"))
    assert isinstance(backend, UnavailableBackend)
    r = await backend.execute(
        "exec_command",
        {"command": "must not execute"},
        ExecutionContext(tmp_path, "c", ""),
        Recorder(),
    )
    assert r.error_code == "backend_unavailable"
    monkeypatch.setattr(sys, "platform", "win32")
    backend = platform_backend(SecurityPolicy("read-only", node_path=str(tmp_path / "absent.exe")))
    r = await backend.execute(
        "exec_command",
        {"command": "must not execute"},
        ExecutionContext(tmp_path, "c", "powershell.exe"),
        Recorder(),
    )
    assert r.error_code == "backend_unavailable" and backend.status == "failed"


@pytest.mark.parametrize(
    "field,value",
    [
        ("allowed_domains", ("example.com",)),
        ("denied_domains", ("example.com",)),
        ("srt_package", "legacy"),
    ],
)
def test_programmatic_legacy_policies_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, value: Any
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    b = WindowsNativeBackend(SecurityPolicy("read-only", **{field: value}))
    with pytest.raises(BridgeError, match="legacy|Legacy"):
        b.installation(tmp_path, "powershell.exe")


@pytest.mark.skipif(not shutil.which("node"), reason="Node required")
def test_child_environment_is_allowlisted_not_inherited() -> None:
    bridge = Path(__file__).resolve().parents[1] / "src/nexus/tools/windows_native_bridge.mjs"
    code = f"import {{childEnvironment}} from {json.dumps(bridge.as_uri())};"
    code += (
        "console.log(JSON.stringify(childEnvironment({"
        'Path:"bin", SystemRoot:"Windows", OPENAI_API_KEY:"secret",'
        'USERPROFILE:"custom-model-key", NODE_OPTIONS:"preload", PYTHONPATH:"inject",'
        'PSModulePath:"wrong-version", HTTP_PROXY:"credential", OTHER_SECRET:"secret"},'
        '["userprofile"], "private", "trusted")));'
    )
    env = json.loads(
        subprocess.check_output(
            [str(shutil.which("node")), "--input-type=module", "-e", code], encoding="utf-8"
        )
    )
    assert env == {
        "Path": "bin",
        "SystemRoot": "Windows",
        "TEMP": "private",
        "TMP": "private",
        "NEXUS_NATIVE_KOFFI": "trusted",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


async def test_one_shot_cleanup_failure_poison_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = WindowsNativeBackend(SecurityPolicy("read-only"))

    async def execute(*a: Any, **kw: Any) -> ToolResult:
        return ToolResult("c", True, {})

    async def close(*a: Any) -> None:
        raise OSError("cleanup unconfirmed")

    monkeypatch.setattr(WindowsNativeBackend, "execute", execute)
    monkeypatch.setattr(WindowsNativeBackend, "close", close)
    with pytest.raises(OSError):
        await original.execute_patch_once({}, ExecutionContext(tmp_path, "c", ""), Recorder())
    assert original.status == "failed"
