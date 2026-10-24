from __future__ import annotations

import argparse
import io
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from nexus.app import bootstrap, cli
from nexus.app import config as configuration
from nexus.app.config import ConfigError, memory_settings, set_memory_enabled
from nexus.app.memory import MemoryStore
from nexus.app.session import SessionError
from tests.conftest import ScriptedModel, reply
from tests.test_conversation import config, ignore


@pytest.mark.parametrize(
    "memory",
    [
        "",
        '[memory]\nenabled = true # preference\nqmd_command = ["custom"]\n',
        'memory.enabled = true\nmemory.qmd_command = ["custom"]\n',
        'memory = { enabled = true, qmd_command = ["custom"] }\n',
    ],
)
def test_persist_only_boolean_preserving_toml(tmp_path: Path, memory: str) -> None:
    path = tmp_path / "config.toml"
    original = (
        memory + '# Model comments\n[model]\nname = "test"\n[mcp.servers.local]\n'
        'command = "server"\n[runtime]\nmax_steps = 22\n'
    )
    path.write_text(original, encoding="utf-8")
    before = tomllib.loads(original)
    for enabled in (False, True, False):
        set_memory_enabled(enabled, path)
        after = tomllib.loads(path.read_text(encoding="utf-8"))
        expected = dict(before)
        expected["memory"] = {**before.get("memory", {}), "enabled": enabled}
        assert after == expected
        assert "# Model comments" in path.read_text(encoding="utf-8")
        assert memory_settings(path)[0] is enabled


def test_bad_config_and_atomic_failure_leave_original(tmp_path: Path, monkeypatch: Any) -> None:
    path = tmp_path / "config.toml"
    path.write_text("invalid = [", encoding="utf-8")
    with pytest.raises(ConfigError):
        set_memory_enabled(False, path)
    assert path.read_text() == "invalid = ["
    original = '[model]\nname = "test"\n'
    path.write_text(original, encoding="utf-8")

    def fail(*args: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(configuration, "atomic_write", fail)
    with pytest.raises(OSError):
        set_memory_enabled(False, path)
    assert path.read_text() == original


async def test_cli_off_no_dependency_work_and_keeps_files(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    store = MemoryStore(tmp_path / ".nexus/memory")
    entry = store.write("MEMORY.md", "preserve")
    store.forget("MEMORY.md", entry["id"], entry["revision"])
    store.write("MEMORY.md", "keep authority")
    original = {p.name: p.read_bytes() for p in store.root.rglob("*") if p.is_file()}
    monkeypatch.setattr(cli, "DependencyBootstrap", lambda: pytest.fail("OFF must not install"))
    assert await cli.application(argparse.Namespace(command="memory", action="off")) == 0
    assert memory_settings()[0] is False
    assert original == {p.name: p.read_bytes() for p in store.root.rglob("*") if p.is_file()}


async def test_cli_on_persists_even_when_setup_fails(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    class Failure:
        async def ensure(self, emit: Any) -> tuple[str, ...]:
            raise RuntimeError("offline network")

    monkeypatch.setattr(cli, "DependencyBootstrap", Failure)
    assert await cli.application(argparse.Namespace(command="memory", action="on")) == 1
    assert memory_settings()[0] is True
    assert cli.memory_status()["index"]["status"] == "unavailable"


async def test_interactive_switch_opens_clean_sessions_and_blocks_memory_resume(
    tmp_path: Path, monkeypatch: Any
) -> None:
    cfg = config(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    path = tmp_path / ".nexus/config.toml"
    path.parent.mkdir()
    path.write_text('[model]\nname="test"\napi_key_env="TEST_CONVERSATION_KEY"\n', encoding="utf-8")
    MemoryStore(tmp_path / ".nexus/memory").write("MEMORY.md", "PRIVATE_MEMORY_SENTINEL")
    models: list[Any] = []
    conversations: list[Any] = []

    class Main(ScriptedModel):
        async def close(self) -> None:
            pass

    def model(*args: Any) -> Main:
        value = Main([reply(text="done")])
        models.append(value)
        return value

    original = bootstrap.Conversation

    def conversation(*args: Any, **kwargs: Any) -> bootstrap.Conversation:
        value = original(*args, **kwargs)
        conversations.append(value)
        return value

    answers = iter(
        ["first", "/memory off", "second", "/memory status", "/memory on", "third", "/exit"]
    )

    class Prompt:
        def __init__(self, **kwargs: Any) -> None:
            from types import SimpleNamespace

            from prompt_toolkit.input import DummyInput

            self.app = SimpleNamespace(input=DummyInput())

        async def prompt_async(self, text: str, **kwargs: Any) -> str:
            return next(answers)

    output = io.StringIO()
    monkeypatch.setattr(cli, "PromptSession", Prompt)
    monkeypatch.setattr(cli, "Console", lambda **k: Console(file=output, width=160))
    monkeypatch.setattr(cli, "Conversation", conversation)
    monkeypatch.setattr(bootstrap, "ChatModel", model)
    with create_app_session(output=DummyOutput()):
        assert await cli.application(argparse.Namespace(command=None)) == 0
    assert len(conversations) == 3 and all(c.closed for c in conversations)
    assert "PRIVATE_MEMORY_SENTINEL" in models[0].requests[0][0].content
    assert "PRIVATE_MEMORY_SENTINEL" not in str(models[1].requests)
    assert "memory_write" not in conversations[1].registry
    assert (
        conversations[1].memory_index is None and conversations[1].session.memory_snapshot is None
    )
    assert "PRIVATE_MEMORY_SENTINEL" in models[2].requests[0][0].content
    assert "New Session opened" in output.getvalue()
    assert memory_settings()[0] is True
    cfg.memory_enabled = False
    with pytest.raises(SessionError, match="Memory is OFF"):
        original(tmp_path, cfg, ignore, resume=conversations[0].writer.path)
    off_resume = original(tmp_path, cfg, ignore, resume=conversations[1].writer.path)
    await off_resume.close()
