from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from nexus.app.events import Events
from nexus.app.session import SessionLog, read_records, replay
from nexus.core.agent import run_turn
from nexus.core.history import ContextBuilder, groups
from nexus.core.types import ExecutionContext, Limits, RuntimeEvent, Session, ToolCancelled
from nexus.tools.execution import execute
from nexus.tools.patch import apply_patch
from nexus.tools.patch_format import MAX_ERROR_DETAIL_BYTES, PatchError, parse_nexus_patch
from nexus.tools.registry import EXEC_SPEC, PATCH_SPEC, native_tools
from tests.conftest import Recorder, ScriptedModel, call, python_command, reply
from tests.test_patch_v02 import update, wrapped

FIXTURES = Path(__file__).parent / "fixtures/stage31b"


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("blanks", [1, 3, 2000])
async def test_only_interior_add_blanks_normalized(
    execution: ExecutionContext,
    newline: str,
    blanks: int,
) -> None:
    body = "*** Add File: a\n+first\n" + "\n" * blanks + "+last"
    raw = wrapped(body).replace("\n", newline)
    events = Recorder()
    result = await apply_patch({"patch": raw}, execution, events)
    assert result.ok and result.data["add_file_blank_lines_normalized"] == blanks
    expected = b"first\n" + b"\n" * blanks + b"last\n"
    assert (execution.workspace / "a").read_bytes() == expected
    audit = events.events[0][1]
    assert audit["after_hash"] == hashlib.sha256(expected).hexdigest()
    assert audit["added_lines"] == blanks + 2
    assert '"diff"' not in result.message().content
    # The explicit valid spelling has identical semantics, without normalization.
    (execution.workspace / "a").unlink()
    explicit = wrapped("*** Add File: a\n+first\n" + "+\n" * blanks + "+last")
    clean = await apply_patch({"patch": explicit}, execution, Recorder())
    assert clean.ok and "add_file_blank_lines_normalized" not in clean.data
    assert (execution.workspace / "a").read_bytes() == expected


@pytest.mark.parametrize(
    "body",
    [
        "*** Add File: a\n\n+body",
        "*** Add File: a\n+body\n",
        "*** Add File: a\n+body\n\n*** Add File: b\n+body",
        "*** Add File: a\n+body\n \n+more",
        "*** Add File: a\n+body\n\t\n+more",
        "*** Add File: a\n+body\n\nmissing prefix\n+more",
        "*** Add File: a\n+body\n\n@@\n+more",
        "*** Add File: ../escape\n+body\n\n+more",
    ],
)
async def test_ambiguous_add_or_path_still_rejected(execution: ExecutionContext, body: str) -> None:
    result = await apply_patch({"patch": wrapped(body)}, execution, Recorder())
    assert not result.ok and not result.data["partial"]
    assert not list(execution.workspace.iterdir())


async def test_missing_end_never_synthesized(execution: ExecutionContext) -> None:
    result = await apply_patch(
        {"patch": "*** Begin Patch\n*** Add File: a\n+one\n\n+two"}, execution, Recorder()
    )
    assert not result.ok and result.data["changed_files"] == 0


@pytest.mark.parametrize(
    "before,body,phrase,line",
    [
        (b"actual\nold\n", "@@ absent\n-old\n+new", "anchor absent not found", "line 1"),
        (b"anchor\nactual\n", "@@ anchor\n-stale\n+new", "old block not found", "line 2"),
        (b"old\nold\n", "@@\n-old\n+new", "candidate start lines (first 5): [1, 2]", "line 1"),
        (
            b"anchor\nold\nanchor\nold\n",
            "@@ anchor\n-old\n+new",
            "anchor anchor matched 2",
            "line 1",
        ),
        (
            b"def f():\n    value = 1\n",
            "@@ def f():\n-  value = 1\n+  value = 2",
            "old block not found",
            "line 2",
        ),
    ],
)
async def test_bounded_current_location_does_not_guess(
    execution: ExecutionContext,
    before: bytes,
    body: str,
    phrase: str,
    line: str,
) -> None:
    path = execution.workspace / "a"
    path.write_bytes(before)
    result = await apply_patch({"patch": update(body)}, execution, Recorder())
    assert result.error_code == "patch_conflict"
    assert result.data["failed_file"] == "a" and result.data["failed_hunk"] == 1
    detail = result.data["detail"]
    assert phrase in detail and line in detail and "Expected old text:" in detail
    assert "preserve leading indentation" in detail and "do not retry" in detail
    assert len(detail.encode()) <= MAX_ERROR_DETAIL_BYTES
    assert path.read_bytes() == before


async def test_unique_rstrip_and_ordered_chunks_keep_existing_semantics(
    execution: ExecutionContext,
) -> None:
    path = execution.workspace / "a"
    path.write_bytes(b"first \t\r\nkeep\r\nlast")
    result = await apply_patch(
        {"patch": update("@@\n-first\n+FIRST\n@@\n-last\n+LAST")}, execution, Recorder()
    )
    assert result.ok and path.read_bytes() == b"FIRST\r\nkeep\r\nLAST"


@pytest.mark.parametrize("sample_index", [0, 1, 2])
def test_real_filepilot_format_failures_still_require_correct_chunks(sample_index: int) -> None:
    evidence = json.loads((FIXTURES / "rename-failures.json").read_text(encoding="utf-8"))
    sample = evidence["samples"][sample_index]
    patch = json.loads(sample["call"]["arguments_json"])["patch"]
    with pytest.raises(PatchError) as caught:
        parse_nexus_patch(patch)
    assert caught.value.code == sample["result"]["error_code"]
    assert caught.value.path == sample["result"]["data"]["failed_file"]
    assert caught.value.hunk == sample["result"]["data"]["failed_hunk"]
    # Real model repair remains accepted; no guessing the missing edit/body text.
    parse_nexus_patch(json.loads(sample["recovery_call"]["arguments_json"])["patch"])


async def test_real_filepilot_stale_old_block_and_actual_repair(
    execution: ExecutionContext,
) -> None:
    evidence = json.loads((FIXTURES / "rename-failures.json").read_text(encoding="utf-8"))
    sample = evidence["samples"][3]
    source = (FIXTURES / "create_delete_harness.js").read_bytes()
    assert hashlib.sha256(source).hexdigest() == evidence["harness_sha256"]
    path = execution.workspace / "tests/web/create_delete_harness.js"
    path.parent.mkdir(parents=True)
    path.write_bytes(source)
    events = Recorder()
    result = await apply_patch(json.loads(sample["call"]["arguments_json"]), execution, events)
    assert result.error_code == "patch_conflict"
    assert result.data["failed_file"] == sample["result"]["data"]["failed_file"]
    assert result.data["failed_hunk"] == 5 and "old block not found" in result.data["detail"]
    assert "current file has" in result.data["detail"] and not events.events
    assert path.read_bytes() == source
    repaired = await apply_patch(
        json.loads(sample["recovery_call"]["arguments_json"]), execution, events
    )
    assert repaired.ok and events.events[0][0] == "patch_diff"
    assert (
        repaired.data["files"][0]["after_hash"]
        == "09bb8514537ef79afbb83a348911d00a74887c00da3830fb95e6d2ccdf7fed98"
    )


@pytest.mark.parametrize("key", ["command", "cmd"])
async def test_real_command_and_alias_without_mutation(
    execution: ExecutionContext, key: str
) -> None:
    args = {key: python_command("print('alias-result')"), "timeout_ms": 10000}
    original = dict(args)
    result = await execute(args, execution, Recorder())
    assert result.ok and "alias-result" in result.data["stdout"] and args == original
    assert result.data.get("argument_alias_used") == ("cmd_to_command" if key == "cmd" else None)


async def test_alias_workdir_and_truncation(execution: ExecutionContext) -> None:
    result = await execute({"cmd": "echo no", "workdir": "missing"}, execution, Recorder())
    assert not result.ok and result.error_code == "invalid_workdir"
    execution.output_limit_bytes = 1000
    result = await execute({"cmd": python_command("print('x'*10000)")}, execution, Recorder())
    assert result.ok and result.truncated and result.data["argument_alias_used"] == "cmd_to_command"


async def test_alias_cancellation_keeps_marker(execution: ExecutionContext) -> None:
    events = Recorder()
    task = asyncio.create_task(
        execute(
            {"cmd": python_command("import time; print('ready',flush=True); time.sleep(30)")},
            execution,
            events,
        )
    )
    await asyncio.wait_for(events.output_started.wait(), 10)
    task.cancel()
    with pytest.raises(ToolCancelled) as caught:
        await task
    assert caught.value.result.data["argument_alias_used"] == "cmd_to_command"
    assert not caught.value.result.data["cleanup_incomplete"]


async def test_alias_original_call_and_pair_survive_replay(execution: ExecutionContext) -> None:
    async def ignore(event: RuntimeEvent) -> None:
        pass

    session = Session(execution.workspace)
    writer = SessionLog.create(session, "alias", {}, execution.workspace)
    events = Events(session, writer, ignore)
    requested = call("exec_command", {"cmd": python_command("print('ok')")})
    model = ScriptedModel([reply(requested), reply(text="done")])
    try:
        result = await run_turn(
            session, "run", model, native_tools(), events, Limits(shell=execution.shell)
        )
        assert result.outcome == "completed"
        rows, _ = read_records(writer.stream)
        restored = replay(rows, execution.workspace)
        groups(ContextBuilder().build_active_context(restored))
        assert next(m for m in restored.messages if m.tool_calls).tool_calls[0] == requested
        receipts = [m for m in restored.messages if m.role == "tool"]
        assert len(receipts) == 1
        assert json.loads(receipts[0].content)["data"]["argument_alias_used"] == "cmd_to_command"
        assert sum(r["kind"] == "tool_started" for r in rows) == 1
        assert sum(r["kind"] == "tool_finished" for r in rows) == 1
    finally:
        writer.close()


def test_schema_stays_canonical_and_description_is_explicit() -> None:
    assert set(EXEC_SPEC.input_schema["properties"]) == {"command", "workdir", "timeout_ms"}
    assert EXEC_SPEC.input_schema["required"] == ["command"]
    assert "not cmd" in EXEC_SPEC.description and '{"command":"pytest -q"}' in EXEC_SPEC.description
    assert "anchor-only or context-only chunk is invalid" in PATCH_SPEC.description
    assert "including empty lines (a lone '+')" in PATCH_SPEC.description
