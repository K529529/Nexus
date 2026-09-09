"""Nexus-owned model-input budget validation boundary."""

from collections.abc import Sequence
from typing import Protocol

from nexus.domain.model import ModelMessage


class ModelInputBudgetGuard(Protocol):
    def ensure_fits(
        self,
        messages: Sequence[ModelMessage],
        *,
        error_code: str,
    ) -> None: ...
