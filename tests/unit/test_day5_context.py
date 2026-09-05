from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from pydantic import SecretStr, ValidationError

from nexus.application.planning import JsonAgentDecisionAdapter, ModelPlanner
from nexus.application.tool_runtime import ToolRuntime
from nexus.config import load_runtime_config
from nexus.config.models import RuntimeConfig
from nexus.context.chunking import LineWindowChunker, estimated_tokens, text_hash
from nexus.context.manager import BoundedContextManager, context_payload, model_input_tokens
from nexus.context.retrieval import ToolRepositoryAccess, lexical_terms, rrf_merge
from nexus.domain.agent_decision import AgentDecisionRequest, Observation
from nexus.domain.context import ContextBudget, ContextCandidate, RetrievalSource
from nexus.domain.exploration import RepositoryInstruction, SelectedFileContext, WorkingContext
from nexus.domain.model import ModelMessage, ModelResponse
from nexus.domain.persistence import SessionTurn
from nexus.domain.planning import Plan, PlanKind
from nexus.domain.ports.context import ContextProvider
from nexus.domain.ports.model_gateway import ModelGateway
from nexus.domain.ports.planning import PlanningRequest
from nexus.errors import ConfigurationError, ContextError
from nexus.infrastructure.repository_files import RepositoryFiles


@pytest.mark.parametrize(
    ("lines", "starts"),
    [
        (0, []),
        (1, [1]),
        (119, [1]),
        (120, [1]),
        (121, [1, 101]),
        (201, [1, 101, 201]),
        (220, [1, 101, 201]),
        (301, [1, 101, 201, 301]),
    ],
)
def test_frozen_windows(lines: int, starts: list[int]) -> None:
    content = "".join(f"line {i}\n" for i in range(1, lines + 1))
    chunks = LineWindowChunker().chunk(
        file_path="src/test.py", language="python", content=content, file_hash=text_hash(content)
    )
    assert [c.start_line for c in chunks] == starts
    for chunk in chunks:
        assert chunk.end_line == min(chunk.start_line + 119, lines)
        assert len(chunk.content.splitlines()) <= 120
        assert chunk.content_hash == text_hash(chunk.content)
        assert chunk.file_hash == text_hash(content)
    if len(chunks) > 1:
        assert chunks[0].content.splitlines()[100:120] == chunks[1].content.splitlines()[:20]


def test_hashes_normalize_newlines_and_bom() -> None:
    assert text_hash("\ufeffalpha\r\nbeta\r") == text_hash("alpha\nbeta\n")


def candidate(name: str, rank: int, source: RetrievalSource) -> ContextCandidate:
    chunk = LineWindowChunker().chunk(
        file_path=name, language="python", content="x\n", file_hash=text_hash("x\n")
    )[0]
    return ContextCandidate(
        chunk,
        rank if source is RetrievalSource.LEXICAL else None,
        rank if source is RetrievalSource.SEMANTIC else None,
        0.5 if source is RetrievalSource.SEMANTIC else None,
        0.0,
        (source,),
    )


def test_rrf_exact_formula_union_and_stable_ties() -> None:
    lexical = (
        candidate("both.py", 2, RetrievalSource.LEXICAL),
        candidate("a.py", 1, RetrievalSource.LEXICAL),
    )
    semantic = (
        candidate("both.py", 5, RetrievalSource.SEMANTIC),
        candidate("b.py", 1, RetrievalSource.SEMANTIC),
    )
    result = rrf_merge(lexical, semantic)
    assert [c.chunk.file_path for c in result] == ["both.py", "a.py", "b.py"]
    assert result[0].rrf_score == 1 / 62 + 1 / 65
    assert result[0].sources == (RetrievalSource.LEXICAL, RetrievalSource.SEMANTIC)
    assert result[1].semantic_rank is None
    assert result[2].lexical_rank is None
    assert result[1].rrf_score == result[2].rrf_score == 1 / 61


def test_query_terms_keep_exact_literals_and_unicode() -> None:
    terms = lexical_terms('Fix `exact_symbol` in src/thing.py and "literal string" 用户配置')
    assert terms[:2] == ("exact_symbol", "literal string")
    assert "src/thing.py" in terms and "用户配置" in terms
    assert len(terms) == len(set(terms))
    assert lexical_terms("the and for") == ()


def test_file_filtering_and_nested_gitignore(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("*.log\nignored/\n", encoding="utf-8")
    (tmp_path / "source.py").write_text("value = 1\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("Repository instructions", encoding="utf-8")
    (tmp_path / "binary").write_bytes(b"ab\0cd")
    (tmp_path / "large").write_bytes(b"x" * 1025)
    (tmp_path / "bad").write_bytes(b"\xff\xfe")
    (tmp_path / "noise.log").write_text("ignored", encoding="utf-8")
    for name in ("node_modules", "ignored", "src"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "a.log").write_text("a", encoding="utf-8")
    (tmp_path / "src" / ".gitignore").write_text("!a.log\n", encoding="utf-8")
    scan = RepositoryFiles(tmp_path, 1024).scan()
    paths = dict(scan.files)
    assert "source.py" in paths and "src/a.log" in paths
    assert "ignored/a.log" not in paths and "node_modules/a.log" not in paths
    assert not {"AGENTS.md", "binary", "bad", "large", "noise.log"} & paths.keys()
    assert scan.skipped >= 6


def manager(maximum: int = 24000, observations: int = 8) -> BoundedContextManager:
    return BoundedContextManager(
        cast(ContextProvider, object()),
        ToolRepositoryAccess(cast(ToolRuntime, object())),
        LineWindowChunker(),
        ContextBudget(12, min(maximum, 12000), observations),
        maximum,
    )


def context() -> WorkingContext:
    return WorkingContext(
        "Implement the task",
        (RepositoryInstruction("AGENTS.md", ".", 0, "Preserve safety constraints", False),),
        (),
        (),
        (),
        False,
    )


def render(view: WorkingContext) -> Sequence[ModelMessage]:
    return [
        ModelMessage("system", "Nexus safety policy"),
        ModelMessage("user", json.dumps(context_payload(view), ensure_ascii=False)),
    ]


def turn(content: str, sequence: int = 1) -> SessionTurn:
    return SessionTurn(
        str(uuid4()), str(uuid4()), None, sequence, "user", content, None, datetime.now(UTC)
    )


def test_sd508a_code_is_evicted_before_recent_conversation() -> None:
    base = replace(context(), recent_conversation_turns=(turn("recent fact " * 100),))
    maximum = model_input_tokens(render(base)) + 10
    code = SelectedFileContext("x.py", "CODE" * 300, (), "hybrid retrieval", False)
    original = replace(base, selected_files=(code,))
    assert estimated_tokens(code.content) < 12000
    assert model_input_tokens(render(original)) > maximum
    result = manager(maximum).fit_model_input(original, render)
    assert not result.selected_files
    assert result.recent_conversation_turns == base.recent_conversation_turns
    assert result.repository_instructions == base.repository_instructions
    assert result.task == base.task
    assert result.truncated
    assert model_input_tokens(render(result)) <= maximum


def test_mandatory_context_overflow_fails_without_truncation() -> None:
    original = replace(context(), task="mandatory" * 1000)
    with pytest.raises(ContextError, match="authoritative") as caught:
        manager(100).fit_model_input(original, render)
    assert caught.value.code == "CONTEXT_BUILD_FAILED"
    assert original.task == "mandatory" * 1000


async def test_retention_and_compaction_leave_complete_execution_values_intact() -> None:
    observations = tuple(
        Observation(str(uuid4()), "read_file", True, f"fact {i}", None, None) for i in range(12)
    )
    turns = tuple(turn(f"message {i}", i + 1) for i in range(9))
    result = await manager().prepare_agent_context(
        working_context=context(), plan=None, observations=observations, conversation_turns=turns
    )
    assert result.recent_observations == observations[-8:]
    assert result.recent_conversation_turns == turns[-6:]
    assert "fact 0" in (result.compacted_observations or "")
    assert "message 0" in (result.compacted_conversation or "")
    assert len(observations) == 12


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_model_input_tokens", 24001),
        ("max_code_context_tokens", 12001),
        ("max_retrieved_chunks", 13),
        ("max_recent_observations", 9),
        ("rrf_k", 61),
        ("lexical_top_k", 21),
        ("semantic_top_k", 19),
        ("final_candidate_count", 13),
    ],
)
def test_frozen_configuration_limits(field: str, value: int) -> None:
    with pytest.raises(ValidationError):
        RuntimeConfig.model_validate({field: value})


def test_downward_budget_configuration_accepts_zero_nonmandatory_context() -> None:
    cfg = RuntimeConfig(
        max_retrieved_chunks=0,
        max_code_context_tokens=0,
        max_recent_observations=0,
    )
    assert cfg.max_retrieved_chunks == 0
    assert cfg.max_code_context_tokens == 0
    assert cfg.max_recent_observations == 0


@pytest.mark.asyncio
async def test_zero_observation_budget_compacts_all_observations() -> None:
    observations = tuple(
        Observation(str(uuid4()), "read_file", True, f"fact {index}", None, None)
        for index in range(2)
    )
    result = await manager(observations=0).prepare_agent_context(
        working_context=context(),
        plan=None,
        observations=observations,
        conversation_turns=(),
    )
    assert result.recent_observations == ()
    assert "fact 0" in (result.compacted_observations or "")
    assert "fact 1" in (result.compacted_observations or "")


def test_fixed_retrieval_knobs_are_not_external_configuration(tmp_path: Path) -> None:
    folder = tmp_path / ".nexus"
    folder.mkdir()
    (folder / "config.toml").write_text(
        "[context]\nlexical_top_k=1\nsemantic_top_k=1\nrrf_k=1\n"
        "final_candidate_count=1\n",
        encoding="utf-8",
    )
    cfg = load_runtime_config(
        repo_root=tmp_path,
        user_config_path=tmp_path / "absent",
        environ={
            "NEXUS_LEXICAL_TOP_K": "1",
            "NEXUS_SEMANTIC_TOP_K": "1",
            "NEXUS_RRF_K": "1",
            "NEXUS_FINAL_CANDIDATE_COUNT": "1",
        },
    )
    assert (
        cfg.lexical_top_k,
        cfg.semantic_top_k,
        cfg.rrf_k,
        cfg.final_candidate_count,
    ) == (20, 20, 60, 12)


def test_embedding_is_independent_and_secrets_are_environment_only(tmp_path: Path) -> None:
    cfg = RuntimeConfig(semantic_enabled=False)
    with pytest.raises(ConfigurationError):
        cfg.require_embedding()
    cfg = RuntimeConfig(
        embedding_model="fixture",
        embedding_dimension=3,
        embedding_base_url="https://example.invalid/v1",
        embedding_api_key=SecretStr("embedding-secret"),
    )
    cfg.require_embedding()
    assert cfg.model_name is None
    assert "embedding-secret" not in repr(cfg)
    folder = tmp_path / ".nexus"
    folder.mkdir()
    (folder / "config.toml").write_text('[embedding]\napi_key="forbidden"', encoding="utf-8")
    with pytest.raises(ConfigurationError):
        load_runtime_config(repo_root=tmp_path, user_config_path=tmp_path / "absent", environ={})


class _CaptureGateway:
    def __init__(self) -> None:
        self.messages: Sequence[ModelMessage] = ()

    async def complete(self, messages: Sequence[ModelMessage]) -> ModelResponse:
        self.messages = messages
        return ModelResponse('{"kind":"TASK_READY","summary":"ready","action":null}')


def _plan() -> Plan:
    # Build a valid read-only Plan using the frozen domain validation.
    from datetime import UTC, datetime

    from nexus.domain.planning import (
        AuthorizationScope,
        PlanStatus,
        PlanStep,
        PlanStepStatus,
        compute_scope_digest,
    )
    from nexus.domain.tooling import ApprovalDecision

    scope = AuthorizationScope((), ())
    steps = (
        PlanStep(
            str(uuid4()),
            1,
            "Inspect current evidence",
            None,
            (),
            None,
            None,
            PlanStepStatus.PENDING,
        ),
    )
    plan_id = str(uuid4())
    return Plan(
        plan_id,
        str(uuid4()),
        str(uuid4()),
        1,
        PlanKind.INITIAL,
        PlanStatus.CREATED,
        ApprovalDecision.PENDING,
        steps,
        scope,
        "Retain the user's bounded task",
        None,
        None,
        compute_scope_digest(plan_id, 1, scope),
        datetime.now(UTC),
        None,
    )


async def test_actual_agent_prompt_uses_manager_view_without_a_second_history_slice() -> None:
    gateway = _CaptureGateway()
    plan = _plan()
    observations = tuple(
        Observation(str(uuid4()), "read_file", True, f"fact {i}", None, None) for i in range(10)
    )
    ctx_manager = manager(observations=3)
    prepared = await ctx_manager.prepare_agent_context(
        working_context=context(), plan=plan, observations=observations, conversation_turns=()
    )
    adapter = JsonAgentDecisionAdapter(
        cast(ModelGateway, gateway), prepare_input=ctx_manager.fit_model_input
    )
    await adapter.decide(AgentDecisionRequest(context().task, prepared, plan, observations))
    payload = json.loads(gateway.messages[1].content)
    assert len(payload["observations"]) == 3
    assert payload["observations"][0]["evidence_summary"] == "fact 7"
    assert payload["plan"]["id"] == plan.plan_id
    assert "fact 0" in payload["compacted_observations"]
    assert model_input_tokens(gateway.messages) <= 24000


async def test_planning_budget_failure_is_context_error_before_model_invocation() -> None:
    gateway = _CaptureGateway()
    ctx_manager = manager(100)
    planner = ModelPlanner(cast(ModelGateway, gateway), prepare_input=ctx_manager.fit_model_input)
    request = PlanningRequest(
        context().task, context(), PlanKind.INITIAL, None, None, str(uuid4()), str(uuid4())
    )
    with pytest.raises(ContextError) as caught:
        await planner.create_plan(request)
    assert caught.value.code == "CONTEXT_BUILD_FAILED"
    assert gateway.messages == ()


def test_code_eviction_keeps_mandatory_plan_or_fails() -> None:
    base = context()
    plan = "Mandatory execution Plan: " + "validation " * 300

    def render_plan(view: WorkingContext) -> Sequence[ModelMessage]:
        return [*render(view), ModelMessage("user", plan)]

    with pytest.raises(ContextError):
        manager(200).fit_model_input(base, render_plan)
    fitted = manager(2000).fit_model_input(base, render_plan)
    assert render_plan(fitted)[-1].content == plan


async def test_total_pressure_compacts_history_before_removing_manifest_evidence() -> None:
    from nexus.domain.exploration import RepositoryFileEvidence

    manifest = RepositoryFileEvidence("package.json", "manifest", "root", "project uses tests")
    observations = (
        Observation(str(uuid4()), "shell", False, "failed " * 2000, "COMMAND_EXIT_NONZERO", None),
    )
    turns = (turn("old request " * 2000),)
    base = replace(context(), manifest_summaries=(manifest,))
    result = await manager(1000).prepare_agent_context(
        working_context=base, plan=None, observations=observations, conversation_turns=turns
    )
    assert not result.recent_observations
    assert not result.recent_conversation_turns
    assert "COMMAND_EXIT_NONZERO" in (result.compacted_observations or "")
    assert result.manifest_summaries == (manifest,)
