from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from nexus.app.tui import select_model


@pytest.mark.parametrize(
    "keys,expected",
    [
        ("\r", "qwen"),
        ("\x1b[A\r", "default"),
        ("\x1b[B\r", "deepseek"),
        ("\x1b[A\x1b[A\r", "default"),
        ("\x1b[B\x1b[B\r", "deepseek"),
        ("\x1b", None),
        ("\x03", None),
    ],
)
async def test_model_menu_keys(keys: str, expected: str | None) -> None:
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        pipe.send_text(keys)
        assert await select_model(
            {"default": "openai/test", "qwen": "dashscope/test", "deepseek": "deepseek/test"},
            "qwen",
        ) == expected


@pytest.mark.parametrize(
    "command,selection,expected",
    [("/model", "qwen", ["qwen"]), ("/model", None, []),
     ("/model", "default", []), ("/model qwen", None, ["qwen"])],
)
async def test_model_command(
    tmp_path: Path, monkeypatch: Any, command: str, selection: str | None, expected: list[str]
) -> None:
    from prompt_toolkit import Application
    from prompt_toolkit.input import DummyInput

    from nexus.app import cli
    from nexus.app.config import Config, ModelConfig
    from nexus.core.inbox import Inbox
    from nexus.core.security import SecurityGateway
    from nexus.core.types import Limits, Session

    cfg = Config(ModelConfig("test", 32768), Limits())
    cfg.models = {"qwen": ModelConfig("qwen-test", 32768)}
    calls: list[str] = []
    answers = iter([command, "/exit"])

    class Conversation:
        gateway = SecurityGateway()
        config = cfg
        default_model = cfg.model
        model_alias = "default"
        session = Session(tmp_path)
        coordinator = None

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.inbox = Inbox()

        async def start_memory(self) -> None:
            pass

        async def summarize_exit(self, reason: str) -> None:
            pass

        async def close(self) -> None:
            pass

        async def select_model(self, alias: str) -> None:
            calls.append(alias)

    class Prompt:
        def __init__(self, **kwargs: Any) -> None:
            self.app: Application[str] = Application(input=DummyInput(), output=DummyOutput())

        async def prompt_async(self, text: str, **kwargs: Any) -> str:
            return next(answers)

    async def select(models: dict[str, str], current: str) -> str | None:
        assert command == "/model"
        assert list(models) == ["default", "qwen"]
        assert current == "default"
        return selection

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(cli, "read_toml", lambda _: {"model": {"name": "test"}})
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    monkeypatch.setattr(cli, "PromptSession", Prompt)
    monkeypatch.setattr(cli, "Conversation", Conversation)
    monkeypatch.setattr(cli, "select_model", select)
    with create_app_session(output=DummyOutput()):
        assert await cli.application(argparse.Namespace(command=None)) == 0
    assert calls == expected
