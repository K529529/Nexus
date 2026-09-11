"""Provider-neutral model request, response, and usage values."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

ModelRole = Literal["system", "user", "assistant"]


class UsageAvailability(StrEnum):
    REPORTED = "REPORTED"
    PARTIAL = "PARTIAL"
    UNAVAILABLE = "UNAVAILABLE"


class ModelCallPhase(StrEnum):
    DIRECT_RESPONSE = "DIRECT_RESPONSE"
    PLAN = "PLAN"
    REPLAN = "REPLAN"
    REPAIR = "REPAIR"
    AGENT_STEP = "AGENT_STEP"
    SKILL_SELECTION = "SKILL_SELECTION"


@dataclass(frozen=True, slots=True)
class TokenUsage:
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    availability: UsageAvailability

    def __post_init__(self) -> None:
        values = (self.input_tokens, self.output_tokens, self.total_tokens)
        if any(value is not None and value < 0 for value in values):
            raise ValueError("Token usage values must not be negative.")
        known = sum(value is not None for value in values)
        if self.availability is UsageAvailability.UNAVAILABLE and known != 0:
            raise ValueError("UNAVAILABLE token usage cannot contain reported values.")
        if self.availability is UsageAvailability.REPORTED and known != 3:
            raise ValueError("REPORTED token usage requires a complete usage triple.")
        if self.availability is UsageAvailability.PARTIAL and known not in {1, 2}:
            raise ValueError("PARTIAL token usage requires one or two reported values.")
        if known == 3:
            assert self.input_tokens is not None
            assert self.output_tokens is not None
            if self.total_tokens != self.input_tokens + self.output_tokens:
                raise ValueError("Token total must equal input plus output tokens.")

    @classmethod
    def unavailable(cls) -> TokenUsage:
        return cls(None, None, None, UsageAvailability.UNAVAILABLE)


@dataclass(frozen=True, slots=True)
class TokenUsageAggregate:
    input_tokens_reported: int = 0
    output_tokens_reported: int = 0
    total_tokens_reported: int = 0
    calls_with_reported_usage: int = 0
    calls_with_partial_usage: int = 0
    calls_with_unavailable_usage: int = 0
    usage_complete: bool = True

    def __post_init__(self) -> None:
        counters = (
            self.input_tokens_reported,
            self.output_tokens_reported,
            self.total_tokens_reported,
            self.calls_with_reported_usage,
            self.calls_with_partial_usage,
            self.calls_with_unavailable_usage,
        )
        if any(value < 0 for value in counters):
            raise ValueError("Token usage aggregate values must not be negative.")
        complete = (
            self.calls_with_partial_usage == 0
            and self.calls_with_unavailable_usage == 0
        )
        if self.usage_complete is not complete:
            raise ValueError("Token usage aggregate completeness is inconsistent.")

    @property
    def call_count(self) -> int:
        return (
            self.calls_with_reported_usage
            + self.calls_with_partial_usage
            + self.calls_with_unavailable_usage
        )

    def add(self, usage: TokenUsage) -> TokenUsageAggregate:
        return TokenUsageAggregate(
            input_tokens_reported=self.input_tokens_reported + (usage.input_tokens or 0),
            output_tokens_reported=self.output_tokens_reported + (usage.output_tokens or 0),
            total_tokens_reported=self.total_tokens_reported + (usage.total_tokens or 0),
            calls_with_reported_usage=(
                self.calls_with_reported_usage
                + int(usage.availability is UsageAvailability.REPORTED)
            ),
            calls_with_partial_usage=(
                self.calls_with_partial_usage
                + int(usage.availability is UsageAvailability.PARTIAL)
            ),
            calls_with_unavailable_usage=(
                self.calls_with_unavailable_usage
                + int(usage.availability is UsageAvailability.UNAVAILABLE)
            ),
            usage_complete=(
                self.usage_complete
                and usage.availability is UsageAvailability.REPORTED
            ),
        )

    def to_usage(self) -> TokenUsage:
        if self.call_count == 0 or (
            self.calls_with_reported_usage == 0
            and self.calls_with_partial_usage == 0
        ):
            return TokenUsage.unavailable()
        availability = (
            UsageAvailability.REPORTED
            if self.usage_complete
            else UsageAvailability.PARTIAL
        )
        if availability is UsageAvailability.PARTIAL:
            values: tuple[int | None, int | None, int | None] = (
                self.input_tokens_reported,
                self.output_tokens_reported,
                None,
            )
        else:
            values = (
                self.input_tokens_reported,
                self.output_tokens_reported,
                self.total_tokens_reported,
            )
        return TokenUsage(*values, availability)

    def to_dict(self) -> dict[str, int | bool]:
        return {
            "input_tokens_reported": self.input_tokens_reported,
            "output_tokens_reported": self.output_tokens_reported,
            "total_tokens_reported": self.total_tokens_reported,
            "calls_with_reported_usage": self.calls_with_reported_usage,
            "calls_with_partial_usage": self.calls_with_partial_usage,
            "calls_with_unavailable_usage": self.calls_with_unavailable_usage,
            "usage_complete": self.usage_complete,
        }


@dataclass(frozen=True, slots=True)
class ModelMessage:
    """A minimal Nexus-owned chat message."""

    role: ModelRole
    content: str


@dataclass(frozen=True, slots=True)
class ModelResponse:
    """A normalized non-streaming model response."""

    content: str
    usage: TokenUsage | None = None


@dataclass(frozen=True, slots=True)
class ModelChunk:
    """A normalized streaming model fragment."""

    content: str
    usage: TokenUsage | None = None
