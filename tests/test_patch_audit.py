from __future__ import annotations

import asyncio
import difflib
import hashlib
import io
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from nexus.app.events import Events
from nexus.app.session import SessionError, SessionLog, read_records, replay, resume_session
from nexus.app.tui import Transcript
from nexus.core.agent import append_message, run_turn
from nexus.core.context import Context, ContextBuilder, groups
from nexus.core.runtime_facts import project_facts
from nexus.core.types import ExecutionContext, Json, Limits, RuntimeEvent, Session
from nexus.tools import patch as module
from nexus.tools.patch import apply_patch
from nexus.tools.registry import native_tools
from tests.conftest import Recorder, ScriptedModel, call, reply
from tests.test_patch_v02 import wrapped
from tests.test_session_compaction import SummaryModel, seed, summary_reply


async def ignore(event: RuntimeEvent) -> None:
    pass


async def test_actual_diff_metadata_and_no_model_diff(execution: ExecutionContext) -> None:
    before = b"--old\nkeep\n"
    after = b"++new\nkeep\n"
    (execution.workspace / "a").write_bytes(before)
    (execution.workspace / "gone").write_bytes(b"bye\n")
    events = Recorder()
    result = await apply_patch(
        {
            "patch": wrapped(
                "*** Update File: a\n@@\n---old\n+++new\n"
                "*** Add File: sub/b\n+added\n*** Delete File: gone"
            )
        },
        execution,
        events,
    )
    assert result.ok and result.data["changed_files"] == 3
    assert result.data["created_directories"] == ["sub"]
    assert '"diff"' not in result.message().content
    assert [kind for kind, _ in events.events] == ["patch_manifest"] + [
        "patch_diff", "patch_committed"] * 3
    audits = [(kind, data) for kind, data in events.events if kind == "patch_diff"]
    for index, ((_, event), fact) in enumerate(
        zip(audits, result.data["files"], strict=True)
    ):
        assert event["file_index"] == index + 1
        assert event["call_id"] == execution.call_id
        assert all(event[key] == value for key, value in fact.items())
        assert event["diff_bytes"] == len(event["diff"].encode())
        assert not event["truncated"] and not event["omitted"]
    audit = audits[0][1]
    assert audit["diff"] == "".join(
        difflib.unified_diff(
            before.decode().splitlines(True),
            after.decode().splitlines(True),
            fromfile="a/a",
            tofile="b/a",
        )
    )
    assert audit["added_lines"] == audit["deleted_lines"] == 1
    assert audit["before_hash"] == hashlib.sha256(before).hexdigest()
    assert audit["after_hash"] == hashlib.sha256(after).hexdigest()
    assert audits[1][1]["before_hash"] is None
    assert audits[2][1]["after_hash"] is None
    assert [f["status"] for f in result.data["files"]] == ["modified", "added", "deleted"]


@pytest.mark.parametrize(
    "body,code",
    [
        ("*** Update File: a\n@@\n-old\n+old", None),
        ("*** Update File: a\n@@\n-missing\n+new", "patch_conflict"),
        ("*** Update File: a\n@@\nraw", "invalid_patch"),
    ],
)
async def test_no_audit_without_changes(
    execution: ExecutionContext, body: str, code: str | None
) -> None:
    (execution.workspace / "a").write_bytes(b"old\n")
    events = Recorder()
    result = await apply_patch({"patch": wrapped(body)}, execution, events)
    assert result.error_code == code and not events.events
    assert result.data["changed_files"] == 0 and not result.data["partial"]
    assert result.data["no_changes"] is (code is None)
    if code:
        assert result.data["failed_file"] == "a" and result.data["failed_hunk"] == 1
        assert result.data["detail"]


async def test_diff_caps_and_independent_metadata_budget(
    execution: ExecutionContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(module, "MAX_DIFF_BYTES", 100)
    monkeypatch.setattr(module, "MAX_CALL_DIFF_BYTES", 110)
    execution.output_limit_bytes = 400
    events = Recorder()
    result = await apply_patch(
        {
            "patch": wrapped(
                "\n".join(
                    f"*** Add File: {name}\n+{body}"
                    for name, body in [
                        ("a", "x" * 200),
                        ("b", "中" * 10),
                        ("c", "中" * 10),
                        ("d", "x"),
                    ]
                )
            )
        },
        execution,
        events,
    )
    assert result.ok and result.truncated
    assert result.data["omitted_files"] > 0 and result.data["changed_files"] == 4
    assert len(events.events) == 9
    audits = [data for kind, data in events.events if kind == "patch_diff"]
    assert audits[0]["omitted"] and audits[2]["omitted"]
    assert not audits[1]["omitted"] and not audits[3]["omitted"]
    assert sum(len(e["diff"].encode()) for e in audits) <= 110
    for audit in audits:
        if audit["omitted"]:
            assert audit["diff"] == "" and audit["truncated"]
            assert audit["diff_bytes"] > audit["diff_limit_bytes"]
            assert audit["omission_reason"] == "diff_capacity"
        else:
            assert audit["diff"].endswith("\n")
    assert '"diff"' not in result.message().content


async def test_default_capacity_omits_large_diff(execution: ExecutionContext) -> None:
    events = Recorder()
    result = await apply_patch(
        {"patch": wrapped("*** Add File: large\n+" + "z" * (module.MAX_DIFF_BYTES + 1))},
        execution,
        events,
    )
    assert result.ok and not result.truncated  # model metadata is complete
    assert next(data for kind, data in events.events if kind == "patch_diff")["omitted"]
    assert (
        result.data["files"][0]["after_hash"]
        == hashlib.sha256((execution.workspace / "large").read_bytes()).hexdigest()
    )


async def test_partial_failure_records_only_committed_files(
    execution: ExecutionContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commit = module._commit

    def fail(path: Path, change: module.Change) -> OSError | None:
        if path.name == "b":
            raise PermissionError("injected")
        return commit(path, change)

    monkeypatch.setattr(module, "_commit", fail)
    events = Recorder()
    result = await apply_patch(
        {"patch": wrapped("*** Add File: a\n+yes\n*** Add File: b\n+no")}, execution, events
    )
    assert not result.ok and result.error_code == "patch_io_error"
    assert result.data["partial"] and result.data["changed_files"] == 1
    assert result.data["failed_file"] == "b" and result.data["detail"] == "PermissionError"
    commits = [data for kind, data in events.events if kind == "patch_committed"]
    assert len(commits) == 1 and commits[0]["path"] == "a"
    assert not (execution.workspace / "b").exists()


async def test_cleanup_failure_after_commit_keeps_receipt(
    execution: ExecutionContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unlink = Path.unlink

    def fail(path: Path, *args: Any, **kwargs: Any) -> None:
        if path.name.startswith(".nexus-patch-"):
            raise PermissionError("cleanup failed")
        unlink(path, *args, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(Path, "unlink", fail)
        events = Recorder()
        result = await apply_patch(
            {"patch": wrapped("*** Add File: a\n+yes\n*** Add File: b\n+no")}, execution, events
        )
    assert (execution.workspace / "a").read_bytes() == b"yes\n"
    assert not (execution.workspace / "b").exists()
    assert not result.ok and result.data["partial"] and result.data["changed_files"] == 1
    assert result.data["failed_file"] == "a"
    commits = [data for kind, data in events.events if kind == "patch_committed"]
    assert len(commits) == 1 and commits[0]["path"] == "a"
    for path in execution.workspace.glob(".nexus-patch-*"):
        path.unlink()


async def test_event_order_redaction_replay_context_and_old_resume(tmp_path: Path) -> None:
    session = Session(tmp_path)
    writer = SessionLog.create(session, "patch", {}, tmp_path)
    public: list[RuntimeEvent] = []

    async def consumer(event: RuntimeEvent) -> None:
        public.append(event)

    events = Events(session, writer, consumer, ("private-value",))
    patch = wrapped("*** Add File: a\n+private-value")
    model = ScriptedModel([reply(call("apply_patch", {"patch": patch})), reply(text="done")])
    result = await run_turn(session, "edit", model, native_tools(), events, Limits())
    records, _ = read_records(writer.stream)
    path = writer.path
    writer.close()
    assert result.outcome == "completed" and result.tool_calls == 1
    audit = next(r for r in records if r["kind"] == "patch_diff")
    started = next(r for r in records if r["kind"] == "tool_started")
    finished = next(r for r in records if r["kind"] == "tool_finished")
    tool = next(r for r in records if r["kind"] == "message" and r["data"]["role"] == "tool")
    assert started["seq"] < audit["seq"] < tool["seq"] < finished["seq"]
    assert audit["data"]["call_id"] == started["data"]["call_id"] == finished["data"]["call_id"]
    assert "private-value" not in path.read_text(encoding="utf-8")
    assert "[REDACTED]" in audit["data"]["diff"] and "private-value" not in str(public)
    assert any(e.kind == "patch_diff" for e in public)  # public eval trajectory consumer
    tools = [m for m in model.requests[-1] if m.role == "tool"]
    assert len(tools) == 1 and '"diff"' not in tools[0].content
    original_call = next(m for m in model.requests[-1] if m.tool_calls).tool_calls[0]
    assert json.loads(original_call.arguments_json)["patch"] == patch  # no historical pruning
    restored = replay(records, tmp_path)
    without_audit = replay([r for r in records if r["kind"] != "patch_diff"], tmp_path)
    assert restored.messages == without_audit.messages
    builder = ContextBuilder()
    assert builder.build_active_context(restored) == builder.build_active_context(without_audit)
    groups(restored.messages)
    assert project_facts(restored, restored.messages) == project_facts(
        without_audit, without_audit.messages
    )
    resumed, writer, warnings = resume_session(path, tmp_path, tmp_path)
    writer.close()
    assert not warnings and resumed.messages == restored.messages
    # Reconstruct a genuine legacy result shape, then use the public resume API.
    for record in records:
        if record is tool:
            value = json.loads(record["data"]["content"])
            value["data"]["files"][0]["diff"] = audit["data"]["diff"]
            record["data"]["content"] = json.dumps(value)
    old = tmp_path / "legacy.jsonl"
    legacy = [r for r in records if r["kind"] != "patch_diff"]
    for index, record in enumerate(legacy):
        record["seq"] = index + 1
    old.write_text("".join(json.dumps(r) + "\n" for r in legacy), encoding="utf-8")
    resumed, writer, _ = resume_session(old, tmp_path, tmp_path)
    writer.close()
    groups(builder.build_active_context(resumed))
    assert '"diff"' in next(m.content for m in resumed.messages if m.role == "tool")


@pytest.mark.parametrize("fault", ["cancel", "log_failure", "crash_after_audit"])
async def test_interrupted_audit_does_not_dispatch_next_file(tmp_path: Path, fault: str) -> None:
    session = Session(tmp_path)
    writer = SessionLog.create(session, "fault", {}, tmp_path)
    reached = asyncio.Event()

    async def consumer(event: RuntimeEvent) -> None:
        if event.kind == "patch_committed" and fault == "cancel":
            reached.set()
            await asyncio.Event().wait()

    class FaultLog:
        def append(self, event: RuntimeEvent, protocol_data: Json | None = None) -> int:
            if event.kind == "patch_diff" and fault == "log_failure":
                writer.failed = True
                raise SessionError("injected write failure")
            return writer.append(event, protocol_data)

    events = Events(session, writer, consumer)
    if fault == "log_failure":
        events.writer = FaultLog()  # type: ignore[assignment]
    patch = wrapped("*** Add File: a\n+yes\n*** Add File: b\n+no")
    if fault == "crash_after_audit":
        session.run_id = "crashed"
        await events("run_started", {})
        await append_message(session, reply(call("apply_patch", {"patch": patch})).message, events)
        await events("tool_started", {"call_id": "c1", "name": "apply_patch"})
        # A process can stop between durable audit and result. Do not fabricate a result.
        await apply_patch(
            {"patch": wrapped("*** Add File: a\n+yes")},
            ExecutionContext(tmp_path, "c1", ""),
            events,
        )
    else:
        task = asyncio.create_task(
            run_turn(
                session,
                "edit",
                ScriptedModel(
                    [
                        reply(call("apply_patch", {"patch": patch})),
                        reply(text="done"),
                    ]
                ),
                native_tools(),
                events,
                Limits(),
            )
        )
        if fault == "cancel":
            await asyncio.wait_for(reached.wait(), 5)
            task.cancel()
        result = await task
        assert result.outcome == ("aborted" if fault == "cancel" else "failed")
        if fault == "cancel":
            receipt = json.loads(next(m.content for m in session.messages if m.role == "tool"))
            assert receipt["error_code"] == "cancelled" and receipt["data"]["partial"]
            assert receipt["data"]["changed_files"] == 1
        else:
            assert result.reason == "session_write_failed; recovery incomplete"
    path = writer.path
    writer.close()
    if fault == "log_failure":
        assert not (tmp_path / "a").exists()
    else:
        assert (tmp_path / "a").read_bytes() == b"yes\n"
    assert not (tmp_path / "b").exists()
    restored, writer, _ = resume_session(path, tmp_path, tmp_path)
    records, _ = read_records(writer.stream)
    writer.close()
    groups(restored.messages)
    assert sum(r["kind"] == "patch_diff" for r in records) == (0 if fault == "log_failure" else 1)
    if fault != "cancel":
        assert json.loads(restored.messages[-1].content)["error_code"] == "interrupted_unknown"


async def test_patch_audit_survives_compaction_and_checkpoint(tmp_path: Path) -> None:
    session = Session(tmp_path)
    writer = SessionLog.create(session, "compact", {}, tmp_path)
    events = Events(session, writer, ignore)
    try:
        await run_turn(
            session,
            "edit",
            ScriptedModel(
                [
                    reply(call("apply_patch", {"patch": wrapped("*** Add File: a\n+AUDIT_ONLY")})),
                    reply(text="done"),
                ]
            ),
            native_tools(),
            events,
            Limits(),
        )
        await seed(session, events)
        model = SummaryModel([summary_reply()])
        context = Context(Limits(context_window=70000))
        assert await context.prepare(
            session, ContextBuilder().build_active_context(session), [], model, events
        )
        assert session.checkpoint is not None
        assert "patch_diff" not in str([m.public() for m in model.requests[0]])
        records, _ = read_records(writer.stream)
        restored = replay(records, tmp_path)
        assert [asdict(m) for m in ContextBuilder().build_active_context(restored)] == [
            asdict(m) for m in ContextBuilder().build_active_context(session)
        ]
        assert sum(r["kind"] == "patch_diff" for r in records) == 1
    finally:
        writer.close()


async def test_tui_event_previews_once_bounded_and_partial_status(tmp_path: Path) -> None:
    output = io.StringIO()
    transcript = Transcript(Console(file=output, color_system=None, width=60))
    session = Session(tmp_path)
    writer = SessionLog.create(session, "display", {}, tmp_path)
    events = Events(session, writer, transcript)
    try:
        patch = wrapped("\n".join(f"*** Add File: f{i}\n+unique\n+" + "z" * 2000 for i in range(8)))
        await run_turn(
            session,
            "edit",
            ScriptedModel(
                [
                    reply(call("apply_patch", {"patch": patch})),
                    reply(text="done"),
                ]
            ),
            native_tools(),
            events,
            Limits(),
        )
    finally:
        writer.close()
    rendered = output.getvalue()
    assert rendered.count("+unique") == 3
    assert rendered.count("Added f0") == 1 and "f3" not in rendered
    assert "5 more changed files" in rendered and "Apply patch" in rendered and "✓" in rendered
    assert len(rendered) < 1400
