from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from nexus.core.agent import run_turn
from nexus.core.types import (
    ExecutionContext,
    Limits,
    ModelError,
    Session,
    Tool,
    ToolResult,
    ToolSpec,
)
from nexus.tools.execution import execute
from nexus.tools.patch import apply_patch
from tests.conftest import Recorder, ScriptedModel, call, python_command, reply


async def test_patch_concurrent_change_and_mode_preservation(
    execution: ExecutionContext, monkeypatch: Any
) -> None:
    from nexus.tools import patch as module

    path = execution.workspace / "a"
    path.write_bytes(b"old\n")
    if os.name != "nt":
        path.chmod(0o750)
    patch = "--- a/a\n+++ b/a\n@@ -1 +1 @@\n-old\n+new\n"
    result = await apply_patch({"patch": patch}, execution, Recorder())
    assert result.ok
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o750
    path.write_bytes(b"old\n")
    original = module.apply_hunks

    def concurrent(before: bytes, hunks: Any) -> bytes:
        after = original(before, hunks)
        path.write_bytes(b"concurrent\n")
        return after

    monkeypatch.setattr(module, "apply_hunks", concurrent)
    result = await apply_patch({"patch": patch}, execution, Recorder())
    assert result.error_code == "concurrent_change"
    assert path.read_bytes() == b"concurrent\n"


@pytest.mark.parametrize(
    "patch",
    [
        "*** Begin Patch\n",
        "new file mode 100755\n",
        "--- a/a\n+++ b/b\n@@ -1 +1 @@\n-x\n+y\n",
        "GIT binary patch\n",
    ],
)
async def test_unsupported_patch_never_writes(execution: ExecutionContext, patch: str) -> None:
    result = await apply_patch({"patch": patch}, execution, Recorder())
    assert result.error_code == "unsupported_patch"
    assert not list(execution.workspace.iterdir())


async def test_cleanup_failure_stops_same_batch(tmp_path: Path) -> None:
    invoked: list[str] = []

    async def dirty(args: Any, context: ExecutionContext, emit: Any) -> ToolResult:
        invoked.append(context.call_id)
        return ToolResult(
            context.call_id, False, {"cleanup_incomplete": True}, "cleanup_incomplete"
        )

    model = ScriptedModel([reply(call("dirty", {}, "a"), call("dirty", {}, "b"))])
    session = Session(tmp_path)
    result = await run_turn(
        session,
        "test",
        model,
        {"dirty": Tool(ToolSpec("dirty", "", {}), dirty)},
        Recorder(),
        Limits(),
    )
    assert result.outcome == "failed" and invoked == ["a"]
    assert json.loads(session.messages[-1].content)["error_code"] == "not_executed"


async def test_invalid_utf8_is_observation(execution: ExecutionContext) -> None:
    result = await execute(
        {"command": python_command("import os; os.write(1,b'\\xff')")}, execution, Recorder()
    )
    assert result.ok and result.data["decode_replaced"]
    assert "\ufffd" in result.data["stdout"]


async def test_context_limit_without_old_groups_stops_without_retry(tmp_path: Path) -> None:
    class TooLarge(ScriptedModel):
        async def complete(self, messages: Any, tools: Any, emit: Any) -> Any:
            self.requests.append(list(messages))
            raise ModelError("context_limit")

    model = TooLarge([])
    result = await run_turn(Session(tmp_path), "small", model, {}, Recorder(), Limits())
    assert result.outcome == "limited" and len(model.requests) == 1
