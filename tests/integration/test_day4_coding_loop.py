from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import cast

import pytest

from nexus.application.diff_service import patch_digest
from nexus.application.runtime import NexusRuntime
from nexus.config.models import RuntimeConfig
from nexus.domain.agent_state import AgentState
from nexus.domain.model import ModelChunk, ModelMessage, ModelResponse
from nexus.domain.planning import PlanApprovalResumeInput, TerminalStatus
from nexus.domain.runtime_events import (
    AgentStepCompleted,
    ApprovalRequested,
    ContextBuilt,
    FinalResult,
    PlanCreated,
    RunInterrupted,
    ValidationFinished,
)
from nexus.domain.tooling import ApprovalDecision
from nexus.infrastructure.bootstrap import bootstrap_application
from nexus.infrastructure.bootstrap.composition import index_repository
from nexus.infrastructure.graph.day4_runtime import Day4LangGraphRuntime
from tests.fixtures.embedding import FixtureEmbedding


@pytest.fixture(autouse=True)
def prefer_current_test_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Use the same pytest installation for the approved validation command."""
    monkeypatch.setenv("PATH", str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"])


class CodingLoopGateway:
    def __init__(self, phase: str = "all") -> None:
        plan: dict[str, object] = {
            "completion_requirement": "WORKSPACE_CHANGE_REQUIRED",
            "rationale_summary": "Update the target and run its focused test.",
            "steps": [
                {
                    "description": "Update alpha value",
                    "tool_name": "edit_file",
                    "target_paths": ["alpha.py"],
                    "command_argv": None,
                    "command_cwd": None,
                },
                {
                    "description": "TEST: run focused alpha test",
                    "tool_name": "shell",
                    "target_paths": [],
                    "command_argv": ["pytest", "-q", "test_alpha.py"],
                    "command_cwd": ".",
                },
            ],
        }
        edit: dict[str, object] = {
            "kind": "TOOL_ACTION",
            "summary": "Apply the approved alpha change.",
            "action": {
                "tool_name": "edit_file",
                "arguments": {
                    "path": "alpha.py",
                    "old_str": "VALUE = 1",
                    "new_str": "VALUE = 2",
                },
            },
        }
        ready: dict[str, object] = {
            "kind": "TASK_READY",
            "summary": "The approved edit is ready for validation.",
            "action": None,
        }
        no_skill: dict[str, object] = {
            "selected_skill_ids": [],
            "selection_reason_summary": "No builtin Skill is relevant to this fixture.",
        }
        responses: dict[str, list[dict[str, object]]] = {
            "all": [no_skill, plan, edit, ready],
            "plan": [no_skill, plan],
            "execute": [edit, ready],
        }
        self._responses = [json.dumps(item) for item in responses[phase]]

    async def complete(self, messages: Sequence[ModelMessage]) -> ModelResponse:
        del messages
        return ModelResponse(self._responses.pop(0))

    async def stream(
        self,
        messages: Sequence[ModelMessage],
    ) -> AsyncIterator[ModelChunk]:
        del messages
        if False:
            yield ModelChunk("")


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "semantic_enabled",
    [
        pytest.param(None, id="default-lexical-without-embedding-config"),
        pytest.param(False, id="explicit-lexical"),
        pytest.param(True, id="semantic-opt-in"),
    ],
)
async def test_realistic_day4_coding_loop_interrupts_edits_validates_and_diffs(
    migrated_database_url: str,
    tmp_path: Path,
    semantic_enabled: bool | None,
) -> None:
    git = shutil.which("git")
    if git is None:
        pytest.skip("Git is unavailable for the Day 4 fixture repository.")
    (tmp_path / "alpha.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "test_alpha.py").write_text(
        "from alpha import VALUE\n\ndef test_value():\n    assert VALUE == 2\n",
        encoding="utf-8",
    )
    (tmp_path / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n", encoding="utf-8")
    _git(git, tmp_path, "init")
    _git(git, tmp_path, "config", "core.autocrlf", "false")
    _git(git, tmp_path, "add", ".gitignore", "alpha.py", "test_alpha.py")
    _git(
        git,
        tmp_path,
        "-c",
        "user.name=Nexus Test",
        "-c",
        "user.email=nexus@example.invalid",
        "commit",
        "-m",
        "fixture",
    )
    config = (
        RuntimeConfig(database_url=migrated_database_url, model_name="fixture-model")
        if semantic_enabled is None
        else RuntimeConfig(
            database_url=migrated_database_url,
            model_name="fixture-model",
            semantic_enabled=semantic_enabled,
        )
    )
    assert config.semantic_enabled is (semantic_enabled is True)
    embedding = FixtureEmbedding()
    if semantic_enabled is True:
        await index_repository(config, workspace_path=tmp_path, embedding_gateway=embedding)

    async with bootstrap_application(
        config,
        model_gateway=CodingLoopGateway(),
        workspace_path=tmp_path,
        embedding_gateway=embedding if semantic_enabled is True else None,
    ) as application:
        interrupted_events = [
            event
            async for event in application.runtime.run(
                "Update alpha.py VALUE so the focused test passes"
            )
        ]
        interrupted = interrupted_events[-1]
        assert isinstance(interrupted, RunInterrupted)
        assert interrupted.session_id is not None
        assert any(isinstance(event, PlanCreated) for event in interrupted_events)
        context_event = next(
            event for event in interrupted_events if isinstance(event, ContextBuilt)
        )
        assert context_event.semantic_retrieval_used == (semantic_enabled is True)
        assert context_event.selected_chunk_count > 0
        assert any(isinstance(event, ApprovalRequested) for event in interrupted_events)

        resumed_events = [
            event
            async for event in application.runtime.resume(
                interrupted.session_id,
                resume_input=PlanApprovalResumeInput(
                    ApprovalDecision.APPROVED,
                    "Fixture scope approved.",
                ),
            )
        ]

    assert any(isinstance(event, ValidationFinished) for event in resumed_events)
    assert any(isinstance(event, AgentStepCompleted) for event in resumed_events)
    assert all(
        event.guard_reason is None
        for event in resumed_events
        if isinstance(event, AgentStepCompleted)
    )
    final = resumed_events[-1]
    assert isinstance(final, FinalResult)
    assert final.terminal_status is TerminalStatus.SUCCEEDED
    assert (tmp_path / "alpha.py").read_text(encoding="utf-8") == "VALUE = 2\n"
    assert final.validation_result is not None
    assert final.validation_result.status.value == "PASS"
    assert final.diff is not None and "+VALUE = 2" in final.diff
    assert final.includes_preexisting_changes is False


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize("semantic_enabled", [False, True])
async def test_day4_reconstructs_process_and_preserves_checkpoint_evidence(
    migrated_database_url: str,
    tmp_path: Path,
    semantic_enabled: bool,
) -> None:
    git = shutil.which("git")
    if git is None:
        pytest.skip("Git is unavailable for the Day 4 fixture repository.")
    (tmp_path / "alpha.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "test_alpha.py").write_text(
        "from alpha import VALUE\n\ndef test_value():\n    assert VALUE == 2\n",
        encoding="utf-8",
    )
    (tmp_path / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n", encoding="utf-8")
    _git(git, tmp_path, "init")
    _git(git, tmp_path, "config", "core.autocrlf", "false")
    _git(git, tmp_path, "add", ".gitignore", "alpha.py", "test_alpha.py")
    _git(
        git,
        tmp_path,
        "-c",
        "user.name=Nexus Test",
        "-c",
        "user.email=nexus@example.invalid",
        "commit",
        "-m",
        "fixture",
    )
    config = RuntimeConfig(
        database_url=migrated_database_url,
        model_name="fixture-model",
        semantic_enabled=semantic_enabled,
    )
    embedding = FixtureEmbedding()
    if semantic_enabled:
        await index_repository(config, workspace_path=tmp_path, embedding_gateway=embedding)

    async with bootstrap_application(
        config,
        model_gateway=CodingLoopGateway("plan"),
        workspace_path=tmp_path,
        embedding_gateway=embedding,
    ) as application_a:
        interrupted_events = [
            event
            async for event in application_a.runtime.run(
                "Update alpha.py VALUE so the focused test passes"
            )
        ]
        interrupted = interrupted_events[-1]
        assert isinstance(interrupted, RunInterrupted)
        assert interrupted.session_id is not None
        run = await application_a.session_service.resolve_resumable_run(interrupted.session_id)
        state_a = await _checkpoint_state(application_a.runtime, run.graph_thread_id)

        assert state_a.pending_plan_approval is not None
        assert state_a.plan is not None
        assert state_a.tool_call_count > 0
        assert state_a.tool_results
        assert state_a.llm_call_count == 2
        assert state_a.step_count == 0
        assert state_a.context is not None
        assert state_a.context.semantic_retrieval_used == semantic_enabled
        assert state_a.context.retrieved_candidates
        prior_tool_results = state_a.tool_results
        prior_scope = state_a.plan.authorization_scope

    async with bootstrap_application(
        config,
        model_gateway=CodingLoopGateway("execute"),
        workspace_path=tmp_path,
        embedding_gateway=embedding,
    ) as application_b:
        resumed_events = [
            event
            async for event in application_b.runtime.resume(
                interrupted.session_id,
                resume_input=PlanApprovalResumeInput(
                    ApprovalDecision.APPROVED,
                    "Fixture scope approved after process reconstruction.",
                ),
            )
        ]
        state_b = await _checkpoint_state(application_b.runtime, run.graph_thread_id)

        assert state_b.approved_plan is not None
        assert state_b.context == state_a.context
        assert state_b.approved_plan.authorization_scope == prior_scope
        assert state_b.tool_call_count > state_a.tool_call_count
        assert state_b.llm_call_count == state_a.llm_call_count + 2
        assert state_b.step_count == state_a.step_count + 2
        assert [(item.tool_name, item.success) for item in state_b.observations] == [
            ("edit_file", True)
        ]
        assert state_b.observations[0].target_path == "alpha.py"
        assert state_b.replan_count == state_a.replan_count
        assert state_b.repair_count == state_a.repair_count
        assert state_b.tool_results[: len(prior_tool_results)] == prior_tool_results
        assert len(state_b.tool_results) > len(prior_tool_results)

    assert (tmp_path / "alpha.py").read_text(encoding="utf-8") == "VALUE = 2\n"
    final = resumed_events[-1]
    assert isinstance(final, FinalResult)
    assert final.terminal_status is TerminalStatus.SUCCEEDED
    assert final.validation_result is not None
    assert final.validation_result.status.value == "PASS"
    assert final.diff is not None and "+VALUE = 2" in final.diff


async def _checkpoint_state(runtime: NexusRuntime, thread_id: str) -> AgentState:
    graph_runtime = cast(
        Day4LangGraphRuntime,
        runtime._graph_runtime,  # noqa: SLF001
    )
    snapshot = await graph_runtime._graph.aget_state(  # noqa: SLF001
        {"configurable": {"thread_id": thread_id}}
    )
    return AgentState(**snapshot.values)


def _git(executable: str, cwd: Path, *arguments: str) -> None:
    subprocess.run(
        [executable, *arguments],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


class QueueCodingGateway:
    def __init__(self, *responses: dict[str, object]) -> None:
        self.responses = [json.dumps(response) for response in responses]
        self.calls: list[tuple[ModelMessage, ...]] = []

    async def complete(self, messages: Sequence[ModelMessage]) -> ModelResponse:
        self.calls.append(tuple(messages))
        return ModelResponse(self.responses.pop(0))

    async def stream(self, messages: Sequence[ModelMessage]) -> AsyncIterator[ModelChunk]:
        del messages
        if False:
            yield ModelChunk("")


def _coding_repository(tmp_path: Path, expected: int) -> None:
    git = shutil.which("git")
    if git is None:
        pytest.skip("Git is unavailable")
    (tmp_path / "alpha.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "test_alpha.py").write_text(
        f"from alpha import VALUE\n\ndef test_value():\n    assert VALUE == {expected}\n",
        encoding="utf-8",
    )
    (tmp_path / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n", encoding="utf-8")
    _git(git, tmp_path, "init")
    _git(git, tmp_path, "config", "core.autocrlf", "false")
    _git(git, tmp_path, "add", ".gitignore", "alpha.py", "test_alpha.py")
    _git(
        git,
        tmp_path,
        "-c",
        "user.name=Nexus Test",
        "-c",
        "user.email=nexus@example.invalid",
        "commit",
        "-m",
        "fixture",
    )


def _test_step() -> dict[str, object]:
    return {
        "description": "TEST: run focused alpha test",
        "tool_name": "shell",
        "target_paths": [],
        "command_argv": ["pytest", "-q", "test_alpha.py"],
        "command_cwd": ".",
    }


def _edit_step() -> dict[str, object]:
    return {
        "description": "Edit alpha",
        "tool_name": "edit_file",
        "target_paths": ["alpha.py"],
        "command_argv": None,
        "command_cwd": None,
    }


def _edit_action(old: int, new: int) -> dict[str, object]:
    return {
        "kind": "TOOL_ACTION",
        "summary": "Apply the approved alpha edit.",
        "action": {
            "tool_name": "edit_file",
            "arguments": {
                "path": "alpha.py",
                "old_str": f"VALUE = {old}",
                "new_str": f"VALUE = {new}",
            },
        },
    }


def _ready_action() -> dict[str, object]:
    return {"kind": "TASK_READY", "summary": "Ready for validation.", "action": None}


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_shell_mutation_is_detected_without_changed_file_ledger(
    migrated_database_url: str, tmp_path: Path
) -> None:
    _coding_repository(tmp_path, expected=2)
    script = (
        "from pathlib import Path; Path('alpha.py').write_text('VALUE = 2\\n', encoding='utf-8')"
    )
    shell_step = {
        "description": "Update alpha with approved Python",
        "tool_name": "shell",
        "target_paths": [],
        "command_argv": ["python", "-c", script],
        "command_cwd": ".",
    }
    gateway = QueueCodingGateway(
        {"selected_skill_ids": [], "selection_reason_summary": "No Skill needed."},
        {
            "completion_requirement": "WORKSPACE_CHANGE_REQUIRED",
            "rationale_summary": "Update alpha and verify it.",
            "steps": [shell_step, _test_step()],
        },
        {
            "kind": "TOOL_ACTION",
            "summary": "Run approved update.",
            "action": {
                "tool_name": "shell",
                "arguments": {"argv": ["python", "-c", script], "cwd": "."},
            },
        },
        _ready_action(),
    )
    config = RuntimeConfig(database_url=migrated_database_url, model_name="fixture-model")
    async with bootstrap_application(
        config, model_gateway=gateway, workspace_path=tmp_path
    ) as application:
        interrupted = [
            event async for event in application.runtime.run("Update alpha with approved Python")
        ][-1]
        assert isinstance(interrupted, RunInterrupted)
        assert interrupted.session_id is not None
        run = await application.session_service.resolve_resumable_run(interrupted.session_id)
        events = [
            event
            async for event in application.runtime.resume(
                interrupted.session_id,
                resume_input=PlanApprovalResumeInput(
                    ApprovalDecision.APPROVED, "Fixture approved."
                ),
            )
        ]
        state = await _checkpoint_state(application.runtime, run.graph_thread_id)
    final = events[-1]
    assert isinstance(final, FinalResult)
    assert final.terminal_status is TerminalStatus.SUCCEEDED
    assert final.changed_files == ()
    assert final.diff is not None and "+VALUE = 2" in final.diff
    assert state.validated_workspace_digest == patch_digest(final.diff)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_full_repair_uses_assertion_evidence_and_final_workspace_patch(
    migrated_database_url: str, tmp_path: Path
) -> None:
    _coding_repository(tmp_path, expected=3)
    gateway = QueueCodingGateway(
        {"selected_skill_ids": [], "selection_reason_summary": "No Skill needed."},
        {
            "completion_requirement": "WORKSPACE_CHANGE_REQUIRED",
            "rationale_summary": "Edit alpha and verify it.",
            "steps": [_edit_step(), _test_step()],
        },
        _edit_action(1, 2),
        _ready_action(),
        {"failure_summary": "The assertion requires VALUE 3.", "steps": [_edit_step()]},
        _edit_action(2, 3),
        _ready_action(),
    )
    config = RuntimeConfig(database_url=migrated_database_url, model_name="fixture-model")
    async with bootstrap_application(
        config, model_gateway=gateway, workspace_path=tmp_path
    ) as application:
        interrupted = [event async for event in application.runtime.run("Set alpha VALUE to 3")][-1]
        assert isinstance(interrupted, RunInterrupted)
        assert interrupted.session_id is not None
        run = await application.session_service.resolve_resumable_run(interrupted.session_id)
        events = [
            event
            async for event in application.runtime.resume(
                interrupted.session_id,
                resume_input=PlanApprovalResumeInput(
                    ApprovalDecision.APPROVED, "Fixture approved."
                ),
            )
        ]
        state = await _checkpoint_state(application.runtime, run.graph_thread_id)
    final = events[-1]
    assert isinstance(final, FinalResult)
    assert final.terminal_status is TerminalStatus.SUCCEEDED
    assert final.validation_result is not None and final.validation_result.status.value == "PASS"
    assert final.diff is not None and "+VALUE = 3" in final.diff
    assert state.baseline_workspace_digest == patch_digest("")
    assert state.validated_workspace_digest == patch_digest(final.diff)
    assert state.approved_plan is not None
    assert state.approved_plan.authorization_scope.allowed_write_actions == (
        ("edit_file", "alpha.py"),
    )
    assert (tmp_path / "alpha.py").read_text(encoding="utf-8") == "VALUE = 3\n"
    assert [item.path for item in final.changed_files] == ["alpha.py"]
    assert len(gateway.calls) == 7
    assert any("AssertionError" in message.content for message in gateway.calls[4])


def _investigative_plan() -> dict[str, object]:
    return {
        "completion_requirement": "WORKSPACE_CHANGE_REQUIRED",
        "rationale_summary": "Inspect the bug before requesting edit authority.",
        "steps": [
            {
                "description": f"Read {path}",
                "tool_name": "read_file",
                "target_paths": [],
                "command_argv": None,
                "command_cwd": None,
            }
            for path in ("alpha.py", "test_alpha.py")
        ],
    }


def _read_action(path: str) -> dict[str, object]:
    return {
        "kind": "TOOL_ACTION",
        "summary": f"Read {path}.",
        "action": {"tool_name": "read_file", "arguments": {"path": path}},
    }


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_investigative_bug_fix_plan_cannot_finish_without_workspace_change(
    migrated_database_url: str, tmp_path: Path
) -> None:
    _coding_repository(tmp_path, expected=2)
    gateway = QueueCodingGateway(
        {"selected_skill_ids": [], "selection_reason_summary": "No Skill needed."},
        _investigative_plan(),
        _read_action("alpha.py"),
        _read_action("test_alpha.py"),
        _ready_action(),
    )
    config = RuntimeConfig(database_url=migrated_database_url, model_name="fixture-model")
    async with bootstrap_application(
        config, model_gateway=gateway, workspace_path=tmp_path
    ) as application:
        interrupted = [event async for event in application.runtime.run("Fix alpha bug")][-1]
        assert isinstance(interrupted, RunInterrupted)
        assert interrupted.session_id is not None
        events = [event async for event in application.runtime.resume(
            interrupted.session_id,
            resume_input=PlanApprovalResumeInput(ApprovalDecision.APPROVED, "Investigate first."),
        )]
    final = events[-1]
    assert isinstance(final, FinalResult)
    assert final.terminal_status is TerminalStatus.FAILED_VALIDATION_UNKNOWN
    assert final.validation_result is not None
    assert final.validation_result.status.value == "UNKNOWN"
    assert (tmp_path / "alpha.py").read_text(encoding="utf-8") == "VALUE = 1\n"


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_investigative_bug_fix_replans_to_approved_edit_with_frozen_requirement(
    migrated_database_url: str, tmp_path: Path
) -> None:
    _coding_repository(tmp_path, expected=2)
    gateway = QueueCodingGateway(
        {"selected_skill_ids": [], "selection_reason_summary": "No Skill needed."},
        _investigative_plan(),
        _read_action("alpha.py"),
        _read_action("test_alpha.py"),
        _edit_action(1, 2),
        {
            "completion_requirement": "WORKSPACE_CHANGE_REQUIRED",
            "rationale_summary": "Edit alpha after investigation and validate.",
            "steps": [_edit_step(), _test_step()],
        },
        _edit_action(1, 2),
        _ready_action(),
    )
    config = RuntimeConfig(database_url=migrated_database_url, model_name="fixture-model")
    async with bootstrap_application(
        config, model_gateway=gateway, workspace_path=tmp_path
    ) as application:
        first = [event async for event in application.runtime.run("Fix alpha bug")][-1]
        assert isinstance(first, RunInterrupted)
        assert first.session_id is not None
        session_id = first.session_id
        run = await application.session_service.resolve_resumable_run(session_id)
        second_events = [event async for event in application.runtime.resume(
            session_id,
            resume_input=PlanApprovalResumeInput(ApprovalDecision.APPROVED, "Investigate first."),
        )]
        assert isinstance(second_events[-1], RunInterrupted)
        final_events = [event async for event in application.runtime.resume(
            session_id,
            resume_input=PlanApprovalResumeInput(ApprovalDecision.APPROVED, "Edit alpha."),
        )]
        state = await _checkpoint_state(application.runtime, run.graph_thread_id)
    final = final_events[-1]
    assert isinstance(final, FinalResult)
    assert final.terminal_status is TerminalStatus.SUCCEEDED
    assert final.diff is not None and "+VALUE = 2" in final.diff
    assert state.plan is not None
    assert state.plan.completion_requirement.value == "WORKSPACE_CHANGE_REQUIRED"
    assert state.plan_history
    assert all(
        plan.completion_requirement.value == "WORKSPACE_CHANGE_REQUIRED"
        for plan in state.plan_history
    )
    assert any(
        result.error is not None and result.error.code == "PLAN_SCOPE_DENIED"
        for result in state.tool_results
    )
    assert (tmp_path / "alpha.py").read_text(encoding="utf-8") == "VALUE = 2\n"


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_pure_read_only_explanation_ignores_preexisting_binary_patch(
    migrated_database_url: str, tmp_path: Path
) -> None:
    _coding_repository(tmp_path, expected=1)
    (tmp_path / "dataset.bin").write_bytes(b"\x00untracked binary")
    gateway = QueueCodingGateway(
        {"selected_skill_ids": [], "selection_reason_summary": "No Skill needed."},
        {
            "completion_requirement": "WORKSPACE_CHANGE_NOT_REQUIRED",
            "rationale_summary": "Read and explain alpha.",
            "steps": [{
                "description": "Read alpha.py",
                "tool_name": "read_file",
                "target_paths": [],
                "command_argv": None,
                "command_cwd": None,
            }],
        },
        _read_action("alpha.py"),
        {"kind": "TASK_READY", "summary": "alpha.py defines VALUE as 1.", "action": None},
    )
    config = RuntimeConfig(database_url=migrated_database_url, model_name="fixture-model")
    async with bootstrap_application(
        config, model_gateway=gateway, workspace_path=tmp_path
    ) as application:
        interrupted = [event async for event in application.runtime.run("Explain alpha.py")][-1]
        assert isinstance(interrupted, RunInterrupted)
        assert interrupted.session_id is not None
        run = await application.session_service.resolve_resumable_run(interrupted.session_id)
        events = [event async for event in application.runtime.resume(
            interrupted.session_id,
            resume_input=PlanApprovalResumeInput(ApprovalDecision.APPROVED, "Explain alpha."),
        )]
        state = await _checkpoint_state(application.runtime, run.graph_thread_id)
    final = events[-1]
    assert isinstance(final, FinalResult)
    assert final.terminal_status is TerminalStatus.SUCCEEDED
    assert final.diff == ""
    assert final.includes_preexisting_changes is True
    assert "VALUE as 1" in final.content
    assert state.baseline_workspace_digest is None
    assert state.validated_workspace_digest is None
    assert not any(
        result.tool_name == "read_file"
        and result.output is not None
        and result.output.get("path") == "dataset.bin"
        for result in state.tool_results
    )
