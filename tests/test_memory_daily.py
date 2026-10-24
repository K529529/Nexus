from __future__ import annotations

import argparse
import asyncio
import io
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from prompt_toolkit import PromptSession
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from nexus.app import bootstrap, cli, memory_daily
from nexus.app.memory import MemoryStore, entries
from nexus.app.session import read_records, replay
from nexus.app.tui import Transcript
from nexus.core.agent import append_message
from nexus.core.context import Context, ContextBuilder
from nexus.core.tokens import TokenEstimator
from nexus.core.types import Limits, Message, ModelError, Session, Usage
from tests.conftest import Recorder, reply
from tests.test_conversation import config, ignore
from tests.test_memory_conversation import Main
from tests.test_session_compaction import SummaryModel, seed, summary_reply

SUMMARY = (
    "### Decisions\n- Keep Markdown as authority.\n"
    "### Lessons Learned\n- Preserve failed attempts.\n"
    "### Notes\nNone.\n### Follow-ups\n- Run dogfooding."
)
NOW = datetime.fromisoformat("2026-10-24T18:00:00+08:00")


def test_pi_handoff_format_tail_and_no_nested_ids(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    assert memory_daily.handoff(store, "sid", NOW) == ""
    done = store.scratchpad("add", "completed")
    store.scratchpad("complete", ident=done["id"], revision=done["revision"])
    assert memory_daily.handoff(store, "sid", NOW) == ""
    store.scratchpad("add", "pending")
    name = memory_daily.daily_name(NOW)
    store.write(name, "\n".join(f"line {i}" for i in range(20)))
    original = store.read(name)
    text = memory_daily.handoff(store, "sid", NOW)
    assert text.startswith("<!-- HANDOFF 2026-10-24T18:00:00+08:00 [sid] -->\n## Session Handoff")
    assert "- [ ] pending" in text and "completed" not in text
    assert "line 4\n" not in text and "line 5\n" in text and text.endswith("line 19")
    store.write(name, text)
    assert store.read(name).startswith(original) and len(entries(store.read(name))) == 2
    store.write(name, memory_daily.handoff(store, "sid", NOW))
    assert len(entries(store.read(name))) == 3  # Pi appends; no invented deduplication.


@pytest.mark.parametrize("automatic", [False, True])
async def test_actual_compactions_handoff_refresh_and_resume(
    tmp_path: Path, monkeypatch: Any, automatic: bool
) -> None:
    cfg = config(monkeypatch)
    cfg.limits.context_window = 70000
    convo = bootstrap.Conversation(tmp_path, cfg, ignore, home=tmp_path)
    assert convo.memory and convo.memory_index
    store = convo.memory
    store.scratchpad("add", "FOLLOW_UP_SENTINEL")
    convo.ensure_events("daily test")
    assert convo.events and convo.writer
    prefix = await convo.memory_prefix()
    await append_message(convo.session, Message("system", "base" + prefix), convo.events)
    store.write("MEMORY.md", "NEW_DISK_FACT")
    before = convo.session.memory_snapshot
    assert not await convo.compact(force=False)
    assert convo.session.memory_snapshot == before
    assert not list((store.root / "daily").glob("*.md"))
    compactor = SummaryModel([summary_reply(), summary_reply()])
    convo.compaction_model = compactor  # type: ignore[assignment]
    try:
        await seed(convo.session, convo.events)
        if automatic:
            assert await convo.context.prepare(
                convo.session,
                ContextBuilder().build_active_context(convo.session),
                [],
                compactor,
                convo.events,
            )
        else:
            assert await convo.compact()
        assert convo.session.checkpoint
        name = memory_daily.daily_name(datetime.now().astimezone())
        assert "## Session Handoff" in store.read(name)
        assert "FOLLOW_UP_SENTINEL" in store.read(name)
        assert convo.memory_index.dirty
        assert convo.session.memory_snapshot is not None
        assert "NEW_DISK_FACT" in convo.session.memory_snapshot["content"]
        assert "## Session Handoff" in convo.session.memory_snapshot["content"]
        assert "NEW_DISK_FACT" not in str(compactor.requests)  # Snapshot is not compactor evidence.
        await seed(convo.session, convo.events, 32, 32)
        await convo.compact()
        assert len(entries(store.read(name))) == 2
        records, _ = read_records(convo.writer.stream)
        restored = replay(records, tmp_path)
        assert restored.memory_snapshot == convo.session.memory_snapshot
        assert ContextBuilder().build_active_context(
            restored
        ) == ContextBuilder().build_active_context(convo.session)
    finally:
        await convo.close()


async def test_empty_handoff_still_refreshes_snapshot(tmp_path: Path, monkeypatch: Any) -> None:
    convo = bootstrap.Conversation(tmp_path, config(monkeypatch), ignore, home=tmp_path)
    assert convo.memory
    convo.ensure_events("snapshot")
    await convo.memory_prefix()
    convo.memory.write("MEMORY.md", "new fact")
    await convo.before_compact()
    assert convo.session.memory_snapshot is not None
    assert "new fact" in convo.session.memory_snapshot["content"]
    assert not (convo.memory.root / "daily").exists()
    await convo.close()


@pytest.mark.parametrize("profile", ["fallback", "deepseek", "qwen"])
def test_exit_whole_request_token_budget_and_recent_tail(tmp_path: Path, profile: str) -> None:
    estimator = {
        "fallback": TokenEstimator(),
        "deepseek": TokenEstimator("deepseek-flash", "https://api.deepseek.com"),
        "qwen": TokenEstimator("Qwen/Qwen3.5-35B-A3B", "https://api.siliconflow.cn/v1"),
    }[profile]
    session = Session(
        tmp_path,
        messages=[
            Message("system", "PRIVATE_SYSTEM"),
            Message("user", "OLD_SENTINEL " + "旧任务 " * 3000),
            Message("assistant", "observed " * 3000, protocol_data={"private": "SECRET_REASONING"}),
            Message("user", "新任务 " * 3000),
            Message("assistant", "NEWEST_SENTINEL"),
        ],
    )
    prepared = memory_daily.exit_request(session, estimator, 6000)
    assert prepared
    request, budget = prepared
    assert budget == 1200 and estimator.count(request, []) <= budget
    assert "NEWEST_SENTINEL" in request[-1].content
    assert "OLD_SENTINEL" not in request[-1].content
    assert "PRIVATE_SYSTEM" not in str(request) and "SECRET_REASONING" not in str(request)
    assert not any(m.tool_calls or m.protocol_data for m in request)
    assert memory_daily.exit_request(session, estimator, 100) is None


@pytest.mark.parametrize(
    "text",
    ["", "None.", "unstructured", "\n".join(f"### {h}\nNone." for h in memory_daily.HEADINGS)],
)
def test_invalid_and_empty_summary(text: str) -> None:
    assert not memory_daily.valid_summary(text)
    assert memory_daily.valid_summary(SUMMARY)


async def seed_short(convo: bootstrap.Conversation) -> None:
    convo.ensure_events("exit test")
    assert convo.events
    for role, text in [
        ("user", "keep Markdown"),
        ("assistant", "agreed"),
        ("user", "preserve failures"),
        ("assistant", "dogfood pending"),
    ]:
        await append_message(convo.session, Message(role, text), convo.events)  # type: ignore[arg-type]


async def test_exit_main_model_once_preserves_daily_notifies_index(
    tmp_path: Path, monkeypatch: Any
) -> None:
    cfg = config(monkeypatch)
    convo = bootstrap.Conversation(tmp_path, cfg, ignore, home=tmp_path)
    assert convo.memory and convo.memory_index
    name = memory_daily.daily_name(datetime.now().astimezone())
    convo.memory.write(name, "EXISTING_NOTE")
    previous = convo.memory.read(name)
    model_reply = reply(text=SUMMARY)
    model_reply.usage = Usage(600, 80, 680, "reported")
    model = Main([model_reply])
    captured: list[Any] = []

    def factory(value: Any) -> Main:
        captured.append(value)
        return model

    monkeypatch.setattr(bootstrap, "ChatModel", factory)
    await seed_short(convo)
    await convo.summarize_exit("slash-exit")
    await convo.summarize_exit("eof")
    await convo.close()
    await convo.summarize_exit("slash-exit")
    assert len(model.requests) == 1 and captured[0].name == cfg.model.name
    assert captured[0].max_output_tokens == 2048 and cfg.model.max_output_tokens == 8192
    assert captured[0].request_timeout_seconds == 10
    assert convo.memory.read(name).startswith(previous)
    assert SUMMARY in convo.memory.read(name) and convo.session.session_id in convo.memory.read(
        name
    )
    assert convo.memory_index.dirty and convo.memory.read(".dirty")
    assert convo.writer
    with convo.writer.path.open("rb") as stream:
        records, _ = read_records(stream)
    written = [
        r["data"]
        for r in records
        if r["kind"] == "memory_daily" and r["data"]["status"] == "written"
    ]
    assert len(written) == 1 and written[0]["usage"]["input_tokens"] == 600
    fresh = bootstrap.Conversation(tmp_path, cfg, ignore, home=tmp_path)
    fresh.ensure_events("fresh")
    assert "Keep Markdown" in await fresh.memory_prefix()
    await fresh.close()


@pytest.mark.parametrize("case", ["short", "off", "eval", "new", "resume", "close"])
async def test_skips_without_model(tmp_path: Path, monkeypatch: Any, case: str) -> None:
    cfg = config(monkeypatch)
    cfg.memory_enabled = case != "off"
    convo = bootstrap.Conversation(
        tmp_path, cfg, ignore, home=tmp_path, registry={} if case == "eval" else None
    )
    if case != "short":
        await seed_short(convo)
    monkeypatch.setattr(bootstrap, "ChatModel", lambda _: pytest.fail("No exit request expected"))
    if case == "close":
        await convo.close()
    else:
        await convo.summarize_exit(case if case in {"new", "resume"} else "eof")
    await convo.close()
    assert not (tmp_path / "memory" / "daily").exists()


@pytest.mark.parametrize("case", ["timeout", "api", "empty", "none", "length", "disk"])
async def test_exit_failures_never_block_or_write(
    tmp_path: Path, monkeypatch: Any, case: str
) -> None:
    convo = bootstrap.Conversation(tmp_path, config(monkeypatch), ignore, home=tmp_path)
    await seed_short(convo)
    closed: list[bool] = []

    class Model:
        async def complete(self, messages: Any, tools: Any, emit: Any) -> Any:
            assert tools == []
            if case == "timeout":
                await asyncio.sleep(10)
            if case == "api":
                raise ModelError("model_transport")
            text = (
                ""
                if case == "empty"
                else "\n".join(f"### {h}\nNone." for h in memory_daily.HEADINGS)
                if case == "none"
                else SUMMARY
            )
            result = reply(text=text)
            if case == "length":
                result.finish_reason = "length"
            return result

        async def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(bootstrap, "ChatModel", lambda _: Model())
    if case == "timeout":
        monkeypatch.setattr(memory_daily, "EXIT_TIMEOUT", 0.01)
    if case == "disk":
        monkeypatch.setattr(
            MemoryStore, "write", lambda *a: (_ for _ in ()).throw(OSError("disk full"))
        )
    await convo.summarize_exit("eof")
    await convo.close()
    assert closed and not (tmp_path / "memory" / "daily").exists()


@pytest.mark.parametrize("end", ["slash-exit", "eof"])
async def test_cli_terminal_stops_before_exit_summary(
    tmp_path: Path, monkeypatch: Any, end: str
) -> None:
    cfg = config(monkeypatch)
    order: list[str] = []
    answers = iter(["/new", "/resume", "/exit"])

    class Prompt:
        def __init__(self, **kwargs: Any) -> None:
            from types import SimpleNamespace

            from prompt_toolkit.input import DummyInput

            self.app = SimpleNamespace(input=DummyInput())

        async def prompt_async(self, text: str, **kwargs: Any) -> str:
            value = next(answers)
            if value == "/exit" and end == "eof":
                raise EOFError
            return value

    async def summarize(self: Any, reason: str) -> None:
        assert order[-1] == "stop"
        order.append(reason)

    async def select(_: Any) -> None:
        return None

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(cli, "read_toml", lambda _: {"model": {"name": "test"}})
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    monkeypatch.setattr(cli, "PromptSession", Prompt)
    monkeypatch.setattr(cli, "select_session", select)
    monkeypatch.setattr(cli, "Console", lambda **_: Console(file=io.StringIO()))
    monkeypatch.setattr(Transcript, "stop_activity", lambda self: order.append("stop"))
    monkeypatch.setattr(bootstrap.Conversation, "summarize_exit", summarize)
    with create_app_session(output=DummyOutput()):
        assert await cli.application(argparse.Namespace(command=None)) == 0
    assert order == ["stop", end]


@pytest.mark.parametrize("maximum", [8192, 32768])
async def test_managed_window_trigger_independent_of_output(tmp_path: Path, maximum: int) -> None:
    context = Context(Limits(context_window=128000, max_output_tokens=maximum))
    assert context.trigger == 115200 and context.budget == 128000
    session = Session(tmp_path, messages=[Message("user", "task")])
    events = Recorder()
    context.tokens = lambda *args: 115199  # type: ignore[method-assign]
    context.check(session.messages, [])
    assert not await context.prepare(session, session.messages, [], None, events)
    assert not events.events
    context.tokens = lambda *args: 115200  # type: ignore[method-assign]
    assert await context.prepare(session, session.messages, [], None, events)
    assert any(kind == "compaction_failed" for kind, _ in events.events)
    context.tokens = lambda *args: 128001  # type: ignore[method-assign]
    with pytest.raises(ModelError, match="context_limit"):
        context.check(session.messages, [])


async def test_real_estimator_boundary_compacts_at_115200(tmp_path: Path, monkeypatch: Any) -> None:
    cfg = config(monkeypatch)
    cfg.memory_enabled = False
    cfg.limits.context_window = 128000
    cfg.limits.max_output_tokens = 32768
    convo = bootstrap.Conversation(tmp_path, cfg, ignore, home=tmp_path)
    convo.ensure_events("budget boundary")
    assert convo.events and convo.writer
    try:
        await seed(convo.session, convo.events, 32)
        # Add only complete groups; find exact boundary using the existing estimator.
        latest = convo.session.messages[-1]
        active = ContextBuilder().build_active_context(convo.session)
        for target in [115199, 115200]:
            low, high = 0, 200000
            while low < high:
                size = (low + high) // 2
                latest.content = "x" * size
                if (
                    convo.context.tokens(convo.context.with_state(convo.session, active), [])
                    < target
                ):
                    low = size + 1
                else:
                    high = size
            latest.content = "x" * low
            assert (
                convo.context.tokens(convo.context.with_state(convo.session, active), []) == target
            )
            convo.context.check(convo.context.with_state(convo.session, active), [])
            model = SummaryModel([summary_reply()])
            compacted = await convo.context.prepare(convo.session, active, [], model, convo.events)
            assert compacted is (target == 115200)
            assert bool(model.requests) is (target == 115200)
        assert convo.session.checkpoint
    finally:
        await convo.close()


@pytest.mark.parametrize("keys,reason", [("/exit\x1b\r", "slash-exit"), ("\x04", "eof")])
async def test_real_prompt_is_stopped_before_summary(
    tmp_path: Path, monkeypatch: Any, keys: str, reason: str
) -> None:
    cfg = config(monkeypatch)
    seen: list[str] = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(cli, "read_toml", lambda _: {"model": {"name": "test"}})
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    monkeypatch.setattr(cli, "Console", lambda **_: Console(file=io.StringIO()))

    async def start(self: Any) -> None:
        pass

    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        prompt: PromptSession[str] = PromptSession(output=DummyOutput())

        async def summarize(self: Any, actual_reason: str) -> None:
            assert not prompt.app.is_running
            seen.append(actual_reason)

        monkeypatch.setattr(cli, "PromptSession", lambda **_: prompt)
        monkeypatch.setattr(bootstrap.Conversation, "start_memory", start)
        monkeypatch.setattr(bootstrap.Conversation, "summarize_exit", summarize)
        pipe.send_text(keys)
        assert await cli.application(argparse.Namespace(command=None)) == 0
    assert seen == [reason]
