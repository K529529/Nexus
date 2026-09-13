from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from nexus.application.execution_context import bind_execution_context
from nexus.application.execution_ledger import ToolExecutionLedger
from nexus.application.model_call_context import current_model_call_phase
from nexus.application.observed_model_gateway import ObservedModelGateway
from nexus.application.structured_output import (
    StructuredOutputViolation,
    append_retry_feedback,
    complete_structured,
)
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
from nexus.errors import ConfigurationError, ContextError, ModelError


class QueueGateway:
    def __init__(self, *results: str | Exception) -> None:
        self.results = list(results)
        self.messages: list[tuple[ModelMessage, ...]] = []
        self.phases: list[ModelCallPhase] = []

    async def complete(self, messages: Sequence[ModelMessage]) -> ModelResponse:
        self.messages.append(tuple(messages))
        self.phases.append(current_model_call_phase())
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return ModelResponse(result)

    async def stream(self, messages: Sequence[ModelMessage]) -> AsyncIterator[ModelChunk]:
        del messages
        if False:
            yield ModelChunk("")


def messages_for(feedback: ModelMessage | None) -> tuple[ModelMessage, ...]:
    return append_retry_feedback((ModelMessage("user", "original task"),), feedback)


def parse_ok(content: str) -> bool:
    try:
        value = json.loads(content)
    except json.JSONDecodeError as exc:
        raise StructuredOutputViolation("JSON_DECODE") from exc
    if not isinstance(value, dict):
        raise StructuredOutputViolation("TOP_LEVEL_TYPE")
    return bool(value["ok"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phase",
    [
        ModelCallPhase.SKILL_SELECTION,
        ModelCallPhase.PLAN,
        ModelCallPhase.REPLAN,
        ModelCallPhase.REPAIR,
        ModelCallPhase.AGENT_STEP,
    ],
)
async def test_invalid_output_retries_once_with_same_phase_and_sanitized_feedback(
    phase: ModelCallPhase,
) -> None:
    gateway = QueueGateway("SENSITIVE invalid body", '{"ok":true}')

    result = await complete_structured(
        gateway,
        phase=phase,
        messages_for_attempt=messages_for,
        parse=parse_ok,
    )

    assert result is True
    assert gateway.phases == [phase, phase]
    assert len(gateway.messages[0]) == 1
    assert len(gateway.messages[1]) == 2
    feedback = gateway.messages[1][-1].content
    assert "Category: JSON_DECODE" in feedback
    assert "complete replacement object" in feedback
    assert "SENSITIVE" not in feedback
    if phase is ModelCallPhase.AGENT_STEP:
        assert "Do not repeat actions recorded as successful" in feedback
    else:
        assert "approved Plan and observations" not in feedback


@pytest.mark.asyncio
async def test_second_invalid_output_stops_after_one_retry() -> None:
    gateway = QueueGateway("first private body", "[]")

    with pytest.raises(StructuredOutputViolation) as caught:
        await complete_structured(
            gateway,
            phase=ModelCallPhase.PLAN,
            messages_for_attempt=messages_for,
            parse=parse_ok,
        )

    assert caught.value.category == "TOP_LEVEL_TYPE"
    assert len(gateway.messages) == 2
    assert "first private body" not in gateway.messages[1][-1].content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        ModelError("provider failed", code="PROVIDER_FAILED"),
        ConfigurationError("configuration failed"),
    ],
)
async def test_provider_and_configuration_failures_are_not_retried(
    error: Exception,
) -> None:
    gateway = QueueGateway(error)

    with pytest.raises(type(error)):
        await complete_structured(
            gateway,
            phase=ModelCallPhase.PLAN,
            messages_for_attempt=messages_for,
            parse=parse_ok,
        )

    assert len(gateway.messages) == 1


@pytest.mark.asyncio
async def test_budget_failure_is_not_retried() -> None:
    gateway = QueueGateway('{"ok":true}')

    def over_budget(feedback: ModelMessage | None) -> tuple[ModelMessage, ...]:
        del feedback
        raise ContextError("too large", code="CONTEXT_BUILD_FAILED")

    with pytest.raises(ContextError) as caught:
        await complete_structured(
            gateway,
            phase=ModelCallPhase.PLAN,
            messages_for_attempt=over_budget,
            parse=parse_ok,
        )

    assert caught.value.code == "CONTEXT_BUILD_FAILED"
    assert gateway.messages == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code",
    [
        "PLAN_SCOPE_DENIED",
        "PERMISSION_DENIED",
        "COMMAND_DENIED",
        "MCP_WRITE_NOT_AUTHORIZED",
        "REPAIR_SCOPE_EXPANSION",
    ],
)
async def test_security_and_scope_failures_are_not_retried(code: str) -> None:
    gateway = QueueGateway('{"ok":true}')

    def reject_security(content: str) -> bool:
        del content
        raise ModelError("safe denial", code=code)

    with pytest.raises(ModelError) as caught:
        await complete_structured(
            gateway,
            phase=ModelCallPhase.REPAIR,
            messages_for_attempt=messages_for,
            parse=reject_security,
        )

    assert caught.value.code == code
    assert len(gateway.messages) == 1


class UsageGateway(QueueGateway):
    async def complete(self, messages: Sequence[ModelMessage]) -> ModelResponse:
        response = await super().complete(messages)
        return ModelResponse(
            response.content,
            TokenUsage(2, 3, 5, UsageAvailability.REPORTED),
        )


@pytest.mark.asyncio
async def test_each_retry_call_uses_existing_model_metrics() -> None:
    events: list[RuntimeEvent] = []
    ledger = ToolExecutionLedger()

    async def emit(event: RuntimeEvent) -> None:
        events.append(event)

    observed = ObservedModelGateway(
        UsageGateway("invalid", '{"ok":true}'),
        emit=emit,
        ledger=ledger,
        provider="test",
        model="test",
    )
    run_id = str(uuid4())
    context = RunExecutionContext(
        trace_id=run_id,
        execution_id=str(uuid4()),
        run_id=run_id,
        session_id=None,
        is_resume=False,
        started_at=datetime.now(UTC),
    )

    with bind_execution_context(context):
        result = await complete_structured(
            observed,
            phase=ModelCallPhase.PLAN,
            messages_for_attempt=messages_for,
            parse=parse_ok,
        )

    assert result is True
    assert ledger.model_count(run_id) == 2
    assert ledger.token_usage(run_id).total_tokens_reported == 10
    assert [type(event) for event in events] == [
        ModelCallStarted,
        ModelCallFinished,
        ModelCallStarted,
        ModelCallFinished,
    ]
    phases: list[ModelCallPhase] = []
    for event in events:
        assert isinstance(event, ModelCallStarted | ModelCallFinished)
        phases.append(event.phase)
    assert phases == [ModelCallPhase.PLAN] * 4
