"""Context-local semantic phase for the next real ModelGateway invocation."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from nexus.domain.model import ModelCallPhase

_MODEL_CALL_PHASE: ContextVar[ModelCallPhase | None] = ContextVar(
    "nexus_model_call_phase", default=None
)


@contextmanager
def bind_model_call_phase(phase: ModelCallPhase) -> Iterator[None]:
    token = _MODEL_CALL_PHASE.set(phase)
    try:
        yield
    finally:
        _MODEL_CALL_PHASE.reset(token)


def current_model_call_phase() -> ModelCallPhase:
    phase = _MODEL_CALL_PHASE.get()
    if phase is None:
        raise RuntimeError("No ModelCallPhase is bound to this model invocation.")
    return phase
