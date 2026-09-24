from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from uuid import uuid4

import pytest
from langgraph.checkpoint.serde import jsonplus
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from pytest import MonkeyPatch
from typer.testing import CliRunner

from nexus.config.models import RuntimeConfig
from nexus.domain.agent_state import AgentState
from nexus.domain.exploration import WorkingContext
from nexus.domain.model import ModelChunk, ModelMessage, ModelResponse
from nexus.domain.persistence import RunStatus
from nexus.domain.runtime_events import ErrorOccurred, FinalResult, RunInterrupted, RuntimeStatus
from nexus.domain.skills import SelectedSkill, SkillLocation, SkillMetadata, SkillSource
from nexus.errors import ModelError, SessionError
from nexus.infrastructure.bootstrap import bootstrap_application
from nexus.infrastructure.checkpoint.postgres import _checkpoint_serializer
from nexus.infrastructure.database import DatabaseBootstrap
from nexus.infrastructure.model_gateway.openai_compatible import OpenAICompatibleModelGateway
from nexus.infrastructure.persistence import SqlAlchemySessionUnitOfWorkFactory
from nexus.interfaces.cli.app import app


class MockModelGateway:
    def __init__(self, response: str) -> None:
        self.response = response
        self.requests: list[list[ModelMessage]] = []

    async def complete(self, messages: Sequence[ModelMessage]) -> ModelResponse:
        self.requests.append(list(messages))
        return ModelResponse(content=self.response)

    async def stream(self, messages: Sequence[ModelMessage]) -> AsyncIterator[ModelChunk]:
        yield ModelChunk(content=self.response)


class FailingModelGateway(MockModelGateway):
    async def complete(self, messages: Sequence[ModelMessage]) -> ModelResponse:
        raise ModelError("Resume model failed.", retryable=True)


def test_checkpoint_serializer_reads_previous_msgpack_without_unregistered_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    location = SkillLocation(SkillSource.BUILTIN, "example/SKILL.md")
    metadata = SkillMetadata(
        "example", "Example", "Example description", "Example use", SkillSource.BUILTIN,
        0, "1.0.0", location,
    )
    state = AgentState(
        task="example",
        messages=[ModelMessage("user", "example")],
        run_id=str(uuid4()),
        session_id=None,
        status=RuntimeStatus.STARTED,
        context=WorkingContext(
            "example", (), (), ("example.py",), (), False,
            selected_skills=(SelectedSkill(metadata, "Example body\n", "Example reason"),),
        ),
    )
    previous = JsonPlusSerializer(allowed_msgpack_modules=True)
    configured = _checkpoint_serializer()
    stored = previous.dumps_typed(state)
    assert stored[0] == "msgpack"
    assert configured.dumps_typed(state) == stored
    with caplog.at_level(logging.WARNING, logger="langgraph.checkpoint.serde.jsonplus"):
        restored = configured.loads_typed(stored)
    assert isinstance(restored, AgentState)
    assert isinstance(restored.context, WorkingContext)
    assert restored.context.selected_skills[0].metadata == metadata
    assert restored.context.selected_skills[0].body == "Example body\n"
    assert restored.messages[0] == ModelMessage("user", "example")
    assert "Deserializing unregistered type" not in caplog.text


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_persisted_checkpoint_resumes_after_runtime_reconstruction(
    migrated_database_url: str,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(jsonplus, "_warned_unregistered_types", set())
    config = RuntimeConfig(database_url=migrated_database_url, semantic_enabled=False)
    gateway_a = MockModelGateway("must not execute before interruption")

    async with bootstrap_application(
        config,
        model_gateway=gateway_a,
        workspace_path=tmp_path,
        interrupt_before_model_response=True,
    ) as runtime_a:
        first_events = [
            event async for event in runtime_a.runtime.run("persist then resume")
        ]

    assert [type(event).__name__ for event in first_events] == [
        "TaskStarted",
        "RunInterrupted",
    ]
    interrupted = first_events[-1]
    assert isinstance(interrupted, RunInterrupted)
    assert interrupted.session_id is not None
    assert gateway_a.requests == []

    gateway_b = MockModelGateway("resumed from PostgreSQL")
    async with bootstrap_application(
        config,
        model_gateway=gateway_b,
        workspace_path=tmp_path,
    ) as runtime_b:
        resumed_events = [
            event async for event in runtime_b.runtime.resume(interrupted.session_id or "")
        ]

    assert [type(event).__name__ for event in resumed_events] == [
        "TaskStarted",
        "ModelCallStarted",
        "ModelCallFinished",
        "FinalResult",
    ]
    final = resumed_events[-1]
    assert isinstance(final, FinalResult)
    assert final.content == "resumed from PostgreSQL"
    assert gateway_b.requests == [
        [ModelMessage(role="user", content="persist then resume")]
    ]
    assert "Deserializing unregistered type" not in caplog.text
    assert "Blocked deserialization" not in caplog.text

    database = DatabaseBootstrap(migrated_database_url)
    factory = SqlAlchemySessionUnitOfWorkFactory(database.session_factory)
    try:
        async with factory() as unit_of_work:
            persisted = await unit_of_work.runs.get(final.run_id)
            turns = await unit_of_work.turns.list_by_session(final.session_id or "")
        assert persisted is not None
        assert persisted.status is RunStatus.COMPLETED
        assert persisted.final_outcome is not None
        assert persisted.final_outcome["content"] == "resumed from PostgreSQL"
        metrics = persisted.final_outcome["metrics"]
        assert isinstance(metrics, dict)
        assert metrics["llm_call_count"] == 1
        assert metrics["token_usage"]["usage_complete"] is False
        assert [turn.role for turn in turns] == ["user", "assistant"]
    finally:
        await database.close()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_resume_failure_persists_failed_and_is_not_resumable(
    migrated_database_url: str,
    tmp_path: Path,
) -> None:
    config = RuntimeConfig(database_url=migrated_database_url, semantic_enabled=False)
    async with bootstrap_application(
        config,
        model_gateway=MockModelGateway("unused"),
        workspace_path=tmp_path,
        interrupt_before_model_response=True,
    ) as runtime_a:
        interrupted_events = [
            event async for event in runtime_a.runtime.run("resume failure task")
        ]

    interrupted = interrupted_events[-1]
    assert isinstance(interrupted, RunInterrupted)
    assert interrupted.session_id is not None

    async with bootstrap_application(
        config,
        model_gateway=FailingModelGateway("unused"),
        workspace_path=tmp_path,
    ) as runtime_b:
        failed_events = [
            event async for event in runtime_b.runtime.resume(interrupted.session_id or "")
        ]

        assert [type(event).__name__ for event in failed_events] == [
            "TaskStarted",
            "ModelCallStarted",
            "ModelCallFinished",
            "ErrorOccurred",
        ]
        failure = failed_events[-1]
        assert isinstance(failure, ErrorOccurred)
        assert failure.code == "GRAPH_RESUME_ERROR"

        with pytest.raises(SessionError) as not_resumable:
            await runtime_b.session_service.resolve_resumable_run(
                interrupted.session_id or ""
            )
        assert not_resumable.value.code == "SESSION_NOT_RESUMABLE"

    database = DatabaseBootstrap(migrated_database_url)
    factory = SqlAlchemySessionUnitOfWorkFactory(database.session_factory)
    try:
        async with factory() as unit_of_work:
            persisted = await unit_of_work.runs.get(failure.run_id)
        assert persisted is not None
        assert persisted.status is RunStatus.FAILED
        assert persisted.final_outcome == {
            "error": {
                "code": "GRAPH_RESUME_ERROR",
                "message": "Nexus could not resume the persisted graph execution.",
            }
        }
    finally:
        await database.close()


@pytest.mark.postgres
def test_session_resume_cli_success_missing_and_cross_repository(
    migrated_database_url: str,
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    repository_a = tmp_path / "repository-a"
    repository_b = tmp_path / "repository-b"
    repository_a.mkdir()
    repository_b.mkdir()
    config = RuntimeConfig(database_url=migrated_database_url, semantic_enabled=False)
    session_id = asyncio.run(_seed_interrupted_run(config, repository_a))

    async def fake_complete(
        self: OpenAICompatibleModelGateway,
        messages: Sequence[ModelMessage],
    ) -> ModelResponse:
        return ModelResponse(content="CLI resumed result")

    monkeypatch.setattr(OpenAICompatibleModelGateway, "complete", fake_complete)
    environment = {
        "NEXUS_SEMANTIC_ENABLED": "false",
        "NEXUS_DATABASE_URL": migrated_database_url,
        "NEXUS_MODEL_NAME": "mock-model",
        "NEXUS_MODEL_API_KEY": "mock-secret",
    }
    runner = CliRunner()

    monkeypatch.chdir(repository_a)
    listed = runner.invoke(app, ["session", "list"], env=environment)
    assert listed.exit_code == 0
    assert session_id in listed.stdout
    assert "yes" in listed.stdout

    monkeypatch.chdir(repository_b)
    cross_repository = runner.invoke(
        app, ["session", "resume", session_id], env=environment
    )
    assert cross_repository.exit_code == 1
    assert "SESSION_REPOSITORY_MISMATCH" in cross_repository.output

    monkeypatch.chdir(repository_a)
    missing = runner.invoke(
        app, ["session", "resume", str(uuid4())], env=environment
    )
    assert missing.exit_code == 1
    assert "SESSION_NOT_FOUND" in missing.output

    resumed = runner.invoke(app, ["session", "resume", session_id], env=environment)
    assert resumed.exit_code == 0
    assert "Working" in resumed.stdout
    assert "Task started" not in resumed.stdout
    assert "CLI resumed result" in resumed.stdout

    completed = runner.invoke(app, ["session", "resume", session_id], env=environment)
    assert completed.exit_code == 1
    assert "SESSION_NOT_RESUMABLE" in completed.output


async def _seed_interrupted_run(config: RuntimeConfig, workspace: Path) -> str:
    async with bootstrap_application(
        config,
        model_gateway=MockModelGateway("unused"),
        workspace_path=workspace,
        interrupt_before_model_response=True,
    ) as application:
        events = [event async for event in application.runtime.run("CLI resume task")]
    interrupted = events[-1]
    assert isinstance(interrupted, RunInterrupted)
    assert interrupted.session_id is not None
    return interrupted.session_id
