"""Single Nexus-owned instrumentation seam around every real model call."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextvars import ContextVar
from uuid import uuid4

from nexus.application.execution_context import current_execution_context
from nexus.application.execution_ledger import ToolExecutionLedger
from nexus.application.model_call_context import current_model_call_phase
from nexus.domain.model import (
    ModelCallPhase,
    ModelChunk,
    ModelMessage,
    ModelResponse,
    TokenUsage,
)
from nexus.domain.ports.model_gateway import ModelGateway
from nexus.domain.runtime_events import ModelCallFinished, ModelCallStarted, RuntimeEvent
from nexus.errors import NexusError

RuntimeEventEmitter = Callable[[RuntimeEvent], Awaitable[None]]

_LAST_MODEL_CALL_ID: ContextVar[str | None] = ContextVar(
    "nexus_last_model_call_id", default=None
)


class ObservedModelGateway:
    """Emit safe model lifecycle events and own model counts and usage."""

    def __init__(
        self,
        gateway: ModelGateway,
        *,
        emit: RuntimeEventEmitter,
        ledger: ToolExecutionLedger,
        provider: str | None,
        model: str | None,
    ) -> None:
        self._gateway = gateway
        self._emit = emit
        self._ledger = ledger
        self._provider = provider
        self._model = model

    def last_model_call_id(self) -> str:
        model_call_id = _LAST_MODEL_CALL_ID.get()
        if model_call_id is None:
            raise RuntimeError("No model call completed in this async execution.")
        return model_call_id

    async def complete(self, messages: Sequence[ModelMessage]) -> ModelResponse:
        context = current_execution_context()
        phase = current_model_call_phase()
        call_id = str(uuid4())
        self._ledger.begin_model(context.run_id)
        await self._emit_started(call_id, phase)
        started = time.perf_counter()
        try:
            response = await self._gateway.complete(messages)
        except Exception as exc:
            usage = TokenUsage.unavailable()
            self._ledger.record_model_usage(context.run_id, usage)
            await self._emit_finished(
                call_id,
                phase,
                success=False,
                started=started,
                usage=usage,
                error_code=_safe_error_code(exc),
            )
            _LAST_MODEL_CALL_ID.set(call_id)
            raise
        usage = response.usage or TokenUsage.unavailable()
        self._ledger.record_model_usage(context.run_id, usage)
        await self._emit_finished(
            call_id,
            phase,
            success=True,
            started=started,
            usage=usage,
            error_code=None,
        )
        _LAST_MODEL_CALL_ID.set(call_id)
        return response

    async def stream(self, messages: Sequence[ModelMessage]) -> AsyncIterator[ModelChunk]:
        context = current_execution_context()
        phase = current_model_call_phase()
        call_id = str(uuid4())
        self._ledger.begin_model(context.run_id)
        await self._emit_started(call_id, phase)
        started = time.perf_counter()
        usage: TokenUsage | None = None
        try:
            async for chunk in self._gateway.stream(messages):
                if chunk.usage is not None:
                    usage = chunk.usage
                yield chunk
        except asyncio.CancelledError:
            normalized = usage or TokenUsage.unavailable()
            self._ledger.record_model_usage(context.run_id, normalized)
            await self._emit_finished(
                call_id,
                phase,
                success=False,
                started=started,
                usage=normalized,
                error_code="MODEL_CALL_CANCELLED",
            )
            _LAST_MODEL_CALL_ID.set(call_id)
            raise
        except GeneratorExit:
            normalized = usage or TokenUsage.unavailable()
            self._ledger.record_model_usage(context.run_id, normalized)
            await self._emit_finished(
                call_id,
                phase,
                success=False,
                started=started,
                usage=normalized,
                error_code="MODEL_STREAM_CLOSED",
            )
            _LAST_MODEL_CALL_ID.set(call_id)
            raise
        except Exception as exc:
            normalized = usage or TokenUsage.unavailable()
            self._ledger.record_model_usage(context.run_id, normalized)
            await self._emit_finished(
                call_id,
                phase,
                success=False,
                started=started,
                usage=normalized,
                error_code=_safe_error_code(exc),
            )
            _LAST_MODEL_CALL_ID.set(call_id)
            raise
        normalized = usage or TokenUsage.unavailable()
        self._ledger.record_model_usage(context.run_id, normalized)
        await self._emit_finished(
            call_id,
            phase,
            success=True,
            started=started,
            usage=normalized,
            error_code=None,
        )
        _LAST_MODEL_CALL_ID.set(call_id)

    async def _emit_started(self, call_id: str, phase: ModelCallPhase) -> None:
        context = current_execution_context()
        await self._emit(
            ModelCallStarted(
                run_id=context.run_id,
                session_id=context.session_id,
                model_call_id=call_id,
                phase=phase,
                provider=self._provider,
                model=self._model,
            )
        )

    async def _emit_finished(
        self,
        call_id: str,
        phase: ModelCallPhase,
        *,
        success: bool,
        started: float,
        usage: TokenUsage,
        error_code: str | None,
    ) -> None:
        context = current_execution_context()
        await self._emit(
            ModelCallFinished(
                run_id=context.run_id,
                session_id=context.session_id,
                model_call_id=call_id,
                phase=phase,
                success=success,
                duration_ms=max(0, int((time.perf_counter() - started) * 1000)),
                usage=usage,
                error_code=error_code,
            )
        )


def _safe_error_code(exc: Exception) -> str:
    return exc.code if isinstance(exc, NexusError) else "MODEL_ERROR"
