from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest

from nexus.core.agent import run_turn
from nexus.core.context import SYSTEM
from nexus.core.observations import compact
from nexus.core.stagnation import StagnationDetector
from nexus.core.types import ExecutionContext, Limits, Session
from nexus.tools import patch as module
from nexus.tools.patch import MAX_BYTES, apply_patch
from nexus.tools.patch_format import (
    MAX_ERROR_DETAIL_BYTES,
    MAX_ERROR_EXCERPT_BYTES,
    bounded_text,
    resolve_update,
)
from nexus.tools.registry import PATCH_SPEC, native_tools
from tests.conftest import Recorder, ScriptedModel, call, reply


def wrapped(body: str) -> str:
    return "*** Begin Patch\n" + body + "\n*** End Patch\n"


def update(body: str, name: str = "a") -> str:
    return wrapped(f"*** Update File: {name}\n{body}")


@pytest.mark.parametrize(
    ("patch", "code"),
    [
        ("*** Begin Patch\n", "invalid_patch"),
        (wrapped(""), "invalid_patch"),
        ("*** Update File: a\n@@\n-x\n+y\n*** End Patch", "unsupported_patch"),
        ("garbage", "unsupported_patch"),
        (" *** Begin Patch\n*** End Patch", "unsupported_patch"),
        (update("@@\n context"), "invalid_patch"),
        (update("@@"), "invalid_patch"),
        (wrapped("*** Update File: a"), "invalid_patch"),
        (wrapped("*** Add File: a"), "invalid_patch"),
        (wrapped("*** Add File: a\nraw"), "invalid_patch"),
        (wrapped("*** Delete File: a\n-bye"), "invalid_patch"),
        (wrapped("*** Update File: a\n*** Move to: b"), "unsupported_patch"),
        (update("@@\nGIT binary patch"), "unsupported_patch"),
        (update("@@\nnew mode 100755"), "unsupported_patch"),
        (update("@@\n-old\n+new\n\\ No newline at end of file"), "invalid_patch"),
    ],
)
async def test_invalid_protocol_never_writes(
    execution: ExecutionContext, patch: str, code: str
) -> None:
    path = execution.workspace / "a"
    path.write_bytes(b"old\n")
    result = await apply_patch({"patch": patch}, execution, Recorder())
    assert result.error_code == code
    assert result.data["changed_files"] == 0 and not result.data["partial"]
    assert path.read_bytes() == b"old\n"


async def test_missing_boundaries_feedback_supports_safe_retry(execution: ExecutionContext) -> None:
    path = execution.workspace / "a"
    path.write_bytes(b"old\n")
    body = "*** Update File: a\n@@\n-old\n+new"
    bad = await apply_patch({"patch": body}, execution, Recorder())
    assert not bad.ok and bad.error_code == "unsupported_patch"
    assert bad.data["changed_files"] == 0 and not bad.data["partial"]
    assert "*** Begin Patch and *** End Patch" in bad.data["detail"]
    assert path.read_bytes() == b"old\n"
    fixed = await apply_patch({"patch": wrapped(body)}, execution, Recorder())
    assert fixed.ok and fixed.data["changed_files"] == 1
    assert path.read_bytes() == b"new\n"


@pytest.mark.parametrize("heading", ["@@ def run():", "@@\n def run():"])
async def test_empty_seek_chunk_feedback_supports_safe_retry(
    execution: ExecutionContext, heading: str
) -> None:
    path = execution.workspace / "a"
    original = b"def run():\nold\n"
    path.write_bytes(original)
    bad = await apply_patch(
        {"patch": update(heading + "\n@@\n-old\n+new")}, execution, Recorder()
    )
    assert not bad.ok and bad.error_code == "invalid_patch"
    assert bad.data["failed_hunk"] == 1 and bad.data["changed_files"] == 0
    assert "Each @@ starts a new chunk" in bad.data["detail"]
    assert "before the next @@" in bad.data["detail"]
    assert path.read_bytes() == original
    fixed = await apply_patch(
        {"patch": update("@@ def run():\n-old\n+new")}, execution, Recorder()
    )
    assert fixed.ok and fixed.data["changed_files"] == 1
    assert path.read_bytes() == b"def run():\nnew\n"


@pytest.mark.parametrize("prefix", ["", "diff --git a/a b/a\n", "\n \t\n"])
async def test_legacy_dispatch_and_mixed_format_unchanged(
    execution: ExecutionContext, prefix: str
) -> None:
    path = execution.workspace / "a"
    path.write_bytes(b"old\n")
    legacy = prefix + "--- a/a\n+++ b/a\n@@ -1 +1 @@\n-old\n+new\n"
    bad = await apply_patch({"patch": legacy + "*** End Patch\n"}, execution, Recorder())
    assert bad.error_code == "unsupported_patch" and path.read_bytes() == b"old\n"
    good = await apply_patch({"patch": legacy}, execution, Recorder())
    assert good.ok and path.read_bytes() == b"new\n"


async def test_actual_toolspec_example_and_schema(execution: ExecutionContext) -> None:
    root = execution.workspace
    (root / "src").mkdir()
    (root / "src/app.py").write_bytes(b"def run():\nold()\n")
    (root / "obsolete.txt").write_bytes(b"obsolete\n")
    description = PATCH_SPEC.description
    example = description.split("Example:\n", 1)[1].split("\n\n", 1)[0]
    result = await apply_patch({"patch": "\n \t\n" + example + "\n\n"}, execution, Recorder())
    assert result.ok and result.data["changed_files"] == 3
    assert (root / "src/app.py").read_bytes() == b"def run():\nnew()\n"
    assert (root / "tests/new_case.txt").read_bytes() == b"fixture\n"
    assert not (root / "obsolete.txt").exists()
    assert result.data["created_directories"] == ["tests"]
    assert PATCH_SPEC.input_schema == {
        "type": "object",
        "properties": {"patch": {"type": "string"}},
        "required": ["patch"],
        "additionalProperties": False,
    }
    assert "Each path may appear only once" in description
    assert "Do not provide line numbers or hunk counts" in description
    assert "does not define a function/class scope" in description
    assert "---" not in description and "unified" not in description
    assert "unified-diff apply_patch" not in SYSTEM
    assert "Use exec_command to inspect and run checks, and apply_patch to edit." in SYSTEM


async def test_requests_intended_edit_and_audit(execution: ExecutionContext) -> None:
    before = (
        "def prepend_scheme_if_needed(url, new_scheme):\n"
        "    parsed = parse_url(url)\n"
        "    scheme, auth, host, port, path, query, fragment = parsed\n"
        "    netloc = parsed.netloc\n"
        "    if not netloc:\n"
        "        netloc, path = path, netloc\n"
        "    if scheme is None:\n"
        "        scheme = new_scheme\n"
        "    return urlunparse((scheme, netloc, path, '', query, fragment))\n"
    )
    addition = '\n    if auth:\n        netloc = "{}@{}".format(auth, netloc)\n'
    path = execution.workspace / "a"
    path.write_bytes(before.encode())
    patch = update(
        "@@ def prepend_scheme_if_needed(url, new_scheme):\n"
        "     netloc = parsed.netloc\n"
        "     if not netloc:\n"
        "         netloc, path = path, netloc\n"
        '+\n+    if auth:\n+        netloc = "{}@{}".format(auth, netloc)'
    )
    result = await apply_patch({"patch": patch}, execution, Recorder())
    expected = before.replace(
        "    if scheme is None:", addition + "    if scheme is None:"
    ).encode()
    assert result.ok and type(result.data["changed_files"]) is int
    assert result.data["changed_files"] == 1 and path.read_bytes() == expected
    fact = result.data["files"][0]
    assert fact["before_hash"] == hashlib.sha256(before.encode()).hexdigest()
    assert fact["after_hash"] == hashlib.sha256(expected).hexdigest()
    assert "--- a/a" in fact["diff"] and "+    if auth:" in fact["diff"]


@pytest.mark.parametrize(
    ("before", "body", "after"),
    [
        (b"old\n", "@@\n-old\n+new", b"new\n"),
        (b"old", "@@\n-old\n+new", b"new"),
        (b"anchor", "@@ anchor\n+next", b"anchor\nnext"),
        (b"anchor\r\nold", "@@ anchor\n-old\n+new\n+next", b"anchor\r\nnew\r\nnext"),
        (b"old\r\nend\r\n", "@@\n-old\n+new", b"new\r\nend\r\n"),
        (b"old\nend", "@@\n-old\n+new", b"new\nend"),
        (b"", "@@\n+next", b"next"),
        (b"", "@@\n+first\n@@\n+second", b"first\nsecond"),
        (b"old\n", "@@\n-old", b""),
        (b"keep \t\r\nold\r\n", "@@\n keep\n-old\n+new", b"keep \t\r\nnew\r\n"),
        (b"anchor \t\nold\n", "@@ anchor\n-old\n+new", b"anchor \t\nnew\n"),
        (b"top\n\nold\n", "@@\n top\n\n-old\n+new", b"top\n\nnew\n"),
        (b"old\n", "@@  \t\n-old\n+new", b"new\n"),
        (b"a\nb\nc\n", "@@\n-a\n+A\n@@\n-c\n+C", b"A\nb\nC\n"),
        (b"anchor\nold\n", "@@ anchor\n+first\n@@ old\n+last", b"anchor\nfirst\nold\nlast\n"),
        (b"old\nold \n", "@@\n-old\n+new", b"new\nold \n"),
        (b"anchor\nold\nanchor \nold \n", "@@ anchor\n-old\n+new", b"anchor\nnew\nanchor \nold \n"),
        ("old\u2028text\n".encode(), "@@\n-old\u2028text\n+new", b"new\n"),
        (b"top\r\nkeep \t\nold\r\nend", "@@\n keep\n-old\n+new", b"top\r\nkeep \t\nnew\r\nend"),
    ],
)
async def test_matching_and_exact_output_bytes(
    execution: ExecutionContext, before: bytes, body: str, after: bytes
) -> None:
    path = execution.workspace / "a"
    path.write_bytes(before)
    result = await apply_patch({"patch": update(body)}, execution, Recorder())
    assert result.ok, result
    assert path.read_bytes() == after


@pytest.mark.parametrize(
    ("before", "body", "hunk", "detail"),
    [
        (b"  old\n", "@@\n- old\n+new", 1, "old block not found"),
        (b"old\n", "@@ absent\n-old\n+new", 1, "anchor"),
        (b"anchor\nanchor\nold\n", "@@ anchor\n-old\n+new", 1, "matched 2"),
        (b"anchor \nanchor\t\nold\n", "@@ anchor\n-old\n+new", 1, "matched 2"),
        (b"old\nold\n", "@@\n-old\n+new", 1, "matched 2"),
        (b"old \nold\t\n", "@@\n-old\n+new", 1, "matched 2"),
        (b"old\n", "@@\n-absent\n+new", 1, "old block not found"),
        (b"old\n", "@@\n+new", 1, "requires an @@ anchor"),
        (b"a\nb\n", "@@\n-b\n+B\n@@\n-a\n+A", 2, "not found"),
        (b"a\nb\nc\n", "@@\n-a\n b\n@@\n-b\n c", 2, "not found"),
        (b"anchor\nold\n", "@@ anchor\n+insert\n@@\n-old\n+new", 2, "same source position"),
        (b"anchor\nold\n", "@@ anchor\n-old\n+new\n@@ anchor\n+later", 2, "anchor"),
        (b"old\n", "@@\n-old\n+new\n@@\n-new\n+other", 2, "not found"),
    ],
)
async def test_conflicts_fail_closed(
    execution: ExecutionContext, before: bytes, body: str, hunk: int, detail: str
) -> None:
    path = execution.workspace / "a"
    path.write_bytes(before)
    result = await apply_patch({"patch": update(body)}, execution, Recorder())
    assert result.error_code == "patch_conflict"
    assert result.data["failed_file"] == "a" and result.data["failed_hunk"] == hunk
    assert detail in result.data["detail"]
    assert path.read_bytes() == before


async def test_anchor_is_search_start_not_scope(execution: ExecutionContext) -> None:
    path = execution.workspace / "a"
    before = b"def first():\n    value = 2\n\ndef second():\n    value = 1\n"
    path.write_bytes(before)
    result = await apply_patch(
        {"patch": update("@@ def first():\n-    value = 1\n+    value = 3")}, execution, Recorder()
    )
    assert result.ok and path.read_bytes() == before.replace(b"value = 1", b"value = 3")
    path.write_bytes(before)
    result = await apply_patch(
        {"patch": update("@@\n def first():\n-    value = 2\n+    value = 3")},
        execution,
        Recorder(),
    )
    assert result.ok and path.read_bytes() == before.replace(b"value = 2", b"value = 3")


@pytest.mark.parametrize(
    "name", ["../outside", "/absolute", "C:/file", "a/../x", "x:stream", "a//b", ""]
)
async def test_path_security(execution: ExecutionContext, name: str) -> None:
    result = await apply_patch(
        {"patch": wrapped(f"*** Add File: {name}\n+x")}, execution, Recorder()
    )
    assert result.error_code == "path_escape" and not list(execution.workspace.iterdir())
    assert result.data["failed_hunk"] is None


@pytest.mark.parametrize("second", ["dir/a", "dir\\a"])
async def test_duplicate_resolved_target_and_preflight(
    execution: ExecutionContext, second: str
) -> None:
    result = await apply_patch(
        {"patch": wrapped(f"*** Add File: dir/a\n+x\n*** Add File: {second}\n+y")},
        execution,
        Recorder(),
    )
    assert result.error_code == "invalid_patch"
    assert not list(execution.workspace.iterdir())


@pytest.mark.parametrize("kind", ["Update", "Delete"])
@pytest.mark.parametrize(
    "content",
    [None, b"\xff", b"a\0b", b"x" * (MAX_BYTES + 1)],
    ids=["missing", "invalid-utf8", "nul", "oversize"],
)
async def test_file_checks(execution: ExecutionContext, kind: str, content: bytes | None) -> None:
    path = execution.workspace / "a"
    if content is not None:
        path.write_bytes(content)
    body = f"*** {kind} File: a" + ("\n@@\n-x\n+y" if kind == "Update" else "")
    result = await apply_patch(
        {"patch": wrapped("*** Add File: fresh\n+x\n" + body)}, execution, Recorder()
    )
    assert not result.ok and result.data["failed_hunk"] is None
    assert result.data["failed_file"] == "a" and not (execution.workspace / "fresh").exists()
    if content is not None:
        assert path.read_bytes() == content


async def test_add_empty_line_existing_target_and_noop(execution: ExecutionContext) -> None:
    patch = wrapped("*** Add File: a\n+")
    result = await apply_patch({"patch": patch}, execution, Recorder())
    assert result.ok and (execution.workspace / "a").read_bytes() == b"\n"
    result = await apply_patch({"patch": patch}, execution, Recorder())
    assert result.error_code == "patch_conflict" and result.data["failed_hunk"] is None
    result = await apply_patch({"patch": update("@@\n-\n+")}, execution, Recorder())
    assert result.ok and result.data["no_changes"] and result.data["changed_files"] == 0


@pytest.mark.parametrize(
    ("body", "hunk"),
    [("@@\n-old\n+new\n@@\nraw", 2), ("@@\n context", 1), ("raw", None)],
)
async def test_parse_error_ownership(
    execution: ExecutionContext, body: str, hunk: int | None
) -> None:
    result = await apply_patch({"patch": update(body)}, execution, Recorder())
    assert result.error_code == "invalid_patch"
    assert result.data["failed_file"] == "a" and result.data["failed_hunk"] == hunk


async def test_error_budget_and_observation(execution: ExecutionContext) -> None:
    (execution.workspace / "a").write_bytes(b"private target text\n")
    result = await apply_patch(
        {"patch": update("@@ " + "锚" * 10000 + "\n-old\n+new")}, execution, Recorder()
    )
    assert result.error_code == "patch_conflict" and result.data["failed_hunk"] == 1
    detail = result.data["detail"]
    assert len(detail.encode()) <= MAX_ERROR_DETAIL_BYTES and "...[truncated]" in detail
    assert (
        len(bounded_text("锚" * 10000, MAX_ERROR_EXCERPT_BYTES).encode()) <= MAX_ERROR_EXCERPT_BYTES
    )
    assert "private target text" not in detail
    # A large raw result becomes COLD; the request-only compact copy keeps chunk ownership.
    result.data["extra"] = "large omitted payload" * 500
    message = result.message()
    message.seq = 17
    projected = compact(message, "apply_patch")
    data = json.loads(projected.content)
    assert data["projection"] == "compact-v1" and data["source_seq"] == 17
    assert data["data"]["failed_hunk"] == 1 and data["data"]["failed_file"] == "a"
    assert "extra" in json.loads(message.content)["data"]


async def test_concurrency_mode_and_partial_commit(
    execution: ExecutionContext, monkeypatch: Any
) -> None:
    path = execution.workspace / "a"
    path.write_bytes(b"old\n")
    if os.name != "nt":
        path.chmod(0o750)
    patch = update("@@\n-old\n+new")
    result = await apply_patch({"patch": patch}, execution, Recorder())
    assert result.ok
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o750
    path.write_bytes(b"old\n")
    original = resolve_update

    def concurrent(before: bytes, file: Any) -> bytes:
        after = original(before, file)
        path.write_bytes(b"concurrent\n")
        return after

    with monkeypatch.context() as scoped:
        scoped.setattr(module, "resolve_update", concurrent)
        result = await apply_patch({"patch": patch}, execution, Recorder())
    assert result.error_code == "concurrent_change" and result.data["failed_hunk"] is None
    assert path.read_bytes() == b"concurrent\n" and result.data["changed_files"] == 0
    commit = module._commit

    def fail_second(target: Path, change: Any) -> None:
        if target.name == "second":
            raise OSError("disk failure")
        commit(target, change)

    monkeypatch.setattr(module, "_commit", fail_second)
    result = await apply_patch(
        {"patch": wrapped("*** Add File: first\n+x\n*** Add File: second\n+y")},
        execution,
        Recorder(),
    )
    assert result.error_code == "patch_io_error" and result.data["partial"]
    assert result.data["changed_files"] == 1 and result.data["failed_file"] == "second"
    assert result.data["failed_hunk"] is None
    assert (execution.workspace / "first").read_bytes() == b"x\n"
    assert not (execution.workspace / "second").exists()
    assert not list(execution.workspace.glob(".nexus-patch-*"))
    detector = StagnationDetector(Session(execution.workspace, run_id="run"))
    detector.observe_completed_step(
        Session(execution.workspace, run_id="run"),
        [(call("apply_patch", {}), result)],
        step=1,
        max_steps=40,
    )
    assert detector.mutation_seen


async def test_registry_agent_chain_and_detector(execution: ExecutionContext) -> None:
    model = ScriptedModel(
        [
            reply(call("apply_patch", {"patch": wrapped("*** Add File: a\n+old")})),
            reply(call("apply_patch", {"patch": update("@@\n-old\n+new")}, "c2")),
            reply(text="done"),
            reply(text="Change is unverified; ending with that limitation."),
        ]
    )
    session = Session(execution.workspace)
    events = Recorder()
    result = await run_turn(session, "edit", model, native_tools(), events, Limits())
    assert result.outcome == "completed" and (execution.workspace / "a").read_bytes() == b"new\n"
    assert sum(kind == "tool_started" for kind, _ in events.events) == 2
    assert sum(kind == "tool_finished" for kind, _ in events.events) == 2
    tool_message = [m for m in model.requests[1] if m.role == "tool"][-1]
    assert json.loads(tool_message.content)["data"]["changed_files"] == 1
    success = await apply_patch({"patch": update("@@\n-new\n+changed")}, execution, Recorder())
    detector = StagnationDetector(session)
    for step in range(1, 40):
        assert (
            detector.observe_completed_step(
                session, [(call("apply_patch", {}), success)], step=step, max_steps=40
            )
            is None
        )
    assert detector.mutation_seen


async def test_symlink_or_junction_rejected(execution: ExecutionContext) -> None:
    target = execution.workspace / "target"
    target.mkdir()
    (target / "a").write_bytes(b"old\n")
    link = execution.workspace / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            raise
        # The existing Windows boundary test uses a junction when symlinks
        # require privileges; both go through the same checked_path policy.
        process = await asyncio.to_thread(
            subprocess.run,
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            check=False,
        )
        assert process.returncode == 0
    result = await apply_patch({"patch": update("@@\n-old\n+new", "link/a")}, execution, Recorder())
    assert result.error_code == "path_escape" and result.data["failed_hunk"] is None
    assert (target / "a").read_bytes() == b"old\n"


async def test_capacity_checks_and_bounded_facts(execution: ExecutionContext) -> None:
    path = execution.workspace / "a"
    path.write_bytes(b"anchor\n" + b"x" * (MAX_BYTES - 7))
    result = await apply_patch({"patch": update("@@ anchor\n+extra")}, execution, Recorder())
    assert result.error_code == "file_too_large" and path.stat().st_size == MAX_BYTES
    assert result.data["failed_hunk"] is None
    result = await apply_patch(
        {"patch": wrapped("*** Add File: b\n+" + "x" * MAX_BYTES)}, execution, Recorder()
    )
    assert result.error_code == "patch_too_large" and not (execution.workspace / "b").exists()
    result = await apply_patch({"patch": wrapped("*** Add File: b\n+\0")}, execution, Recorder())
    assert result.error_code == "unsupported_patch"
    execution.output_limit_bytes = 1024
    result = await apply_patch(
        {"patch": wrapped("*** Add File: b\n+" + "x" * 20000)}, execution, Recorder()
    )
    assert result.ok and result.truncated and result.data["changed_files"] == 1
    assert len(result.data["files"][0]["diff"].encode()) < 1024
    assert (execution.workspace / "b").read_bytes() == b"x" * 20000 + b"\n"


@pytest.mark.parametrize("arguments", [{}, {"patch": 1}, {"patch": "", "other": True}])
async def test_argument_validation(execution: ExecutionContext, arguments: dict[str, Any]) -> None:
    result = await apply_patch(arguments, execution, Recorder())
    assert result.error_code == "invalid_arguments" and not list(execution.workspace.iterdir())


async def test_directory_is_not_a_text_target(execution: ExecutionContext) -> None:
    (execution.workspace / "a").mkdir()
    result = await apply_patch({"patch": wrapped("*** Delete File: a")}, execution, Recorder())
    assert result.error_code == "patch_conflict" and (execution.workspace / "a").is_dir()
