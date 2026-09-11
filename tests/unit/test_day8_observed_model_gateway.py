from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from nexus.application.execution_context import bind_execution_context
from nexus.application.execution_ledger import ToolExecutionLedger
from nexus.application.model_call_context import bind_model_call_phase
from nexus.application.observed_model_gateway import ObservedModelGateway
from nexus.domain.model import (
    ModelCallPhase,
    ModelChunk,
    ModelMessage,
    ModelResponse,
    TokenUsage,
    UsageAvailability,
)
from nexus.domain.observability import RunExecutionContext
from nexus.domain.runtime_events import ModelCallFinished, ModelCallStarted, RuntimeEvent
from nexus.errors import ModelError


class _Gateway:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    async def complete(self, messages: Sequence[ModelMessage]) -> ModelResponse:
        if self.fail:
            raise ModelError("safe failure", code="PROVIDER_FAILED", retryable=True)
        return ModelResponse(
            "done", TokenUsage(2, 3, 5, UsageAvailability.REPORTED)
        )

    async def stream(self, messages: Sequence[ModelMessage]) -> AsyncIterator[ModelChunk]:
        yield ModelChunk("do")
        yield ModelChunk(
            "ne", TokenUsage(2, 3, 5, UsageAvailability.REPORTED)
        )


def _context() -> RunExecutionContext:
    run_id = str(uuid4())
    return RunExecutionContext(
        trace_id=run_id,
        execution_id=str(uuid4()),
        run_id=run_id,
        session_id=None,
        is_resume=False,
        started_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_observed_gateway_emits_one_pair_and_owns_count_and_usage() -> None:
    events: list[RuntimeEvent] = []
    ledger = ToolExecutionLedger()

    async def emit(event: RuntimeEvent) -> None:
        events.append(event)

    gateway = ObservedModelGateway(
        _Gateway(),
        emit=emit,
        ledger=ledger,
        provider="openai_compatible",
        model="test-model",
    )
    context = _context()
    with bind_execution_context(context), bind_model_call_phase(ModelCallPhase.PLAN):
        response = await gateway.complete([ModelMessage("user", "secret")])

    assert response.content == "done"
    assert ledger.model_count(context.run_id) == 1
    assert ledger.token_usage(context.run_id).input_tokens_reported == 2
    assert [type(event) for event in events] == [ModelCallStarted, ModelCallFinished]
    started, finished = events
    assert isinstance(started, ModelCallStarted)
    assert isinstance(finished, ModelCallFinished)
    assert started.model_call_id == finished.model_call_id == gateway.last_model_call_id()
    assert started.phase is finished.phase is ModelCallPhase.PLAN
    assert "secret" not in repr(events)


@pytest.mark.asyncio
async def test_observed_gateway_counts_and_finishes_failed_real_call() -> None:
    events: list[RuntimeEvent] = []
    ledger = ToolExecutionLedger()

    async def emit(event: RuntimeEvent) -> None:
        events.append(event)

    gateway = ObservedModelGateway(
        _Gateway(fail=True),
        emit=emit,
        ledger=ledger,
        provider=None,
        model=None,
    )
    context = _context()
    with (
        bind_execution_context(context),
        bind_model_call_phase(ModelCallPhase.AGENT_STEP),
        pytest.raises(ModelError, match="safe failure"),
    ):
        await gateway.complete([ModelMessage("user", "secret")])

    assert ledger.model_count(context.run_id) == 1
    assert ledger.token_usage(context.run_id).calls_with_unavailable_usage == 1
    finished = events[-1]
    assert isinstance(finished, ModelCallFinished)
    assert finished.success is False
    assert finished.error_code == "PROVIDER_FAILED"


@pytest.mark.asyncio
async def test_observed_stream_uses_single_terminal_aggregate_usage() -> None:
    events: list[RuntimeEvent] = []
    ledger = ToolExecutionLedger()

    async def emit(event: RuntimeEvent) -> None:
        events.append(event)

    gateway = ObservedModelGateway(
        _Gateway(), emit=emit, ledger=ledger, provider="test", model="stream"
    )
    context = _context()
    with bind_execution_context(context), bind_model_call_phase(ModelCallPhase.DIRECT_RESPONSE):
        chunks = [
            chunk.content
            async for chunk in gateway.stream([ModelMessage("user", "secret")])
        ]
        content = "".join(chunks)

    assert content == "done"
    assert ledger.model_count(context.run_id) == 1
    assert ledger.token_usage(context.run_id).total_tokens_reported == 5
