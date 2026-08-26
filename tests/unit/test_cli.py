import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace

from pydantic import SecretStr
from pytest import MonkeyPatch
from typer.testing import CliRunner

import nexus.interfaces.cli.app as cli_module
from nexus.config.models import RuntimeConfig
from nexus.domain.runtime_events import FinalResult, RuntimeEvent, TaskStarted
from nexus.interfaces.cli.app import app

runner = CliRunner()
ANSI_ESCAPE_SEQUENCE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def test_root_help_exits_successfully() -> None:
    result = runner.invoke(app, [])
    assert result.exit_code == 0
    assert "Usage" in result.stdout
    assert "chat" in result.stdout


def test_explicit_and_chat_help() -> None:
    root_help = runner.invoke(app, ["--help"], color=False)
    chat_help = runner.invoke(app, ["chat", "--help"], color=False)
    chat_help_text = ANSI_ESCAPE_SEQUENCE.sub("", chat_help.stdout)

    assert root_help.exit_code == 0
    assert chat_help.exit_code == 0
    assert "TASK" in chat_help_text
    assert "--model" in chat_help_text
    assert "--base-url" in chat_help_text
    assert "--api-key" not in chat_help_text


class FakeRuntime:
    async def run(
        self,
        task: str,
        session_id: str | None = None,
    ) -> AsyncIterator[RuntimeEvent]:
        yield TaskStarted(run_id="test-run", session_id=session_id, task=task)
        yield FinalResult(run_id="test-run", session_id=session_id, content="Short greeting.")


def test_mocked_chat_smoke(monkeypatch: MonkeyPatch) -> None:
    config = RuntimeConfig(model_name="mock-model", model_api_key=SecretStr("mock-secret"))

    @asynccontextmanager
    async def fake_bootstrap(resolved: RuntimeConfig) -> AsyncIterator[SimpleNamespace]:
        assert resolved == config
        yield SimpleNamespace(runtime=FakeRuntime())

    monkeypatch.setattr(cli_module, "load_runtime_config", lambda **_: config)
    monkeypatch.setattr(cli_module, "bootstrap_application", fake_bootstrap)

    result = runner.invoke(app, ["chat", "Reply with a short greeting."])

    assert result.exit_code == 0
    assert "Task started" in result.stdout
    assert "Short greeting." in result.stdout
