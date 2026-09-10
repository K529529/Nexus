"""Shared Day 5 model-input accounting exposed through the Day 7 guard port."""

from collections.abc import Sequence

from nexus.context.manager import model_input_tokens
from nexus.domain.model import ModelMessage
from nexus.errors import ContextError


class Day5ModelInputBudgetGuard:
    def __init__(self, maximum: int) -> None:
        self._maximum = maximum

    def ensure_fits(
        self,
        messages: Sequence[ModelMessage],
        *,
        error_code: str,
    ) -> None:
        if model_input_tokens(messages) > self._maximum:
            raise ContextError(
                "The complete model request exceeds the configured input budget.",
                code=error_code,
            )
