from __future__ import annotations

import asyncio
import io
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest
from prompt_toolkit import PromptSession
from prompt_toolkit.application import create_app_session
from prompt_toolkit.data_structures import Size
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.output.vt100 import Vt100_Output
from rich.cells import cell_len
from rich.console import Console

from nexus.app import bootstrap
from nexus.app.agent_view import AgentUsage
from nexus.app.cli import agent_command
from nexus.app.input import TaskInput
from nexus.app.subagents import Agent
from nexus.app.tui import Transcript
from nexus.core.types import Message, RunResult
from tests.conftest import ScriptedModel, reply
from tests.test_cli_ux import until
from tests.test_conversation import ignore
from tests.test_input_v02 import editor_session
from tests.test_stage8_collaboration import root_conversation
from tests.test_stage8_runtime_ui import screen_rows


def agent(ident: str, task: str, state: str = "completed", *, parent: str = "root") -> Agent:
    return Agent(ident, parent, "scout", "详细任务说明", "tests", 1 if parent == "root" else 2,
                 "test", None, state=state, source_run_id=task, task_name="测试覆盖调查",
                 delivered=True)


async def test_current_tree_history_names_usage_and_real_editor(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    root = root_conversation(tmp_path, monkeypatch)
    manager = root.coordinator
    assert manager is not None
    root.session.run_id = "task-two"
    for i in range(12):
        manager.agents[f"old-{i:04}"] = agent(f"old-{i:04}", "task-one")
    for i in range(9):
        manager.agents[f"failed-{i}"] = agent(f"failed-{i}", "task-one", "failed")
    current = agent("current1", "task-two", "running")
    current.task_name = "执行流程调查"
    waiting = agent("waiting1", "task-one", "waiting_decision")
    manager.agents.update({current.id: current, waiting.id: waiting})
    manager.usage = {
        "root": AgentUsage(1, 1, [{"total_tokens": 100}]),
        "old-0000": AgentUsage(1, 1, [{"total_tokens": 200}]),
        current.id: AgentUsage(2, 1, [{"total_tokens": 30}]),
    }
    captured = io.StringIO()
    root.consumer = Transcript(Console(file=captured, width=120, no_color=True))
    try:
        panel = manager.panel()
        assert "old-0000" not in panel and "历史 12 个" in panel
        assert "执行流程调查 · scout current1 · running" in panel
        assert "测试覆盖调查 · scout waiting1 · waiting_decision" in panel
        assert panel.index("current1") < panel.index("failed-0")
        assert panel.index("waiting1") < panel.index("failed-0")
        assert "会话累计（主+全部子代理）: ≥330 tok?" in panel
        with create_app_session(output=DummyOutput()):
            editor = TaskInput(PromptSession(output=DummyOutput()))
            assert await agent_command(root, editor, "/agents")
            assert "执行流程调查 · scout current1" in captured.getvalue()
            assert "该代理累计: ≥30 tok?" in captured.getvalue()
            captured.seek(0)
            captured.truncate()
            assert await agent_command(root, editor, "/agents history")
            assert "测试覆盖调查 · scout old-0000" in captured.getvalue()
            assert "current1" not in captured.getvalue() and "waiting1" not in captured.getvalue()
            assert await agent_command(root, editor, "/agent old-0000")
            assert editor.target_agent == "old-0000"  # History IDs still resolve.
        terminal = Vt100_Output(io.StringIO(), lambda: Size(rows=24, columns=100))
        async with editor_session(terminal) as (editor, prompt, pipe, pending):
            editor.agent_panel = manager.panel
            pipe.send_text("中文草稿")
            await until(lambda: prompt.default_buffer.text == "中文草稿")
            await until(lambda: "waiting1" in "\n".join(screen_rows(prompt)))
            text = "\n".join(screen_rows(prompt))
            assert "old-0000" not in text
            assert "current1" in text and "执行流程调查" in text and "测试覆盖调查" in text
            assert "Nexus" in text and "中文草稿" in text
            pipe.send_text("\r")
            assert await pending == "中文草稿"
    finally:
        manager.agents.clear()  # Display fixtures have no running conversations.
        await root.close()


@pytest.mark.parametrize("state", ["starting", "running", "waiting_decision", "cancelling",
                                   "failed", "cleanup_failed", "recovery_failed", "interrupted",
                                   "limited"])
async def test_older_unsettled_agents_and_ancestors_stay_visible(
    tmp_path: Path, monkeypatch: Any, state: str,
) -> None:
    root = root_conversation(tmp_path, monkeypatch)
    manager = root.coordinator
    assert manager is not None
    root.session.run_id = "new-task"
    parent = agent("parent01", "old-task")
    child = agent("nested01", "child-run", state, parent=parent.id)
    manager.agents = {parent.id: parent, child.id: child}
    try:
        assert manager.task_run(child) == "old-task"  # Legacy nested association.
        assert manager.current_agents() == {parent.id, child.id}
        assert "nested01" in manager.panel() and "nested01" not in manager.panel(history=True)
        child.state = "completed"
        assert not manager.current_agents()
        assert "nested01" in manager.panel(history=True)
        child.result, child.delivered = RunResult(outcome="completed"), False
        assert manager.current_agents() == {parent.id, child.id}  # Result needs delivery.
        child.delivered = True
        async def cleanup() -> None:
            await asyncio.Event().wait()
        blocker = asyncio.create_task(cleanup())
        child.task = blocker
        try:
            assert child.id in manager.current_agents()  # Core completion != cleanup finished.
        finally:
            blocker.cancel()
            with pytest.raises(asyncio.CancelledError):
                await blocker
    finally:
        manager.agents.clear()
        await root.close()


async def test_names_task_scope_and_delivery_survive_session_restore(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    root = root_conversation(tmp_path, monkeypatch)
    manager = root.coordinator
    assert manager is not None and root.events is not None and root.writer is not None

    class Model(ScriptedModel):
        def __init__(self, _: Any) -> None:
            super().__init__([reply(text="child complete")])

        async def close(self) -> None:
            pass

    monkeypatch.setattr(bootstrap, "ChatModel", Model)
    path = root.writer.path
    ids: list[str] = []
    try:
        for run, name in (("task-one", "执行流程调查"), ("task-two", "测试覆盖调查")):
            root.session.run_id = run
            await root.events("run_started", {"task": run})
            ident = (await manager.spawn("root", {
                "role": "scout", "task": "详细背景与交付要求\n不用于短标题",
                "task_name": name, "scope": "tests",
            }))["agent_id"]
            ids.append(ident)
            child = manager.agents[ident]
            assert child.task is not None
            await child.task
            assert child.state == "completed" and child.task_name == name
            assert child.task_run_id == run
            await manager.deliver_results("root")
            await root.events("run_finished", asdict(RunResult(outcome="completed")))
        assert ids[0][:8] not in manager.panel() and ids[1][:8] in manager.panel()
    finally:
        await root.close()
    restored = bootstrap.Conversation(tmp_path, root.config, ignore, home=tmp_path / "home",
                                      resume=path)
    try:
        tree = restored.coordinator
        assert tree is not None
        assert tree.current_run_id == "task-two"
        assert all(a.delivered for a in tree.agents.values())
        assert "执行流程调查" in tree.panel(history=True)
        assert "执行流程调查" not in tree.panel()
        assert "测试覆盖调查" in tree.panel()
        assert ids[0][:8] in tree.panel(history=True)
        # An actual task with no new children must not show the previous tree.
        result = await restored.turn("下一轮独立任务，不创建子代理")
        assert result.outcome == "completed"
        assert tree.current_run_id == restored.session.run_id != "task-two"
        assert "当前任务暂无子代理" in tree.panel()
        assert "历史 2 个" in tree.panel()
        restored.session.run_id = None  # Compaction/setup temporarily clear it.
        assert "当前任务暂无子代理" in tree.panel()
    finally:
        await restored.close()


async def test_legacy_title_fallback_is_bounded_and_redacted(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    root = root_conversation(tmp_path, monkeypatch)
    manager = root.coordinator
    assert manager is not None
    try:
        name = manager.short_task_name("\n测试覆盖调查\n背景：仓库与全部要求")
        assert name == "测试覆盖调查"
        assert manager.short_task_name(
            "背景：workspace 是项目目录\n目标：执行流程调查\n交付：报告") == "执行流程调查"
        assert manager.short_task_name("Background: repo; Goal: investigate tests") == (
            "investigate tests")
        assert cell_len(manager.short_task_name("超长中文标题" * 40)) <= 24
        monkeypatch.setattr(root, "secret_values", lambda: ("VERY_LONG_SECRET_1234567890",))
        assert "VERY_LONG" not in manager.short_task_name("VERY_LONG_SECRET_1234567890 工作")
        child = agent("legacy01", "old")
        child.task_name = ""
        child.assignment = "\n执行流程调查\n更多背景"
        manager.agents[child.id] = child
        assert "执行流程调查 · scout legacy01" in manager.panel(history=True)
        messages = [Message("user", "result",
                            input_id=f"agent-result:{child.id}:{child.execution_id}")]
        assert manager.result_in_messages(messages, child)
        child.execution_id = "another-execution"
        assert not manager.result_in_messages(messages, child)
    finally:
        manager.agents.clear()
        await root.close()
