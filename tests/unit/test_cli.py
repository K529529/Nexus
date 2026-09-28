import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

from pydantic import SecretStr
from pytest import MonkeyPatch
from typer.testing import CliRunner

import nexus.interfaces.cli.app as cli_module
import nexus.interfaces.cli.profile as profile_module
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
    assert "--profile" in chat_help_text
    assert "--api-key" not in chat_help_text


class FakeRuntime:
    async def run(
        self,
        task: str,
        session_id: str | None = None,
    ) -> AsyncIterator[RuntimeEvent]:
        yield TaskStarted(run_id="test-run", session_id=session_id, task=task)
        yield FinalResult(run_id="test-run", session_id=session_id, content="Short greeting.")


def test_mocked_chat_smoke(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    config = RuntimeConfig(model_name="mock-model", model_api_key=SecretStr("mock-secret"))

    @asynccontextmanager
    async def fake_bootstrap(
        resolved: RuntimeConfig, *, local_profile_input_diagnostics: bool = False,
    ) -> AsyncIterator[SimpleNamespace]:
        assert resolved == config
        assert local_profile_input_diagnostics is False
        yield SimpleNamespace(runtime=FakeRuntime())

    monkeypatch.setattr(cli_module, "load_runtime_config", lambda **_: config)
    monkeypatch.setattr(cli_module, "bootstrap_application", fake_bootstrap)
    monkeypatch.setattr(profile_module, "_NEXUS_PROJECT_ROOT", tmp_path)

    result = runner.invoke(app, ["chat", "Reply with a short greeting."])

    assert result.exit_code == 0
    assert "Analyzing repository" in result.stdout
    assert "Task started" not in result.stdout
    assert "Short greeting." in result.stdout
    assert "Execution Profile" not in result.stdout
    assert "Profile saved:" not in result.stdout
    assert not (tmp_path / "profiles").exists()


def test_profile_save_preserves_utf8_lines(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(profile_module, "_NEXUS_PROJECT_ROOT", tmp_path)
    profile = profile_module.ExecutionProfile()
    lines = ["Execution Profile", "Agent model inputs", "selected_files=文件.py"]
    monkeypatch.setattr(profile, "lines", lambda: lines)

    saved_path = profile.save()

    assert saved_path.read_bytes() == "\n".join(lines).encode("utf-8")


def test_chat_profile_is_saved_after_failure(
    monkeypatch: MonkeyPatch, tmp_path: Path,
) -> None:
    config = RuntimeConfig(model_name="mock-model", model_api_key=SecretStr("mock-secret"))

    class FailingRuntime:
        async def run(self, task: str) -> AsyncIterator[RuntimeEvent]:
            yield TaskStarted(run_id="test-run", session_id=None, task=task)
            from nexus.domain.runtime_events import ErrorOccurred

            yield ErrorOccurred(
                run_id="test-run",
                session_id=None,
                code="INVALID_PLAN_OUTPUT",
                message="The model returned an invalid Plan.",
                retryable=True,
                failure_category="STEP_SCHEMA",
            )

    @asynccontextmanager
    async def fake_bootstrap(
        resolved: RuntimeConfig, *, local_profile_input_diagnostics: bool = False,
    ) -> AsyncIterator[SimpleNamespace]:
        assert resolved == config
        assert local_profile_input_diagnostics is True
        yield SimpleNamespace(runtime=FailingRuntime())

    nexus_root = tmp_path / "nexus"
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    monkeypatch.chdir(repository_root)
    monkeypatch.setattr(cli_module, "load_runtime_config", lambda **_: config)
    monkeypatch.setattr(cli_module, "bootstrap_application", fake_bootstrap)
    monkeypatch.setattr(profile_module, "_NEXUS_PROJECT_ROOT", nexus_root)

    result = runner.invoke(app, ["chat", "Work", "--profile"])

    assert result.exit_code == 1
    assert "Execution Profile" not in result.stdout
    assert "Failure category    STEP_SCHEMA" not in result.stdout
    saved_line = result.stdout.splitlines()[-1]
    assert saved_line.startswith("Profile saved: ")
    saved_path = Path(saved_line.removeprefix("Profile saved: "))
    assert saved_path.parent == nexus_root / "profiles"
    assert not (repository_root / "profiles").exists()
    assert re.fullmatch(r"profile-\d{8}-\d{6}-\d{6}\.txt", saved_path.name)
    profile_text = saved_path.read_text(encoding="utf-8")
    assert "Execution Profile" in profile_text
    assert "Failure category    STEP_SCHEMA" in profile_text
    assert "Error [INVALID_PLAN_OUTPUT]" in result.stderr
