from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from nexus.core.agent import run_turn
from nexus.core.types import ExecutionContext, Limits, Message, Session
from nexus.tools import subagent
from nexus.tools.subagent import create_spawn_agent_tool, inspect_files
from tests.conftest import Recorder, ScriptedModel, call, reply


def test_inspection_bounded_confined_and_readonly(tmp_path: Path) -> None:
    (tmp_path / "module.py").write_text("needle\n" * 300)
    context = ExecutionContext(tmp_path, "read", "/bin/sh", 120)
    for mode in ["read", "search", "list"]:
        result = inspect_files({"mode": mode, "path": "module.py", "text": "needle"}, context)
        assert result.ok and len(result.data["text"].encode()) <= 120
    for path in ["../outside", str(tmp_path), ".git/config", ".env"]:
        assert not inspect_files({"mode": "read", "path": path}, context).ok
    assert (tmp_path / "module.py").read_text() == "needle\n" * 300


def test_read_page_preserves_source_and_line_numbers_with_long_path(tmp_path: Path) -> None:
    relative = Path("a_very_long_package_name") / "another_long_module_name" / "source_file.py"
    target = tmp_path / relative
    target.parent.mkdir(parents=True)
    source = [f"value_{i} = {i}" for i in range(161)]
    target.write_text("\n".join(source) + "\n", encoding="utf-8")
    result = inspect_files(
        {"mode": "read", "path": relative.as_posix(), "start_line": 2},
        ExecutionContext(tmp_path, "page", "/bin/sh", 8000),
    )
    # Repeating a long path per line used to exhaust 8KB before the end of this page.
    assert "161: value_160 = 160" in result.data["text"]
    assert not result.truncated and len(result.data["text"].encode()) <= 8000
    assert result.data["text"].splitlines() == [f"Path: {relative.as_posix()}"] + [
        f"{i + 1}: {source[i]}" for i in range(1, 161)
    ]
    assert target.read_text(encoding="utf-8") == "\n".join(source) + "\n"


def test_search_keeps_per_match_paths(tmp_path: Path) -> None:
    for name in ["a.py", "b.py"]:
        (tmp_path / name).write_text("first\nneedle\n", encoding="utf-8")
    result = inspect_files(
        {"mode": "search", "path": ".", "text": "needle"},
        ExecutionContext(tmp_path, "search", "/bin/sh", 8000),
    )
    assert result.data["text"].splitlines() == ["a.py:2: needle", "b.py:2: needle"]


async def test_recursive_loop_isolation_evidence_and_main_usage(tmp_path: Path) -> None:
    (tmp_path / "module.py").write_text("def required_interface(): pass\n")
    child_model = ScriptedModel(
        [
            reply(call("inspect_repository", {"mode": "read", "path": "module.py"})),
            reply(
                text=json.dumps(
                    {"findings": ["module.py:1 required_interface exists"], "uncertainties": []}
                )
            ),
        ]
    )
    parent = Session(tmp_path, messages=[Message("system", "project instructions")])
    main_model = ScriptedModel(
        [
            reply(call("spawn_agent", {"task": "Find required interfaces"}, "child")),
            reply(text="Integrated required_interface evidence"),
        ]
    )
    events = Recorder()
    result = await run_turn(
        parent,
        "private main task",
        main_model,
        {"spawn_agent": create_spawn_agent_tool(parent, child_model, Limits())},
        events,
        Limits(),
    )
    assert result.outcome == "completed" and result.model_calls == 2
    assert result.usage.total_tokens == 30
    assert not any("private main task" in m.content for ms in child_model.requests for m in ms)
    assert not any(m.role == "tool" and m.tool_call_id == "c1" for m in parent.messages)
    observation = next(m for m in main_model.requests[1] if m.role == "tool")
    data = json.loads(observation.content)["data"]
    assert data["report"]["findings"] == ["module.py:1 required_interface exists"]
    assert data["usage"]["total_tokens"] == 30 and data["steps"] == 2
    assert len([d for k, d in events.events if k == "subagent_model_started"]) == 2
    assert data["workspace_access"] == "read_only"


async def test_depth_and_call_limits(tmp_path: Path) -> None:
    child_model = ScriptedModel(
        [
            reply(call("spawn_agent", {"task": "recurse"})),
            reply(call("exec_command", {"command": "touch evil"}, "c2")),
            reply(text='{"findings":[],"uncertainties":["restricted"]}'),
        ]
    )
    parent = Session(tmp_path, run_id="run")
    tool = create_spawn_agent_tool(parent, child_model, Limits())
    events = Recorder()
    result = await tool.execute(
        {"task": "test"}, ExecutionContext(tmp_path, "child", "/bin/sh"), events
    )
    assert result.ok
    errors = [d["error_code"] for k, d in events.events if k == "subagent_tool_finished"]
    assert errors == ["unknown_tool", "unknown_tool"]
    assert not (tmp_path / "evil").exists()
    parent.messages = [
        Message("assistant", tool_calls=[call("spawn_agent", {}, str(i))], run_id="run")
        for i in range(3)
    ]
    result = await tool.execute(
        {"task": "test"}, ExecutionContext(tmp_path, "child", "/bin/sh"), events
    )
    assert result.error_code == "subagent_call_limit"


async def test_child_step_budget(tmp_path: Path) -> None:
    model = ScriptedModel(
        [reply(call("inspect_repository", {"mode": "list", "path": "."}, str(i))) for i in range(6)]
    )
    tool = create_spawn_agent_tool(Session(tmp_path), model, Limits())
    result = await tool.execute(
        {"task": "inspect"}, ExecutionContext(tmp_path, "c", "sh"), Recorder()
    )
    assert not result.ok and result.data["steps"] == 6 and result.data["reason"] == "max_steps"


async def test_timeout_and_external_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Slow(ScriptedModel):
        async def complete(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            await asyncio.sleep(10)

    tool = create_spawn_agent_tool(Session(tmp_path), Slow([]), Limits())
    monkeypatch.setattr(subagent, "TIMEOUT_SECONDS", 0.01)
    result = await tool.execute(
        {"task": "inspect"}, ExecutionContext(tmp_path, "c", "sh"), Recorder()
    )
    assert not result.ok and result.data["reason"] == "timeout"
    monkeypatch.setattr(subagent, "TIMEOUT_SECONDS", 150)
    task = asyncio.ensure_future(
        tool.execute({"task": "inspect"}, ExecutionContext(tmp_path, "c", "sh"), Recorder())
    )
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
