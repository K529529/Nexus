from __future__ import annotations

import asyncio
import io
from pathlib import Path
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.application import create_app_session
from prompt_toolkit.data_structures import Size
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.output.vt100 import Vt100_Output
from rich.console import Console

from nexus.app.agent_view import AgentUsage
from nexus.app.cli import agent_command
from nexus.app.input import TaskInput
from nexus.app.tui import Transcript
from nexus.core.types import Json, RuntimeEvent
from tests.test_cli_ux import until
from tests.test_input_v02 import editor_session
from tests.test_stage8_collaboration import root_conversation


async def test_child_output_is_separate_details_and_selected_guidance(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    root = root_conversation(tmp_path, monkeypatch)
    output = io.StringIO()
    root.consumer = Transcript(Console(file=output, width=100, no_color=True))
    manager = root.coordinator
    assert manager is not None
    try:
        ident = (await manager.spawn("root", {"role": "worker", "task": "work", "scope": "a"}))[
            "agent_id"]
        await until(lambda: manager.agents[ident].conversation is not None)
        child = manager.owner(ident)
        assert child.events is not None
        await child.events("message", {"role": "assistant", "content": "CHILD_DETAIL_ONLY"})
        assert "CHILD_DETAIL_ONLY" not in output.getvalue()
        with create_app_session(output=DummyOutput()):
            editor = TaskInput(PromptSession(output=DummyOutput()))
            assert await agent_command(root, editor, f"/agent {ident[:8]}")
            assert editor.target_agent == ident
            assert await agent_command(root, editor, f"/agent detail {ident[:8]}")
            assert "CHILD_DETAIL_ONLY" in output.getvalue()
            decision = asyncio.create_task(manager.report(ident, "choose A or B?", True))
            await until(lambda: manager.agents[ident].state == "waiting_decision")
            assert "DECISION NEEDED" in output.getvalue()
            assert (await manager.user_message(editor.target_agent, "A"))["received"]
            assert (await decision)["answer"] == "A"
            assert any("User guided child" in m.text for m in root.inbox.guides())
            await agent_command(root, editor, "/agent root")
            assert editor.target_agent == "root"
    finally:
        await root.close()


async def test_live_tree_redraw_preserves_editable_draft(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    root = root_conversation(tmp_path, monkeypatch)
    manager = root.coordinator
    assert manager is not None
    output = io.StringIO()
    terminal = Vt100_Output(output, lambda: Size(rows=30, columns=100), term="xterm-256color")
    try:
        ident = (await manager.spawn("root", {"role": "worker", "task": "work", "scope": "a"}))[
            "agent_id"]
        await until(lambda: manager.agents[ident].conversation is not None)
        async with editor_session(terminal) as (editor, prompt, pipe, pending):
            editor.agent_panel = lambda: manager.panel()
            pipe.send_text("draft 中文")
            await until(lambda: prompt.default_buffer.text == "draft 中文")
            prompt.app.invalidate()
            await until(lambda: "Agents" in output.getvalue())
            manager.agents[ident].state = "waiting_decision"
            prompt.app.invalidate()
            def screen_text() -> str:
                screen = prompt.app.renderer.last_rendered_screen
                return "\n".join("".join(c.char for _, c in sorted(row.items()))
                                 for row in screen.data_buffer.values()) if screen else ""
            await until(lambda: "waiting_decision" in screen_text())
            assert prompt.default_buffer.text == "draft 中文"
            pipe.send_text("\r")
            assert await pending == "draft 中文"
    finally:
        await root.close()


def test_usage_counts_failed_compaction_attempts_and_marks_missing() -> None:
    ledger = AgentUsage()
    events: list[tuple[str, Json]] = [
        ("model_started", {}),
        ("model_finished", {"usage": {"input_tokens": 10, "output_tokens": 2,
                                     "total_tokens": 12, "cached_input_tokens": 8}}),
        ("model_started", {"purpose": "compaction"}),
        ("model_finished", {"error": "retry", "usage": {"input_tokens": 5, "output_tokens": 1,
                                      "total_tokens": 6, "cached_input_tokens": 0}}),
    ]
    for kind, data in events:
        ledger.observe(RuntimeEvent(kind, "", "s", "r", data))
    assert ledger.public()["total_tokens"] == 18
    assert ledger.public()["input_tokens"] == 15  # Cache is already included, not added.
    ledger.observe(RuntimeEvent("model_started", "", "s", "r", {}))
    assert ledger.public()["total_tokens"] is None
    assert ledger.public()["total_tokens_known"] == 18
    assert ledger.public()["cost"] is None
