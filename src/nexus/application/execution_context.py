"""Context-local Day 8 execution identity propagation."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from nexus.domain.observability import RunExecutionContext

_EXECUTION_CONTEXT: ContextVar[RunExecutionContext | None] = ContextVar(
    "nexus_run_execution_context", default=None
)


@contextmanager
def bind_execution_context(context: RunExecutionContext) -> Iterator[None]:
    token = _EXECUTION_CONTEXT.set(context)
    try:
        yield
    finally:
        _EXECUTION_CONTEXT.reset(token)


def current_execution_context() -> RunExecutionContext:
    context = _EXECUTION_CONTEXT.get()
    if context is None:
        raise RuntimeError("No RunExecutionContext is bound to this async execution.")
    return context
