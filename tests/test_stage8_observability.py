from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from nexus.app import bootstrap
from nexus.app.agent_history import tree_facts
from nexus.app.config import MultiAgentConfig
from nexus.app.session import SessionLog
from nexus.core.types import Emit, ExecutionContext, Json, RuntimeEvent, Session, Tool, ToolResult
from nexus.evaluation.agent_trace import render_agents
from nexus.evaluation.cases import CASE_IDS
from nexus.evaluation.history import build_history, collect_history
from nexus.tools.registry import EXEC_SPEC, default_tools
from tests.conftest import ScriptedModel, call, reply
from tests.test_conversation import config, ignore
from tests.test_evaluation_history import saved_run


def saved_tree(tmp_path: Path) -> Path:
    root = Session(tmp_path, session_id="root-session")
    child = Session(tmp_path, session_id="child")
    writer = SessionLog.create(root, "tree", {}, tmp_path / "audit")
    child_writer = SessionLog.create(child, "child", {},
        directory=writer.path.with_suffix("") / "subagents")
    def emit(log: SessionLog, ident: str, kind: str, second: int, data: Json) -> None:
        log.append(RuntimeEvent(kind, f"2026-11-03T00:00:{second:02d}+00:00", ident, ident, data))
    metadata = {"agent_id": "child", "parent_id": "root", "role": "worker", "depth": 1,
                "assignment": "test <script>alert(1)</script>", "state": "running",
                "model": "test-child", "reasoning_effort": "high",
                "log_path": "root-session/subagents/child.jsonl"}
    emit(writer, "root-session", "run_started", 0, {"execution_id": "root-exec"})
    emit(writer, "root-session", "agent_created", 1, metadata)
    emit(child_writer, "child", "run_started", 1, {"execution_id": "child-exec",
        "agent_id": "child", "parent_agent_id": "root", "root_execution_id": "root-exec",
        "parent_execution_id": "root-exec", "root_run_id": "root-session"})
    for log, ident, offset, tokens in [
        (writer, "root-session", 0, 10), (child_writer, "child", 1, 20),
    ]:
        emit(log, ident, "model_started", offset + 1, {"request_id": ident, "attempt": 1})
        emit(log, ident, "model_finished", offset + 3, {"usage": {
            "input_tokens": tokens, "output_tokens": 2, "total_tokens": tokens + 2,
            "cached_input_tokens": 5,
        }})
    child_writer.append(RuntimeEvent("message", "2026-11-03T00:00:05+00:00", "child", "child",
        {"role": "assistant", "content": "public result"}),
        {"fields": {"reasoning_content": "PRIVATE_CONTINUATION"}})
    emit(child_writer, "child", "run_finished", 5, {"outcome": "completed", "duration_ms": 4000})
    emit(writer, "root-session", "agent_state", 5, {**metadata, "state": "completed"})
    emit(writer, "root-session", "run_finished", 6, {"outcome": "completed", "duration_ms": 6000})
    child_writer.close()
    writer.close()
    return writer.path


def test_saved_tree_statistics_and_safe_parallel_report(tmp_path: Path) -> None:
    facts = tree_facts(saved_tree(tmp_path))
    assert facts["usage"]["total_tokens"] == 34
    assert facts["usage"]["input_tokens"] == 30
    assert facts["duration_ms"] == 6000  # Never sum the child's overlapping 4000 ms.
    assert [a["state"] for a in facts["agents"]] == ["completed", "completed"]
    html = render_agents(facts, "case")
    assert 'class="agent-filter"' in html and 'class="agent-lane"' in html
    assert "&lt;script&gt;" in html and "<script>alert(1)</script>" not in html
    assert "PRIVATE_CONTINUATION" not in html
    assert "unknown" in html  # No invented price.
    assert render_agents({"delegated": False}, "old") == ""


def test_history_keeps_child_models_and_actual_delegation(tmp_path: Path) -> None:
    facts = tree_facts(saved_tree(tmp_path))
    directory = saved_run(tmp_path, "run", [CASE_IDS[0]])
    summary_path = directory / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["cases"][0]["collaboration"] = facts
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    manifest = directory / "manifest.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["multi_agent"] = {"enabled": True}
    manifest.write_text(json.dumps(data), encoding="utf-8")
    history = collect_history(tmp_path)
    case = history["runs"][0]["cases"][0]
    assert case["delegated"] and case["agents"][1]["model"] == "test-child"
    assert "events" not in case["agents"][1]
    page = build_history(tmp_path).read_text(encoding="utf-8")
    assert "agents-history" in page and "实际委派" in page


async def test_custom_environment_factory_is_inherited_without_host_shell(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    sessions: list[Session] = []
    called: list[str] = []
    def registry(session: Session) -> dict[str, Tool]:
        sessions.append(session)
        async def execute(args: Json, context: ExecutionContext, emit: Emit) -> ToolResult:
            called.append(args["command"])
            return ToolResult(context.call_id, True, {"environment": "isolated-test-container"})
        return default_tools(session, {"exec_command": Tool(EXEC_SPEC, execute)})

    class Model(ScriptedModel):
        def __init__(self, _: Any) -> None:
            super().__init__([reply(call("exec_command", {"command": "container only"})),
                              reply(text="done")])
        async def close(self) -> None:
            pass

    monkeypatch.setattr(bootstrap, "ChatModel", Model)
    cfg = replace(config(monkeypatch), multi_agent=MultiAgentConfig(enabled=True))
    root = bootstrap.Conversation(tmp_path, cfg, ignore, home=tmp_path / "home", registry=registry,
                                  environment_display=("Linux", "/workspace"))
    root.ensure_events("test")
    manager = root.coordinator
    assert manager is not None
    try:
        ident = (await manager.spawn("root", {"role": "worker", "task": "test", "scope": "a"}))[
            "agent_id"]
        task = manager.agents[ident].task
        assert task is not None
        await task
        assert called == ["container only"]
        assert len(sessions) == 2 and sessions[0] is not sessions[1]
        assert manager.owner(ident).environment_display == ("Linux", "/workspace")
        assert manager.owner(ident).memory is None
    finally:
        await root.close()
