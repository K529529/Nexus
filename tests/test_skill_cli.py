from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path
from typing import Any

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from nexus.app import bootstrap, cli
from nexus.app.session import list_sessions, read_records
from nexus.app.tui import select_session, show_skills
from nexus.core.types import Session
from tests.conftest import ScriptedModel, reply
from tests.test_conversation import config, ignore
from tests.test_skills import install


async def test_interactive_select_switch_off_new_no_model_calls(
    tmp_path: Path, monkeypatch: Any
) -> None:
    cfg = config(monkeypatch)
    install(tmp_path / ".nexus" / "skills")
    install(tmp_path / ".nexus" / "skills", "other")
    answers = iter(
        [
            "/skills",
            "/skill missing",
            "/skill regression",
            "/skills",
            "/skill other",
            "/skill off",
            "/skills",
            "/skill regression",
            "/new",
            "/skills",
            "/typo",
            "/exit",
        ]
    )
    conversations: list[bootstrap.Conversation] = []
    original = bootstrap.Conversation

    def create(*args: Any, **kwargs: Any) -> bootstrap.Conversation:
        value = original(*args, **kwargs)
        conversations.append(value)
        return value

    class Prompt:
        async def prompt_async(self, text: str) -> str:
            return next(answers)

    def no_model(_: Any) -> None:
        raise AssertionError("Slash commands must not call the model")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(cli, "read_toml", lambda _: {"model": {"name": "test"}})
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    monkeypatch.setattr(cli, "PromptSession", Prompt)
    monkeypatch.setattr(cli, "Conversation", create)
    monkeypatch.setattr(bootstrap, "ChatModel", no_model)
    output = io.StringIO()
    monkeypatch.setattr(cli, "Console", lambda **_: Console(file=output, width=160))
    assert await cli.application(argparse.Namespace(command=None)) == 0
    assert len(conversations) == 2 and all(c.closed for c in conversations)
    assert conversations[0].session.selected_skill is not None
    assert conversations[1].session.selected_skill is None
    assert conversations[1].writer is None
    writer = conversations[0].writer
    assert writer
    with writer.path.open("rb") as stream:
        records, _ = read_records(stream)
    selections = [r["data"]["skill"] for r in records if r["kind"] == "skill_selected"]
    assert [s["name"] if s else None for s in selections] == [
        "regression",
        "other",
        None,
        "regression",
    ]
    assert "Unknown Skill" in output.getvalue() and "Unknown command" in output.getvalue()
    assert (
        "[selected for session]" in output.getvalue()
        and "off (automatic mode)" in output.getvalue()
    )


@pytest.mark.parametrize(
    "argv",
    [
        ["nexus", "--skill", "regression"],
        ["nexus", "exec", "task", "--skill", "regression"],
        ["nexus", "--skill", "regression", "exec", "task"],
    ],
)
def test_argument_forms(argv: list[str], monkeypatch: Any) -> None:
    seen: list[argparse.Namespace] = []

    async def app(args: argparse.Namespace) -> int:
        seen.append(args)
        return 0

    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(cli, "application", app)
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0 and seen[0].skill == "regression"


async def test_offline_catalog_without_config_or_key(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    install(tmp_path / ".nexus" / "skills")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda: pytest.fail("No model configuration needed"))
    assert await cli.application(argparse.Namespace(command="skills")) == 0
    assert "regression" in capsys.readouterr().out


async def test_selector_number_empty_and_cancel() -> None:
    items = [
        {
            "path": str(i),
            "title": "Task\nwith\x1b[31m controls",
            "project": "test",
            "updated": "2026-10-06T01:02:03+00:00",
            "status": "completed",
        }
        for i in range(12)
    ]
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        pipe.send_text("2")
        assert await select_session(items) == Path("1")
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        pipe.send_text("\x1b[B" * 9 + "2")
        assert await select_session(items) == Path("10")
    assert await select_session([]) is None


async def test_resume_bad_selection_keeps_current(tmp_path: Path, monkeypatch: Any) -> None:
    cfg = config(monkeypatch)
    bad = tmp_path / "bad.jsonl"
    bad.write_text("broken", encoding="utf-8")
    answers = iter(["/resume", "/exit"])
    instances: list[bootstrap.Conversation] = []
    original = bootstrap.Conversation

    def create(*args: Any, **kwargs: Any) -> bootstrap.Conversation:
        value = original(*args, **kwargs)
        instances.append(value)
        return value

    class Prompt:
        async def prompt_async(self, text: str) -> str:
            assert len(instances) == 1 and not instances[0].closed
            return next(answers)

    async def select(_: Any) -> Path:
        return bad

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(cli, "read_toml", lambda _: {"model": {"name": "test"}})
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    monkeypatch.setattr(cli, "PromptSession", Prompt)
    monkeypatch.setattr(cli, "Conversation", create)
    monkeypatch.setattr(cli, "select_session", select)
    assert await cli.application(argparse.Namespace(command=None)) == 0
    assert len(instances) == 1 and instances[0].closed


async def test_recent_session_uses_last_user_task(tmp_path: Path, monkeypatch: Any) -> None:
    cfg = config(monkeypatch)

    class Model(ScriptedModel):
        async def close(self) -> None:
            pass

    monkeypatch.setattr(bootstrap, "ChatModel", lambda _: Model([reply(text="done")]))
    for text in ("first task", "second task"):
        convo = bootstrap.Conversation(tmp_path, cfg, ignore, home=tmp_path)
        try:
            await convo.turn(text)
        finally:
            await convo.close()
    items = list_sessions(tmp_path, tmp_path)
    assert [s["title"] for s in items] == ["second task", "first task"]
    assert all(s["status"] == "completed" and s["session_id"] for s in items)


async def test_selection_write_failure_never_changes_state(
    tmp_path: Path, monkeypatch: Any
) -> None:
    from nexus.app.session import SessionError

    install(tmp_path / "skills")
    convo = bootstrap.Conversation(tmp_path, config(monkeypatch), ignore, home=tmp_path)
    try:
        await convo.select_skill("regression")
        selected = convo.session.selected_skill
        assert convo.writer

        def fail(*args: Any, **kwargs: Any) -> int:
            raise SessionError("injected failure")

        monkeypatch.setattr(convo.writer, "append", fail)
        with pytest.raises(SessionError):
            await convo.select_skill(None)
        assert convo.session.selected_skill is selected
    finally:
        await convo.close()
    assert list_sessions(tmp_path, tmp_path)[0]["status"] == "idle"


def test_saved_skill_display_without_source(tmp_path: Path) -> None:
    from nexus.core.skills import snapshot

    session = Session(tmp_path, selected_skill=snapshot("saved", "description", "body"))
    output = io.StringIO()
    show_skills(session, tmp_path, [], Console(file=output))
    assert "saved snapshot, source unavailable" in output.getvalue()
