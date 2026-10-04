from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from nexus.app.events import Events, redact
from nexus.app.session import (
    SessionError,
    SessionLog,
    list_sessions,
    read_records,
    replay,
    resume_session,
    session_directory,
)
from nexus.core.agent import append_message, run_turn
from nexus.core.context import ContextBuilder, estimate, groups, instructions
from nexus.core.types import Limits, Message, RuntimeEvent, Session, ToolResult, ToolSpec
from nexus.tools.registry import native_tools
from tests.conftest import Recorder, ScriptedModel, call, reply


async def ignore(event: RuntimeEvent) -> None:
    pass


async def test_terminal_fallback_keeps_tool_output_hidden(tmp_path: Path, capsys: Any) -> None:
    async def broken(event: RuntimeEvent) -> None:
        raise RuntimeError("broken terminal")

    session = Session(tmp_path)
    writer = SessionLog.create(session, "task", {"name": "test"}, tmp_path)
    events = Events(session, writer, broken)
    try:
        await events("tool_output_delta", {"call_id": "c", "text": "RAW_FILE_BODY" * 1000})
        await events("tool_finished", {"ok": False, "error_code": "command_timeout"})
        await events("assistant_delta", {"text": "Final explanation"})
    finally:
        writer.close()
    rendered = capsys.readouterr()
    assert "RAW_FILE_BODY" not in rendered.out + rendered.err
    assert "command_timeout" in rendered.err
    assert "Final explanation" in rendered.out


async def test_jsonl_private_public_split_redaction_and_resume(tmp_path: Path) -> None:
    session = Session(tmp_path)
    writer = SessionLog.create(session, "a task", {"name": "test"}, tmp_path)
    public: list[RuntimeEvent] = []

    async def consumer(event: RuntimeEvent) -> None:
        public.append(event)

    events = Events(session, writer, consumer, ("secret-key",))
    message = Message("assistant", "value=secret-key", protocol_data={"private": "required"})
    await events("assistant_delta", {"text": "value=secr"})
    await events("assistant_delta", {"text": "et-key"})
    await append_message(session, message, events)
    path = writer.path
    writer.close()
    raw = path.read_text(encoding="utf-8")
    assert "secret-key" not in raw and "assistant_delta" not in raw
    assert "protocol_data" in raw
    assert "protocol_data" not in str(public) and "required" not in str(public)
    assert "secret-key" not in str(public)
    restored, writer, warnings = resume_session(path, tmp_path, tmp_path)
    try:
        assert restored.messages[0].protocol_data == {"private": "required"}
        assert "[REDACTED]" in restored.messages[0].content
    finally:
        writer.close()
    assert session_directory(tmp_path) != session_directory(tmp_path / "other")
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("tool_started", [False, True])
async def test_crash_missing_result_is_unknown_never_replayed(
    tmp_path: Path, tool_started: bool
) -> None:
    session = Session(tmp_path)
    writer = SessionLog.create(session, "crash", {}, tmp_path)
    events = Events(session, writer, ignore)
    await append_message(session, Message("user", "task"), events)
    await append_message(
        session, reply(call("exec_command", {"command": "do not replay"})).message, events
    )
    if tool_started:
        await events("tool_started", {"call_id": "c1", "name": "exec_command"})
    path = writer.path
    writer.close()
    restored, writer, warnings = resume_session(path, tmp_path, tmp_path)
    try:
        assert json.loads(restored.messages[-1].content)["error_code"] == "interrupted_unknown"
        assert warnings
        assert len(groups(restored.messages)) == 2
    finally:
        writer.close()


async def test_truncated_tail_new_file_preserves_original(tmp_path: Path) -> None:
    session = Session(tmp_path)
    writer = SessionLog.create(session, "recover", {}, tmp_path)
    await append_message(session, Message("user", "retained"), Events(session, writer, ignore))
    path = writer.path
    writer.close()
    with path.open("ab") as stream:
        stream.write(b'{"schema_version":')
    before = path.read_bytes()
    restored, writer, warnings = resume_session(path, tmp_path, tmp_path)
    try:
        assert writer.path != path and restored.messages[0].content == "retained"
        assert path.read_bytes() == before and warnings
        records, truncated = read_records(writer.stream)
        assert not truncated
        assert records[0]["data"]["recovered_from"] == session.session_id
    finally:
        writer.close()


@pytest.mark.parametrize("fault", ["middle", "seq", "version"])
async def test_corrupt_session_rejected(tmp_path: Path, fault: str) -> None:
    session = Session(tmp_path)
    writer = SessionLog.create(session, "broken", {}, tmp_path)
    events = Events(session, writer, ignore)
    await append_message(session, Message("user", "one"), events)
    await append_message(session, Message("assistant", "two"), events)
    path = writer.path
    writer.close()
    lines = path.read_bytes().splitlines(keepends=True)
    if fault == "middle":
        lines[1] = b"INVALID\n"
    else:
        item = json.loads(lines[1])
        item["seq" if fault == "seq" else "schema_version"] = 99
        lines[1] = (json.dumps(item) + "\n").encode()
    path.write_bytes(b"".join(lines))
    with pytest.raises(SessionError):
        resume_session(path, tmp_path, tmp_path)


def test_single_writer_and_workspace_isolation(tmp_path: Path) -> None:
    session = Session(tmp_path)
    writer = SessionLog.create(session, "locked", {}, tmp_path)
    try:
        with pytest.raises(SessionError):
            SessionLog(writer.path)
    finally:
        writer.close()
    with pytest.raises(SessionError, match="another workspace"):
        resume_session(writer.path, tmp_path / "other", tmp_path)
    assert list_sessions(tmp_path, tmp_path)[0]["title"] == "locked"


async def test_flush_failure_stops_next_side_effect(tmp_path: Path, monkeypatch: Any) -> None:
    session = Session(tmp_path)
    writer = SessionLog.create(session, "disk error", {}, tmp_path)
    events = Events(session, writer, ignore)
    original = writer.append

    def fail(event: RuntimeEvent, protocol_data: Any = None) -> int:
        if event.kind == "tool_started":
            writer.failed = True
            raise SessionError("simulated disk error")
        return original(event, protocol_data)

    monkeypatch.setattr(writer, "append", fail)
    model = ScriptedModel(
        [
            reply(
                call(
                    "apply_patch",
                    {"patch": "--- /dev/null\n+++ b/should-not-exist\n@@ -0,0 +1 @@\n+x\n"},
                )
            )
        ]
    )
    try:
        result = await run_turn(session, "test", model, native_tools(), events, Limits())
        assert result.outcome == "failed"
        assert not (tmp_path / "should-not-exist").exists()
    finally:
        writer.close()


async def test_tui_failure_is_optional(tmp_path: Path) -> None:
    session = Session(tmp_path)
    writer = SessionLog.create(session, "tui failure", {}, tmp_path)

    async def broken(event: RuntimeEvent) -> None:
        raise ValueError("render failure")

    try:
        result = await run_turn(
            session,
            "test",
            ScriptedModel([reply(text="done")]),
            {},
            Events(session, writer, broken),
            Limits(),
        )
        assert result.outcome == "completed"
    finally:
        writer.close()


async def test_legacy_compaction_restores_originals_without_cross_run_summary(
    tmp_path: Path,
) -> None:
    session = Session(tmp_path, run_id="old-run")
    writer = SessionLog.create(session, "compact", {}, tmp_path)
    events = Events(session, writer, ignore)
    await events("run_started", {"workspace": str(tmp_path)})
    for message in [
        Message("system", "root instructions"),
        Message("user", "old task"),
        reply(call("exec_command", {}, "old-call")).message,
        ToolResult("old-call", True, {"stdout": "large " * 700}).message(),
        Message("assistant", "previous final"),
    ]:
        await append_message(session, message, events)
    await events("run_finished", {"outcome": "completed"})
    session.run_id = "current-run"
    await events("run_started", {"workspace": str(tmp_path)})
    await append_message(session, Message("user", "current task"), events)
    await events(
        "context_compacted",
        {
            "summary": "Old task plus current task mixed in legacy summary",
            "summary_index": 1,
            "kept_seqs": [session.messages[0].seq, session.messages[-1].seq],
            "replaced_seqs": [m.seq for m in session.messages[1:-1]],
        },
    )
    try:
        records, _ = read_records(writer.stream)
        restored = replay(records, tmp_path)
        assert [(m.public(), m.seq) for m in restored.messages] == [
            (m.public(), m.seq) for m in session.messages
        ]
        assert [
            m.content for m in ContextBuilder().build_active_context(restored, "current-run")
        ] == [
            "root instructions",
            "current task",
        ]
        groups(restored.messages)
        assert any(r["kind"] == "context_compacted" for r in records)
    finally:
        writer.close()


async def test_protected_context_overflow_no_model_request(tmp_path: Path) -> None:
    session = Session(tmp_path)
    model = ScriptedModel([])
    result = await run_turn(
        session,
        "huge " * 10000,
        model,
        {},
        Recorder(),
        Limits(context_window=4000, max_output_tokens=1000),
    )
    assert result.outcome == "limited" and not model.requests


def test_missing_optional_secret_does_not_corrupt_text() -> None:
    assert redact("task KEY", ("", "KEY")) == "task [REDACTED]"


async def test_safe_short_delta_is_not_delayed(tmp_path: Path) -> None:
    session = Session(tmp_path)
    writer = SessionLog.create(session, "stream", {}, tmp_path)
    seen: list[str] = []

    async def consumer(event: RuntimeEvent) -> None:
        seen.append(event.data["text"])

    try:
        events = Events(session, writer, consumer, ("VERY_LONG_SECRET_KEY",))
        await events("assistant_delta", {"text": "ready\n"})
        assert seen == ["ready\n"]
    finally:
        writer.close()


def test_instructions_include_convergence_policy(tmp_path: Path) -> None:
    text = instructions(tmp_path, "shell")
    for principle in (
        "requested outcome as the global objective",
        "form or discriminate actionable hypotheses",
        "prefer the lowest-cost experiment",
        "make the smallest useful edit and test it",
        "Editing and testing are part of investigation and do not require certainty",
        "If evidence is still insufficient, continue investigating",
        "directly tied to choosing or validating a fix",
        "receiving decisive evidence, re-evaluate the original task",
        "change the experiment rather than repeatedly refining the same uninformative path",
        "unless that information is necessary to choose or validate the fix",
    ):
        assert principle in text


def test_only_root_agents_and_schema_protocol_budget(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("root rules", encoding="utf-8")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested/AGENTS.md").write_text("nested rules", encoding="utf-8")
    text = instructions(tmp_path, "shell")
    assert "root rules" in text and "nested rules" not in text
    messages = [Message("assistant", "small")]
    baseline = estimate(messages, [])
    assert estimate(messages, [ToolSpec("x", "large " * 500, {})]) > baseline
    messages[0].protocol_data = {"private": "large " * 500}
    assert estimate(messages, []) > baseline
    (tmp_path / "AGENTS.md").write_bytes(b"x" * 65537)
    with pytest.raises(ValueError, match="64 KiB"):
        instructions(tmp_path, "shell")


@pytest.mark.parametrize("side_effect", [False, True])
def test_process_death_releases_lock_and_recovers_unknown(
    tmp_path: Path, side_effect: bool
) -> None:
    code = """
import os,sys
from pathlib import Path
from nexus.app.session import SessionLog,now
from nexus.core.types import Session,Message,ToolCall,RuntimeEvent
root=Path(sys.argv[1]); s=Session(root); w=SessionLog.create(s,'crash',{},root)
m=Message('assistant',tool_calls=[ToolCall('c1','exec_command','{}')])
w.append(RuntimeEvent('message',now(),s.session_id,None,m.public()))
if sys.argv[2]=='True': (root/'effect').write_text('happened')
os._exit(23)
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path), str(side_effect)], check=False
    )
    assert result.returncode == 23
    path = next(session_directory(tmp_path, tmp_path).glob("*.jsonl"))
    restored, writer, _ = resume_session(path, tmp_path, tmp_path)
    try:
        assert json.loads(restored.messages[-1].content)["error_code"] == "interrupted_unknown"
        assert (tmp_path / "effect").exists() == side_effect
    finally:
        writer.close()
