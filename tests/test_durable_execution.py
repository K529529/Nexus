from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

from nexus.app.config import default_shell
from nexus.app.events import Events
from nexus.app.session import SessionError, SessionLog, read_records, resume_session
from nexus.core.agent import append_message, run_turn
from nexus.core.durable import prepared
from nexus.core.security import ApprovalDecision, ApprovalRequest, SecurityGateway, SecurityPolicy
from nexus.core.types import (
    ExecutionContext,
    Limits,
    RuntimeEvent,
    Session,
    ToolResult,
)
from nexus.tools.plan import create_update_plan_tool
from nexus.tools.registry import native_tools
from tests.conftest import Recorder, ScriptedModel, call, python_command, reply


async def ignore(event: RuntimeEvent) -> None:
    pass


# These are abrupt process exits, bypassing Python finally/agent cancellation.
# The shell really appends an fsynced byte; repeating it is observably non-idempotent.
CRASH_SCRIPT = r"""
import asyncio, os, sys
from pathlib import Path
from nexus.app.config import default_shell
from nexus.app.events import Events
from nexus.app.session import SessionLog
from nexus.core.agent import run_turn
from nexus.core.types import Limits, Session
from nexus.tools.registry import native_tools
from tests.conftest import ScriptedModel, call, python_command, reply

root, window = Path(sys.argv[1]), sys.argv[2]
session = Session(root, session_id="crash")
writer = SessionLog.create(session, "crash windows", {}, root)
async def ignore(event): pass
events = Events(session, writer, ignore)
async def emit(kind, data, **kw):
    if window == "result_not_saved" and kind == "tool_checkpoint" and "result" in data:
        os._exit(77)
    seq = await events(kind, data, **kw)
    if kind == "tool_checkpoint" and data.get("phase") == window:
        os._exit(77)
    if window == "running" and kind == "tool_output_delta":
        os._exit(77)
    if window == kind and kind in {"patch_manifest", "patch_diff", "patch_committed"}:
        os._exit(77)
    return seq
code = ("import os; f=open('counter','ab'); f.write(b'x'); f.flush(); "
        "os.fsync(f.fileno()); f.close(); print('effect',flush=True)")
tool = call("exec_command", {"command": python_command(code)})
if window.startswith("patch_"):
    tool = call("apply_patch", {"patch": "*** Begin Patch\n*** Add File: a\n+one\n"
                                      "*** Add File: b\n+two\n*** End Patch"})
if window == "patch_after_write":
    from nexus.tools import patch
    commit = patch._commit
    def stop_after_write(path, change):
        commit(path, change)
        os._exit(77)
    patch._commit = stop_after_write
asyncio.run(run_turn(session, "work", ScriptedModel([reply(tool), reply(text="done")]),
                     native_tools(), emit, Limits(shell=default_shell())))
raise RuntimeError("Crash boundary not reached")
"""


def crash(tmp_path: Path, window: str) -> Path:
    result = subprocess.run(
        [sys.executable, "-c", CRASH_SCRIPT, str(tmp_path), window],
        capture_output=True,
        timeout=25,
        check=False,
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join(
                [
                    str(Path(__file__).resolve().parents[1] / "src"),
                    str(Path(__file__).resolve().parents[1]),
                ]
            ),
        },
    )
    assert result.returncode == 77, result.stderr.decode(errors="replace")
    return next((tmp_path / "sessions").glob("*/crash.jsonl"))


@pytest.mark.parametrize(
    "window",
    [
        "PREPARED",
        "DISPATCHING",
        "running",
        "result_not_saved",
        "COMPLETED",
    ],
)
@pytest.mark.parametrize("torn_tail", [False, True])
async def test_process_death_shell_never_replays_unknown(
    tmp_path: Path,
    window: str,
    torn_tail: bool,
) -> None:
    path = crash(tmp_path, window)
    if torn_tail:
        with path.open("ab") as stream:
            stream.write(b'{"schema_version":1,"unfinished":')
    original = path.read_bytes()
    session, writer, _ = resume_session(path, tmp_path, tmp_path)
    assert bool(session.pending_tools) == (window == "PREPARED")
    before = (tmp_path / "counter").read_bytes() if (tmp_path / "counter").exists() else b""
    model = ScriptedModel([reply(text="Inspected recovery.")])
    try:
        result = await run_turn(
            session,
            "continue",
            model,
            native_tools(),
            Events(session, writer, ignore),
            Limits(shell=default_shell()),
        )
        assert result.outcome == "completed", result
        records, truncated = read_records(writer.stream)
        assert not truncated
        restored_path = writer.path
        saved = next(json.loads(m.content) for m in session.messages if m.role == "tool")
        if window in {"PREPARED", "COMPLETED"}:
            assert saved["ok"] and not session.unresolved_effects
        else:
            assert saved["error_code"] == "interrupted_unknown" and session.unresolved_effects
        if window == "COMPLETED":
            complete = next(
                r["data"]["result"]
                for r in records
                if r["kind"] == "tool_checkpoint" and "result" in r["data"]
            )
            assert saved == complete
        assert len([m for m in model.requests[0] if m.role == "tool"]) == 1
        if torn_tail:
            assert original == path.read_bytes() and restored_path != path
    finally:
        writer.close()
    after = (tmp_path / "counter").read_bytes() if (tmp_path / "counter").exists() else b""
    assert after == (b"x" if window == "PREPARED" else before)
    assert after == (b"" if window == "DISPATCHING" else b"x")
    again, writer, _ = resume_session(restored_path, tmp_path, tmp_path)
    writer.close()
    assert not again.pending_tools and again.unresolved_effects == session.unresolved_effects
    if torn_tail:
        # Selecting the preserved damaged source must reach the same completed copy.
        source_again, writer, _ = resume_session(path, tmp_path, tmp_path)
        assert writer.path == restored_path and not source_again.pending_tools
        writer.close()


@pytest.mark.parametrize(
    "window,states",
    [
        ("patch_manifest", ["not_applied", "not_applied"]),
        ("patch_diff", ["not_applied", "not_applied"]),
        ("patch_after_write", ["applied", "not_applied"]),
        ("patch_committed", ["applied", "not_applied"]),
    ],
)
@pytest.mark.parametrize("conflict", [False, True])
def test_patch_hash_reconciliation_never_reapplies(
    tmp_path: Path,
    window: str,
    states: list[str],
    conflict: bool,
) -> None:
    path = crash(tmp_path, window)
    if conflict:
        (tmp_path / "a").write_bytes(b"concurrent edit")
    session, writer, _ = resume_session(path, tmp_path, tmp_path)
    writer.close()
    value = json.loads(session.messages[-1].content)
    assert value["error_code"] == "interrupted_unknown"
    evidence = value["data"]["patch_recovery"]
    assert evidence["complete_manifest"]
    assert [f["state"] for f in evidence["files"]] == (
        ["conflict", states[1]] if conflict else states
    )
    assert not (tmp_path / "b").exists() and not session.pending_tools
    if conflict:
        assert (tmp_path / "a").read_bytes() == b"concurrent edit"


@pytest.mark.parametrize("answer", [None, "deny", "allow_once"])
async def test_unknown_shell_guard_survives_new_call_ids_and_resume(
    tmp_path: Path,
    answer: str | None,
) -> None:
    path = crash(tmp_path, "result_not_saved")
    session, writer, _ = resume_session(path, tmp_path, tmp_path)
    requests: list[ApprovalRequest] = []

    async def approve(request: ApprovalRequest) -> ApprovalDecision:
        requests.append(request)
        assert "unresolved_effects" in request.capability_scope
        assert answer in {"deny", "allow_once"}
        return ApprovalDecision(request.request_id, answer)  # type: ignore[arg-type]

    gateway = SecurityGateway(approver=approve if answer else None)
    new = call(
        "exec_command", {"command": python_command("open('counter','ab').write(b'y')")}, "new-id"
    )
    try:
        result = await run_turn(
            session,
            "continue",
            ScriptedModel([reply(new), reply(text="done")]),
            native_tools(),
            Events(session, writer, ignore),
            Limits(shell=default_shell()),
            gateway=gateway,
        )
        assert result.outcome == "completed"
        assert session.unresolved_effects
        path = writer.path
    finally:
        writer.close()
    assert (tmp_path / "counter").read_bytes() == (b"xy" if answer == "allow_once" else b"x")
    again, writer, _ = resume_session(path, tmp_path, tmp_path)
    writer.close()
    assert again.unresolved_effects == session.unresolved_effects
    assert bool(requests) == bool(answer)
    # Existing deny policy remains a deny, even if the recovery approver would allow.
    denied = await SecurityGateway(SecurityPolicy("read-only"), approver=approve).execute(
        call("apply_patch", {"patch": "unused"}, "patch-new"),
        native_tools(),
        again,
        ExecutionContext(tmp_path, "patch-new", default_shell()),
        Recorder(),
    )
    assert denied.error_code == "policy_denied"


async def test_prepared_redacted_arguments_are_not_reexecuted(tmp_path: Path) -> None:
    session = Session(tmp_path, run_id="r")
    writer = SessionLog.create(session, "redacted", {}, tmp_path)
    events = Events(session, writer, ignore, ("secret-value",))
    tool = call("exec_command", {"command": "echo secret-value"})
    await events("run_started", {})
    await append_message(session, reply(tool).message, events)
    await events("tool_checkpoint", prepared(tool))
    writer.close()
    session, writer, _ = resume_session(writer.path, tmp_path, tmp_path)
    writer.close()
    assert not session.pending_tools
    assert json.loads(session.messages[-1].content)["error_code"] == "not_executed"


@pytest.mark.parametrize("barrier", ["PREPARED", "DISPATCHING", "COMPLETED"])
async def test_fsync_failure_stops_dispatch_and_later_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    barrier: str,
) -> None:
    session = Session(tmp_path)
    writer = SessionLog.create(session, "fsync", {}, tmp_path)
    real = os.fsync

    def fail(descriptor: int) -> None:
        with writer.path.open("rb") as stream:
            records, _ = read_records(stream)
        if records[-1]["kind"] == "tool_checkpoint" and records[-1]["data"]["phase"] == barrier:
            raise OSError("injected fsync failure")
        real(descriptor)

    monkeypatch.setattr(os, "fsync", fail)
    model = ScriptedModel(
        [
            reply(
                call("exec_command", {"command": python_command("open('first','w').write('x')")}),
                call(
                    "exec_command",
                    {"command": python_command("open('second','w').write('x')")},
                    "c2",
                ),
            )
        ]
    )
    try:
        result = await run_turn(
            session,
            "run",
            model,
            native_tools(),
            Events(session, writer, ignore),
            Limits(shell=default_shell()),
        )
        assert result.outcome == "failed" and "session_write_failed" in (result.reason or "")
        assert writer.failed and not (tmp_path / "second").exists()
        assert (tmp_path / "first").exists() == (barrier == "COMPLETED")
    finally:
        writer.close()


async def test_saved_full_result_recovered_in_batch_order(tmp_path: Path) -> None:
    session = Session(tmp_path, run_id="r")
    writer = SessionLog.create(session, "batch", {}, tmp_path)
    events = Events(session, writer, ignore)
    one, two = call("missing", {}, "a"), call("missing", {}, "b")
    await events("run_started", {})
    await append_message(session, reply(one, two).message, events)
    for tool in (one, two):
        await events("tool_checkpoint", prepared(tool))
    saved = ToolResult("b", False, {"stdout": "complete output"}, "unknown_tool", 42, True)
    await events("tool_checkpoint", {"call_id": "b", "phase": "COMPLETED", "result": asdict(saved)})
    writer.close()
    session, writer, _ = resume_session(writer.path, tmp_path, tmp_path)
    try:
        assert [c.id for c in session.pending_tools] == ["a", "b"]
        result = await run_turn(
            session,
            "continue",
            ScriptedModel([reply(text="done")]),
            native_tools(),
            Events(session, writer, ignore),
            Limits(),
        )
        assert result.outcome == "completed"
        values = [json.loads(m.content) for m in session.messages if m.role == "tool"]
        assert [v["call_id"] for v in values] == ["a", "b"] and values[1] == asdict(saved)
    finally:
        writer.close()


async def test_checkpoint_result_wrong_call_fails_closed(tmp_path: Path) -> None:
    session = Session(tmp_path, run_id="r")
    writer = SessionLog.create(session, "corrupt", {}, tmp_path)
    events = Events(session, writer, ignore)
    tool = call("missing", {})
    await events("run_started", {})
    await append_message(session, reply(tool).message, events)
    await events("tool_checkpoint", prepared(tool))
    await events(
        "tool_checkpoint",
        {"call_id": tool.id, "phase": "COMPLETED", "result": asdict(ToolResult("wrong", True, {}))},
    )
    writer.close()
    with pytest.raises(SessionError, match="checkpoint result"):
        resume_session(writer.path, tmp_path, tmp_path)


async def test_prepared_internal_plan_can_resume_again(tmp_path: Path) -> None:
    session = Session(tmp_path, run_id="r")
    writer = SessionLog.create(session, "plan", {}, tmp_path)
    events = Events(session, writer, ignore)
    tool = call("update_plan", {"plan": [{"step": "Inspect", "status": "in_progress"}]})
    await events("run_started", {})
    await append_message(session, reply(tool).message, events)
    await events("tool_checkpoint", prepared(tool))
    writer.close()
    session, writer, _ = resume_session(writer.path, tmp_path, tmp_path)
    try:
        registry = {"update_plan": create_update_plan_tool(session)}
        result = await run_turn(
            session,
            "continue",
            ScriptedModel([reply(text="done")]),
            registry,
            Events(session, writer, ignore),
            Limits(),
        )
        assert result.outcome == "completed" and session.plan is not None
    finally:
        writer.close()
    again, writer, _ = resume_session(writer.path, tmp_path, tmp_path)
    writer.close()
    assert again.plan == session.plan and not again.pending_tools


async def test_recovery_failure_keeps_later_saved_result(tmp_path: Path) -> None:
    session = Session(tmp_path, run_id="r")
    writer = SessionLog.create(session, "batch failure", {}, tmp_path)
    events = Events(session, writer, ignore)
    one, two = call("exec_command", {}, "a"), call("missing", {}, "b")
    await events("run_started", {})
    await append_message(session, reply(one, two).message, events)
    for tool in (one, two):
        await events("tool_checkpoint", prepared(tool))
    saved = ToolResult("b", False, {"stdout": "complete"}, "unknown_tool")
    await events("tool_checkpoint", {"call_id": "b", "phase": "COMPLETED", "result": asdict(saved)})
    writer.close()
    session, writer, _ = resume_session(writer.path, tmp_path, tmp_path)
    events = Events(session, writer, ignore)

    async def fail(kind: str, data: dict[str, object], **kw: object) -> int:
        if kind == "tool_started":
            raise RuntimeError("Recovered call failed before dispatch")
        return await events(kind, data)

    try:
        result = await run_turn(
            session, "continue", ScriptedModel([]), native_tools(), fail, Limits()
        )
        assert result.outcome == "failed"
        values = [json.loads(m.content) for m in session.messages if m.role == "tool"]
        assert values[1] == asdict(saved)
    finally:
        writer.close()
    again, writer, _ = resume_session(writer.path, tmp_path, tmp_path)
    writer.close()
    assert not again.pending_tools
