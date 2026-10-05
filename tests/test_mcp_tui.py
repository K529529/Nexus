from __future__ import annotations

import argparse
import io
import json
import re
import subprocess
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from nexus.app import bootstrap, cli
from nexus.app.config import Config, ConfigError, ModelConfig, load_config
from nexus.app.events import Events
from nexus.app.session import SessionLog, read_records
from nexus.app.tui import (
    Transcript,
    json_consumer,
    select_session,
    setup,
    terminal_text,
    tool_title,
)
from nexus.core.agent import run_turn
from nexus.core.types import (
    ExecutionContext,
    Json,
    Limits,
    RuntimeEvent,
    Session,
    Tool,
    ToolResult,
    ToolSpec,
)
from nexus.tools.mcp import adapt_tool, connect_servers, public_name
from nexus.tools.registry import native_tools
from tests.conftest import Recorder, ScriptedModel, call, reply


async def test_real_stdio_and_failed_server_same_loop(tmp_path: Path) -> None:
    registry = native_tools()
    events = Recorder()
    server = Path(__file__).parent / "fixtures/mcp_server.py"
    async with AsyncExitStack() as stack:
        unavailable = await connect_servers(
            {
                "broken": {"command": "nexus-test-no-such-executable"},
                "local": {"command": sys.executable, "args": [str(server.resolve())]},
            },
            registry,
            stack,
            events,
        )
        assert unavailable == ["broken"]
        assert {"exec_command", "apply_patch"} <= registry.keys()
        name = public_name("local", "add")
        model = ScriptedModel([reply(call(name, {"a": 3, "b": 4})), reply(text="7")])
        session = Session(tmp_path)
        result = await run_turn(session, "add", model, registry, events, Limits())
        assert result.outcome == "completed"
        value = json.loads([m for m in model.requests[-1] if m.role == "tool"][-1].content)
        assert value["ok"] and value["data"]["structured_content"]["sum"] == 7


async def test_mcp_pagination_invalid_partial_discovery_and_names() -> None:
    class FakeClient:
        def __init__(self, parameters: Any, **kwargs: Any) -> None:
            self.command = parameters.command
            assert kwargs["input_required_max_rounds"] == 0

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *args: Any) -> None:
            pass

        async def list_tools(self, cursor: str | None = None) -> Any:
            name = "first" if cursor is None else "second"
            return SimpleNamespace(
                result_type="complete",
                tools=[
                    SimpleNamespace(name=name, description="test", input_schema={"type": "object"})
                ],
                next_cursor="again" if cursor is None or self.command == "bad" else None,
            )

    registry = native_tools()
    async with AsyncExitStack() as stack:
        unavailable = await connect_servers(
            {"bad": {"command": "bad"}, "好": {"command": "good"}},
            registry,
            stack,
            Recorder(),
            client_factory=FakeClient,
        )
        assert unavailable == ["bad"]
        assert len(registry) == 4
        assert all(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name) for name in registry)
        assert public_name("a.b", "c") != public_name("a", "b.c")


async def test_mcp_output_error_budget_and_no_retry(tmp_path: Path) -> None:
    calls = 0

    class FakeClient:
        async def call_tool(self, *args: Any, **kwargs: Any) -> Any:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise TimeoutError
            return SimpleNamespace(
                content=[
                    SimpleNamespace(type="text", text="x" * 1000),
                    SimpleNamespace(type="image", data="PRIVATE_BASE64"),
                ],
                structured_content={"large": "y" * 500},
                result_type="complete",
                is_error=True,
            )

    registry = native_tools()
    spec = ToolSpec("remote", "desc", {"type": "object"})
    tool = adapt_tool(FakeClient(), "original", spec, "server", set(), registry, ["remote"])
    registry["remote"] = tool
    context = ExecutionContext(tmp_path, "id", "/bin/sh", 256)
    first = await tool.execute({}, context, Recorder())
    assert not first.ok and first.truncated
    assert "PRIVATE_BASE64" not in str(first)
    second = await tool.execute({}, context, Recorder())
    assert second.error_code == "mcp_timeout" and second.data["side_effects"] == "unknown"
    assert calls == 2 and "remote" not in registry


async def test_transcript_no_duplicate_final_and_controls() -> None:
    output = io.StringIO()
    transcript = Transcript(Console(file=output, color_system=None, width=60))
    for kind, data in [
        ("model_started", {}),
        ("assistant_delta", {"text": "Unique final"}),
        ("message", {"role": "assistant", "content": "Unique final"}),
    ]:
        await transcript(RuntimeEvent(kind, "time", "session", "run", data))
    assert output.getvalue().count("Unique final") == 1
    assert terminal_text("safe\x1b[2J\r\x00text") == "safetext"


@pytest.mark.parametrize(
    "command,title",
    [
        ('Get-Content "src/file name.py" -TotalCount 10', 'Read "src/file name.py" -TotalCount 10'),
        ("cat a.py", "Read a.py"),
        ('sed -n "1,20p" a.py', 'Read -n "1,20p" a.py'),
        ("head -20 a.py", "Read -20 a.py"),
        ("tail -20 a.py", "Read -20 a.py"),
        ('rg "run_turn" tests', 'Search "run_turn" tests'),
        ("grep pattern a.py", "Search pattern a.py"),
        ("Select-String -Pattern run_turn -Path a.py", "Search -Pattern run_turn -Path a.py"),
        ("pytest -q", "Test pytest -q"),
        ("uv run pytest -q", "Test uv run pytest -q"),
        ("git status --short", "Inspect git status --short"),
        ("git diff", "Inspect git diff"),
        ("git log -3", "Inspect git log -3"),
        ("python script.py", "Run python script.py"),
        ("cat a; cat b", "Run shell command"),
        ("rg x | head", "Run shell command"),
        ("echo " + "x" * 2000, "Run shell command"),
    ],
)
def test_ui_command_labels_do_not_change_arguments(command: str, title: str) -> None:
    data = {"name": "exec_command", "arguments_json": json.dumps({"command": command})}
    original = dict(data)
    assert tool_title(data) == title
    assert data == original


async def test_model_activity_is_silent_and_narrow_success_is_one_line() -> None:
    output = io.StringIO()
    ui = Transcript(Console(file=output, width=40, color_system=None))
    await ui(RuntimeEvent("model_started", "t", "s", "r", {}))
    await ui(RuntimeEvent("message", "t", "s", "r", {"role": "assistant", "content": ""}))
    assert output.getvalue() == ""
    await ui(
        RuntimeEvent(
            "tool_started",
            "t",
            "s",
            "r",
            {
                "call_id": "c",
                "name": "exec_command",
                "arguments_json": json.dumps({"command": "cat " + "folder/" * 15 + "file.py"}),
            },
        )
    )
    value = ToolResult("c", True, {"exit_code": 0}, duration_ms=800)
    await ui(RuntimeEvent("message", "t", "s", "r", value.message().public()))
    rows = output.getvalue().splitlines()
    assert len(rows) == 1 and len(rows[0]) <= 40
    assert "✓ · 0.8s" in rows[0] and "…" in rows[0]


async def test_terminal_activity_is_transient_and_removed_on_abort(monkeypatch: Any) -> None:
    monkeypatch.setenv("TERM", "xterm-256color")
    output = io.StringIO()
    console = Console(file=output, force_terminal=True, legacy_windows=False)
    ui = Transcript(console)
    await ui(
        RuntimeEvent(
            "tool_started",
            "t",
            "s",
            "r",
            {
                "call_id": "c",
                "name": "exec_command",
                "arguments_json": '{"command":"pytest"}',
            },
        )
    )
    assert ui.activity is not None and ui.activity.transient
    activity = ui.activity
    await ui(
        RuntimeEvent(
            "run_finished",
            "t",
            "s",
            "r",
            {
                "outcome": "aborted",
                "duration_ms": 100,
                "usage": {},
                "reason": "user_abort",
            },
        )
    )
    assert ui.activity is None and not ui.tools and not activity.is_started
    assert "\x1b[2K" in output.getvalue()


async def test_dumb_terminal_does_not_leave_temporary_activity(monkeypatch: Any) -> None:
    monkeypatch.setenv("TERM", "dumb")
    output = io.StringIO()
    ui = Transcript(Console(file=output, force_terminal=True))
    await ui(RuntimeEvent("tool_started", "t", "s", "r", {"name": "exec_command"}))
    assert ui.activity is None and output.getvalue() == ""


async def test_json_consumer_retains_full_public_tool_events(capsys: Any) -> None:
    body = "source content\n" * 1000
    value = ToolResult("c", True, {"stdout": body, "exit_code": 0})
    event = RuntimeEvent("message", "t", "s", "r", value.message().public())
    await json_consumer(event)
    row = json.loads(capsys.readouterr().out)
    assert json.loads(row["data"]["content"])["data"]["stdout"] == body


@pytest.mark.parametrize("streamed", [True, False])
async def test_transcript_hides_tool_bodies_and_keeps_read_status(streamed: bool) -> None:
    output = io.StringIO()
    transcript = Transcript(Console(file=output, color_system=None))
    await transcript(
        RuntimeEvent(
            "tool_started",
            "time",
            "s",
            "r",
            {
                "name": "exec_command",
                "call_id": "c",
                "arguments_json": '{"command":"Get-Content README.md"}',
            },
        )
    )
    body = "FILE_BODY\n" * 1000
    if streamed:
        await transcript(
            RuntimeEvent("tool_output_delta", "time", "s", "r", {"call_id": "c", "text": body})
        )
    assert output.getvalue() == ""
    value = ToolResult("c", True, {"stdout": body, "stderr": "", "exit_code": 0})
    await transcript(RuntimeEvent("message", "time", "s", "r", value.message().public()))
    assert len(output.getvalue().splitlines()) == 1
    assert "Read README.md" in output.getvalue() and "✓" in output.getvalue()
    # MCP text and structured content must not dump entire documents either.
    value = ToolResult("m", True, {"text": [body], "structured_content": {"document": body}})
    await transcript(RuntimeEvent("message", "time", "s", "r", value.message().public()))
    assert "FILE_BODY" not in output.getvalue()
    assert "exit=0" not in output.getvalue()
    assert len(output.getvalue()) < 200


@pytest.mark.parametrize(
    "error,data",
    [
        ("command_exit_nonzero", {"exit_code": 1, "stderr": "noise\n" * 500 + "AssertionError"}),
        ("command_timeout", {"exit_code": -1}),
        ("cleanup_incomplete", {"exit_code": 0}),
        ("patch_io_error", {"partial": True, "detail": "PermissionError"}),
        ("mcp_timeout", {"side_effects": "unknown"}),
        ("mcp_tool_error", {"text": ["noise\n" * 500 + "REMOTE_ERROR"]}),
    ],
)
async def test_transcript_failures_remain_visible_and_bounded(error: str, data: Json) -> None:
    output = io.StringIO()
    transcript = Transcript(Console(file=output, color_system=None))
    result = ToolResult("c", False, data, error)
    await transcript(RuntimeEvent("message", "time", "s", "r", result.message().public()))
    rendered = output.getvalue()
    assert error in rendered and len(rendered) < 600
    for field, expected in [
        ("stderr", "AssertionError"),
        ("detail", "PermissionError"),
        ("partial", "partial changes"),
        ("side_effects", "side effects unknown"),
        ("text", "REMOTE_ERROR"),
    ]:
        if field in data:
            assert expected in rendered


async def test_transcript_bounds_compound_commands_and_patch_previews() -> None:
    output = io.StringIO()
    transcript = Transcript(Console(file=output, color_system=None, width=60))
    await transcript(
        RuntimeEvent(
            "tool_started",
            "time",
            "s",
            "r",
            {
                "name": "exec_command",
                "call_id": "p",
                "arguments_json": json.dumps({"command": "Get-Content a;\n" + "echo LONG;" * 1000}),
            },
        )
    )
    assert output.getvalue() == ""
    files = [
        {
            "status": "modified",
            "path": f"file{n}.py",
            "added_lines": 200,
            "deleted_lines": 1,
            "diff": "--- a/file.py\n+++ b/file.py\n@@ -1 +1,200 @@\n-old\n+new\n" + "+" * 2000,
        }
        for n in range(20)
    ]
    result = ToolResult("p", True, {"files": files, "omitted_files": 2}, truncated=True)
    await transcript(RuntimeEvent("message", "time", "s", "r", result.message().public()))
    rendered = output.getvalue()
    assert "Run shell command" in rendered and "Get-Content" not in rendered
    assert "file0.py" in rendered and "+new" in rendered
    assert "file3.py" not in rendered and "19 more changed files" in rendered
    assert "tool result truncated" in rendered
    assert len(rendered) < 1400


async def test_compact_transcript_preserves_model_observation_and_session(tmp_path: Path) -> None:
    body = "important source line\n" * 1000
    model = ScriptedModel([reply(call("read", {})), reply(text="Final explanation")])

    async def execute(args: Json, context: ExecutionContext, emit: Any) -> ToolResult:
        await emit("tool_output_delta", {"call_id": context.call_id, "text": body})
        return ToolResult(context.call_id, True, {"stdout": body, "exit_code": 0})

    session = Session(tmp_path)
    writer = SessionLog.create(session, "explain", {"name": "test"}, tmp_path)
    output = io.StringIO()
    events = Events(session, writer, Transcript(Console(file=output, color_system=None)))
    try:
        result = await run_turn(
            session,
            "explain",
            model,
            {"read": Tool(ToolSpec("read", "read", {"type": "object"}), execute)},
            events,
            Limits(context_window=100000),
        )
    finally:
        writer.close()
    assert result.outcome == "completed"
    tool_message = [m for m in model.requests[-1] if m.role == "tool"][-1]
    assert json.loads(tool_message.content)["data"]["stdout"] == body
    assert "important source line" not in output.getvalue()
    assert output.getvalue().count("Final explanation") == 1
    with writer.path.open("rb") as stream:
        records, truncated = read_records(stream)
    assert not truncated
    tools = [r for r in records if r["kind"] == "message" and r["data"]["role"] == "tool"]
    assert json.loads(tools[0]["data"]["content"])["data"]["stdout"] == body


async def test_selector_keyboard_and_escape() -> None:
    items = [
        {
            "path": str(n),
            "title": f"Task {n}",
            "project": "repo",
            "updated": "today",
            "status": "completed",
        }
        for n in (1, 2)
    ]
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        pipe.send_text("\x1b[B\r")
        assert await select_session(items) == Path("2")
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        pipe.send_text("\x1b")
        assert await select_session(items) is None


async def test_setup_preserves_other_sections_and_never_key(
    tmp_path: Path, monkeypatch: Any
) -> None:
    path = tmp_path / "config.toml"
    original = '[mcp.servers.test]\ncommand="server"\n# keep comment\n'
    path.write_text(original, encoding="utf-8")
    answers = iter(["model", "https://example.com/v1", "32768", "TEST_SETUP_KEY", "high", "y"])
    monkeypatch.setenv("TEST_SETUP_KEY", "PRIVATE_SECRET")

    async def ask(text: str) -> str:
        return next(answers)

    output = io.StringIO()
    assert await setup(path, ask, Console(file=output))
    assert original in path.read_text()
    assert "PRIVATE_SECRET" not in path.read_text() + output.getvalue()
    assert load_config(path).model.reasoning_effort == "high"
    before = path.read_bytes()

    async def cancel(text: str) -> str:
        raise EOFError

    assert not await setup(path, cancel, Console(file=output))
    assert path.read_bytes() == before
    path.write_text("[broken")
    with pytest.raises(ConfigError):
        await setup(path, ask, Console(file=output))
    assert path.read_text() == "[broken"


@pytest.mark.parametrize(
    "argv,code", [(["--help"], 0), (["--version"], 0), ([], 2), (["resume"], 2)]
)
def test_cli_offline_and_non_tty(argv: list[str], code: int) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "nexus", *argv],
        input="",
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == code
    if code == 2:
        assert "exec" in result.stderr and "TTY" in result.stderr


async def test_exec_json_exit_code_and_same_runtime(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    class FakeModel(ScriptedModel):
        async def close(self) -> None:
            pass

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TEST_EXEC_KEY", "secret-exec")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    config = Config(ModelConfig("test", 32768, api_key_env="TEST_EXEC_KEY"), Limits())
    monkeypatch.setattr(cli, "load_config", lambda: config)
    monkeypatch.setattr(bootstrap, "ChatModel", lambda _: FakeModel([reply(text="done")]))
    assert await cli.application(argparse.Namespace(command="exec", task="test", json=True)) == 0
    events: list[Json] = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events[-1]["kind"] == "run_finished" and events[-1]["data"]["outcome"] == "completed"
    assert "secret-exec" not in str(events) and "protocol_data" not in str(events)


async def test_cancel_resume_keeps_current_conversation(tmp_path: Path, monkeypatch: Any) -> None:
    instances: list[Any] = []
    answers = iter(["/resume", "/exit"])

    class FakeConversation:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.closed = False
            instances.append(self)

        async def close(self) -> None:
            self.closed = True

    class FakePrompt:
        async def prompt_async(self, text: str) -> str:
            assert len(instances) == 1 and not instances[0].closed
            return next(answers)

    async def cancel(items: Any) -> None:
        assert not instances[0].closed

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(cli, "read_toml", lambda _: {"model": {"name": "test"}})
    monkeypatch.setattr(cli, "load_config", lambda: object())
    monkeypatch.setattr(cli, "PromptSession", FakePrompt)
    monkeypatch.setattr(cli, "Conversation", FakeConversation)
    monkeypatch.setattr(cli, "list_sessions", lambda _: [])
    monkeypatch.setattr(cli, "select_session", cancel)
    assert await cli.application(argparse.Namespace(command=None)) == 0
    assert len(instances) == 1 and instances[0].closed


def test_signal_wakes_idle_event_loop() -> None:
    # A separate interpreter owns SIGINT; never interrupt pytest itself.
    code = """
import asyncio, os, signal, threading, time
from nexus.app.cli import drive
from nexus.core.types import RunResult
class Conversation:
    async def turn(self, text):
        # Windows os.kill(SIGINT) terminates rather than delivering a console event.
        def interrupt():
            if os.name == 'nt':
                signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
            else:
                os.kill(os.getpid(), signal.SIGINT)
        threading.Timer(.2, interrupt).start()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            return RunResult('aborted')
        raise AssertionError('signal failed to wake event loop')
started = time.monotonic()
result = asyncio.run(drive(Conversation(), 'test'))
assert result.outcome == 'aborted'
assert time.monotonic() - started < 5
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
