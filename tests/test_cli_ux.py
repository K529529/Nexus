from __future__ import annotations

import argparse
import asyncio
import io
import json
import signal
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from prompt_toolkit import PromptSession
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console
from rich.markdown import Markdown
from rich.text import Text

from nexus.app import bootstrap, cli, tui
from nexus.app.input import TaskInput
from nexus.app.profile import InteractionSummary
from nexus.app.session import read_records, replay
from nexus.core.types import Json, RunResult, RuntimeEvent
from tests.conftest import ScriptedModel, reply
from tests.test_conversation import config

TASK = "  # 中文任务 🐍\n\n" + ("- 保留段落和缩进 café\n" * 150) + (
    "\n```python\ndef hello():\n    print('你好')\n\t# tab\n```\n  "
)


async def until(condition: Callable[[], bool]) -> None:
    async with asyncio.timeout(5):
        while not condition():  # noqa: ASYNC110 - observe third-party UI state, no event hook
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("bracketed", [False, True])
@pytest.mark.parametrize("ending", ["\n", "\r\n"])
async def test_multiline_input_never_submits_without_explicit_key(
    bracketed: bool, ending: str,
) -> None:
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        prompt: PromptSession[str] = PromptSession(output=DummyOutput())
        editor = TaskInput(prompt)
        pending = asyncio.create_task(editor.read())
        await until(lambda: prompt.app.is_running)
        data = TASK.replace("\n", ending)
        if bracketed:
            pipe.send_text("\x1b[200~" + data + "\x1b[201~")
        else:
            pipe.send_text("\x1bOQ" + data + "\x1bOQ")  # F2 capture, then finish
        await until(lambda: editor.expand(prompt.default_buffer.text) == TASK)
        assert len(prompt.default_buffer.text) < 100
        assert editor.expand(prompt.default_buffer.text) == TASK
        assert not pending.done()
        # Another rapid paste still belongs to the same draft.
        pipe.send_text("\x1b[200~more\ntext\x1b[201~")
        await until(lambda: editor.expand(prompt.default_buffer.text).endswith("more\ntext"))
        assert not pending.done()
        pipe.send_text("\r")
        assert await asyncio.wait_for(pending, 5) == TASK + "more\ntext"


@pytest.mark.parametrize("os_signal", [False, True])
async def test_cancel_draft_discards_queued_submission_and_can_exit(os_signal: bool) -> None:
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        prompt: PromptSession[str] = PromptSession(output=DummyOutput())
        editor = TaskInput(prompt)
        async def read_until_exit() -> str:
            try:
                return await editor.read()
            except KeyboardInterrupt:
                return "EXIT"

        pending = asyncio.create_task(read_until_exit())
        await until(lambda: prompt.app.is_running)
        pipe.send_text("draft")
        await until(lambda: prompt.default_buffer.text == "draft")
        if os_signal:
            handler = signal.getsignal(signal.SIGINT)
            assert callable(handler)
            handler(signal.SIGINT, None)
        else:
            pipe.send_text("\x03residual\n\x1b\r")
        await until(lambda: prompt.default_buffer.text == "")
        assert not pending.done()
        pipe.send_text("\x03")
        assert await pending == "EXIT"


async def test_run_cancellation_quarantines_residual_input_and_cleans_once() -> None:
    started, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls: list[str] = []

    class Conversation:
        async def turn(self, text: str) -> RunResult:
            calls.append(text)
            if text == "continue":
                return RunResult("completed")
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cleaning.set()
                await release.wait()
                return RunResult("aborted", reason="user_abort")
            raise AssertionError("unreachable")

    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        prompt: PromptSession[str] = PromptSession(output=DummyOutput())
        editor = TaskInput(prompt)
        conversation: Any = Conversation()
        run = asyncio.create_task(cli.drive(conversation, "one", editor=editor))
        await started.wait()
        pipe.send_text("leftover\n\x03")
        await asyncio.wait_for(cleaning.wait(), 5)
        pipe.send_text("\x03\x03another\n\x1b\r")
        await asyncio.sleep(0.05)
        assert not run.done()  # repeated cancellation must not interrupt cleanup
        release.set()
        assert (await run).outcome == "aborted"
        assert run.cancelling() == 0
        pending = asyncio.create_task(editor.read())
        await until(lambda: prompt.app.is_running)
        assert prompt.default_buffer.text == ""
        pipe.send_text("continue\r")
        assert await pending == "continue"
        assert calls == ["one"]
        assert (await cli.drive(conversation, "continue", editor=editor)).outcome == "completed"
        assert calls == ["one", "continue"]

        async def idle_exit() -> bool:
            try:
                await editor.read()
            except KeyboardInterrupt:
                return True
            return False

        idle = asyncio.create_task(idle_exit())
        await until(lambda: prompt.app.is_running)
        pipe.send_text("\x03")
        assert await idle


@pytest.mark.parametrize("exit_keys,reason", [
    ("/exit\r", "slash-exit"), ("\x04", "eof"), ("\x03", "ctrl-c"),
    (None, "eof"),
])
async def test_real_cli_one_task_one_run_replay_and_exit(
    tmp_path: Path, monkeypatch: Any, exit_keys: str | None, reason: str,
) -> None:
    cfg = config(monkeypatch)
    cfg.memory_enabled = False
    output = io.StringIO()
    events: list[RuntimeEvent] = []
    exits: list[str] = []
    instances: list[bootstrap.Conversation] = []

    class Model(ScriptedModel):
        async def close(self) -> None:
            pass

    model = Model([reply(text="Done")])

    class Conversation(bootstrap.Conversation):
        def __init__(self, workspace: Path, config: Any, consumer: Any) -> None:
            async def collect(event: RuntimeEvent) -> None:
                events.append(event)
                await consumer(event)

            super().__init__(workspace, config, collect, home=tmp_path)
            instances.append(self)

        async def summarize_exit(self, value: str) -> None:
            assert signal.getsignal(signal.SIGINT) == signal.SIG_IGN
            exits.append(value)
            await super().summarize_exit(value)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(cli, "read_toml", lambda _: {"model": {"name": "test"}})
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    monkeypatch.setattr(cli, "Console", lambda **_: Console(file=output, width=100))
    monkeypatch.setattr(cli, "Conversation", Conversation)
    monkeypatch.setattr(bootstrap, "ChatModel", lambda _: model)
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        prompt: PromptSession[str] = PromptSession(output=DummyOutput())
        monkeypatch.setattr(cli, "PromptSession", lambda **_: prompt)
        app = asyncio.create_task(cli.application(argparse.Namespace(command=None)))
        await until(lambda: prompt.app.is_running)
        pipe.send_text("\x1b[200~" + TASK + "\x1b[201~")
        await until(lambda: len(prompt.default_buffer.text) == 1)
        assert not model.requests
        assert not any(e.kind in {"run_started", "model_started"} for e in events)
        pipe.send_text("\r")
        await until(lambda: any(e.kind == "run_finished" for e in events))
        await until(lambda: prompt.app.is_running)
        if exit_keys is None:
            pipe.close()
        else:
            pipe.send_text(exit_keys)
        assert await asyncio.wait_for(app, 5) == 0
    assert len(model.requests) == 1
    assert sum(e.kind == "run_started" for e in events) == 1
    user = [e.data for e in events if e.kind == "message" and e.data["role"] == "user"]
    assert len(user) == 1 and user[0]["content"] == TASK
    assert exits == [reason]
    assert output.getvalue().count("Interaction Summary") == 1
    writer = instances[0].writer
    assert writer is not None
    with writer.path.open("rb") as stream:
        records, _ = read_records(stream)
    restored = replay(records, tmp_path)
    assert [m.content for m in restored.messages if m.role == "user"] == [TASK]
    # Existing Session resume consumes exactly the same durable messages.
    resumed = bootstrap.Conversation(tmp_path, cfg, tui.Transcript(), home=tmp_path,
                                     resume=writer.path)
    try:
        assert [m.public() for m in resumed.session.messages] == [
            m.public() for m in restored.messages
        ]
    finally:
        await resumed.close()


def event(kind: str, data: Json, run: str = "r") -> RuntimeEvent:
    return RuntimeEvent(kind, "t", "s", run, data)


async def test_transient_preview_classifies_public_content_once(monkeypatch: Any) -> None:
    monkeypatch.setenv("TERM", "xterm-256color")
    output = io.StringIO()
    console = Console(file=output, force_terminal=True, color_system=None, width=45)
    live: list[Any] = []
    rendered: list[Any] = []
    original_print = console.print

    def capture(*args: Any, **kwargs: Any) -> None:
        rendered.extend(args)
        original_print(*args, **kwargs)

    class Live:
        def __init__(self, text: Any, **kwargs: Any) -> None:
            assert kwargs["transient"]
            if kwargs["auto_refresh"]:
                assert kwargs["refresh_per_second"] == 8
            self.updates = [text]
            self.stopped = False
            live.append(self)

        def start(self, **kwargs: Any) -> None:
            pass

        def update(self, text: Any, **kwargs: Any) -> None:
            self.updates.append(text)

        def stop(self) -> None:
            self.stopped = True

    monkeypatch.setattr(console, "print", capture)
    monkeypatch.setattr(tui, "Live", Live)
    now = [1.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    ui = tui.Transcript(console)
    process = "先检查当前代码。" * 20
    await ui(event("model_started", {}))
    await ui(event("assistant_delta", {"text": process[:10]}))
    now[0] = 1.01
    for char in process[10:]:
        await ui(event("assistant_delta", {"text": char}))
    assert len(live[0].updates) == 2  # one start and one throttled update
    assert output.getvalue() == ""
    await ui(event("model_finished", {"finish_reason": "tool_calls"}))
    await ui(event("message", {"role": "assistant", "content": process, "tool_calls": [
        {"id": "c", "name": "exec_command", "arguments_json": "{}"},
    ]}))
    assert live[0].stopped
    blocks = [obj for obj in rendered if isinstance(obj, Text) and "│" in obj.plain]
    assert len(blocks) == 1 and blocks[0].style == "dim italic"
    assert all(line.startswith("│ ") for line in blocks[0].plain.splitlines())
    await ui(event("tool_started", {"call_id": "c", "name": "exec_command"}))
    from nexus.core.types import ToolResult
    await ui(event("message", ToolResult("c", True, {}).message().public()))
    assert "✓" in output.getvalue()
    markdown = """# 完成

**粗体** 和 *斜体*，`inline`

| 文件 | 状态 |
| --- | --- |
| a.py | OK |

1. 检查
2. 验证

- 中文段落

> 引用

```python
print('你好')
```

```text
src/
  └── app.py
```
"""
    await ui(event("model_started", {}))
    await ui(event("assistant_delta", {"text": markdown}))
    await ui(event("model_finished", {"finish_reason": "stop"}))
    await ui(event("message", {"role": "assistant", "content": markdown}))
    finals = [obj for obj in rendered if isinstance(obj, Markdown)]
    assert len(finals) == 1 and finals[0].markup == markdown
    assert output.getvalue().count("app.py") == 1
    assert ui.preview is None and ui.activity is None and ui.flush_handle is None
    assert all(item.stopped for item in live)


async def test_large_preview_bounded_and_interrupt_restores_terminal(monkeypatch: Any) -> None:
    monkeypatch.setenv("TERM", "xterm-256color")
    output = io.StringIO()
    ui = tui.Transcript(Console(file=output, force_terminal=True, width=40))
    await ui(event("model_started", {}))
    await ui(event("assistant_delta", {"text": "中文" * 100000}))
    assert len(ui.buffer) <= 4000 and len(ui.preview_tail) <= 4000
    await ui(event("run_finished", {
        "outcome": "aborted", "reason": "user_abort", "usage": {}, "duration_ms": 1,
    }))
    assert ui.preview is None and ui.activity is None and ui.flush_handle is None
    assert "Interrupted preview" in tui.terminal_text(output.getvalue())
    assert "aborted" in output.getvalue()


@pytest.mark.parametrize("terminal", [False, True])
async def test_no_color_or_dumb_terminal_and_private_fields(
    monkeypatch: Any, terminal: bool,
) -> None:
    monkeypatch.setenv("TERM", "dumb")
    output = io.StringIO()
    ui = tui.Transcript(Console(file=output, force_terminal=terminal, no_color=True))
    message = {"role": "assistant", "content": "# final unique", "protocol_data": {
        "reasoning_content": "PRIVATE_SENTINEL",
    }}
    original = json.dumps(message)
    await ui(event("model_started", {}))
    await ui(event("assistant_delta", {"text": "# final unique"}))
    await ui(event("model_finished", {"finish_reason": "stop"}))
    await ui(event("message", message))
    assert output.getvalue().count("# final unique") == 1
    assert "PRIVATE_SENTINEL" not in output.getvalue()
    assert "\x1b" not in output.getvalue()
    assert json.dumps(message) == original


async def test_lifetime_summary_partial_usage_unknown_and_once() -> None:
    observed: list[RuntimeEvent] = []

    async def consume(value: RuntimeEvent) -> None:
        observed.append(value)

    summary = InteractionSummary(consume)
    for run in ["one", "two"]:
        await summary(event("run_started", {}, run))
        await summary(event("model_started", {}, run))
    await summary(event("model_finished", {
        "duration_ms": 500, "usage": {"source": "reported", "total_tokens": 42},
    }, "two"))
    await summary(event("tool_started", {"call_id": "c"}))
    await summary(event("tool_finished", {"call_id": "c", "ok": True, "duration_ms": 200}))
    out = io.StringIO()
    summary.render(Console(file=out, width=110))
    summary.render(Console(file=out, width=110))
    text = out.getvalue()
    assert text.count("Interaction Summary") == 1
    assert ">= 42 (partial)" in text and ">= 0.5s (partial)" in text
    assert "100% of finished calls" in text and summary.runs == 2
    assert len(observed) == 7
    empty = InteractionSummary(consume)
    await empty(event("model_started", {}))
    out = io.StringIO()
    empty.render(Console(file=out, width=110))
    assert "N/A" in out.getvalue() and "unknown" in out.getvalue()


async def test_ctrl_c_queued_just_after_submit_still_cancels_run() -> None:
    from prompt_toolkit.input.typeahead import store_typeahead
    from prompt_toolkit.key_binding import KeyPress
    from prompt_toolkit.keys import Keys

    class Conversation:
        async def turn(self, text: str) -> RunResult:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return RunResult("aborted", reason="user_abort")
            raise AssertionError("unreachable")

    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        prompt: PromptSession[str] = PromptSession(output=DummyOutput())
        editor = TaskInput(prompt)
        store_typeahead(pipe, [KeyPress(Keys.ControlC, "\x03")])
        conversation: Any = Conversation()
        result = await asyncio.wait_for(cli.drive(conversation, "task", editor=editor), 5)
        assert result.outcome == "aborted"
