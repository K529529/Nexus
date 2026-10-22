"""One local estimator over the actual Chat Completions message/tool shapes."""

from __future__ import annotations

import gzip
import json
import math
from collections import deque
from fractions import Fraction
from functools import lru_cache
from pathlib import Path
from statistics import median
from urllib.parse import urlsplit

from jinja2 import Environment, Template
from tokenizers import Tokenizer

from nexus.core.types import Json, Message, ToolSpec, json_text


def wire_message(message: Message) -> Json:
    wire: Json = {"role": message.role, "content": message.content}
    if message.tool_calls:
        wire["tool_calls"] = [
            {
                "id": c.id,
                "type": "function",
                "function": {
                    "name": c.name,
                    "arguments": c.arguments_json,
                },
            }
            for c in message.tool_calls
        ]
    if message.tool_call_id is not None:
        wire["tool_call_id"] = message.tool_call_id
    if message.protocol_data:
        fields = message.protocol_data.get("fields", {})
        if isinstance(fields, dict) and isinstance(fields.get("reasoning_content"), str):
            wire["reasoning_content"] = fields["reasoning_content"]
    return wire


def wire_tools(tools: list[ToolSpec]) -> list[Json]:
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.input_schema,
            },
        }
        for t in tools
    ]


def estimate(messages: list[Message], tools: list[ToolSpec]) -> int:
    """Unvalidated fallback for unknown providers; also the legacy artifact budget unit."""
    request = {"messages": [wire_message(m) for m in messages]}
    if tools:
        request["tools"] = wire_tools(tools)
    return math.ceil(len(json_text(request).encode("utf-8")) / 3) + 8 * len(messages)


DATA = Path(__file__).with_name("tokenizer_data")


@lru_cache(maxsize=2)
def tokenizer(profile: str) -> Tokenizer:
    return Tokenizer.from_str(gzip.decompress((DATA / f"{profile}.json.gz").read_bytes()).decode())


@lru_cache(maxsize=1)
def qwen_template() -> Template:
    # Trusted, pinned upstream template; no runtime downloads or user templates.
    return Environment().from_string((DATA / "qwen.jinja").read_text(encoding="utf-8"))


class TokenEstimator:
    """A model/provider-bound tokenizer and a small, bounded usage correction.

    Counts request envelopes, not JSON transport bytes. Correction uses only previous
    observations, never cache-miss tokens, and cannot turn one anomalous receipt into
    an arbitrary capacity multiplier. Instances are never shared between model roles.
    """

    def __init__(
        self,
        name: str = "",
        base_url: str = "",
        *,
        reasoning_effort: str | None = None,
        enable_thinking: bool | None = None,
    ) -> None:
        host = urlsplit(base_url).hostname
        self.profile = "fallback"
        if host == "api.deepseek.com" and name == "deepseek-flash":
            self.profile = "deepseek"
        elif host == "api.siliconflow.cn" and name == "Qwen/Qwen3.5-35B-A3B":
            self.profile = "qwen"
        self.reasoning_effort = reasoning_effort
        self.enable_thinking = enable_thinking
        self.ratios: deque[Fraction] = deque(maxlen=3)
        self.last_estimate: int | None = None
        self.last_report: int | None = None
        self.scope = ""

    def raw(self, messages: list[Message], tools: list[ToolSpec]) -> int:
        if self.profile == "fallback":
            return estimate(messages, tools)
        if not messages:
            return 0
        wire = [wire_message(m) for m in messages]
        schemas = wire_tools(tools)
        if self.profile == "deepseek":
            from nexus._vendor.encoding_dsv4 import encode_messages

            if schemas:
                if wire[0]["role"] != "system":
                    wire.insert(0, {"role": "system", "content": ""})
                wire[0]["tools"] = schemas
            effort = "max" if self.reasoning_effort == "max" else "high"
            text = encode_messages(
                wire,
                thinking_mode="chat" if self.reasoning_effort == "none" else "thinking",
                reasoning_effort=effort,
            )
            # Measured API envelope residual, independent of content length; see P1 report.
            overhead = 26
        else:
            for message in wire:
                for call in message.get("tool_calls", []):
                    call["function"]["arguments"] = json.loads(call["function"]["arguments"])
            # Standalone fragments used by history splitting/summary budget may lack a user.
            # A framing user enables the official template; subtract its exact envelope.
            fragment = not any(m["role"] == "user" for m in wire)
            if fragment:
                wire.insert(
                    1 if wire[0]["role"] == "system" else 0, {"role": "user", "content": ""}
                )
            template = qwen_template()
            text = template.render(
                messages=wire,
                tools=schemas,
                enable_thinking=self.enable_thinking,
                add_generation_prompt=True,
            )
            overhead = (5 if schemas else 0) - (5 if fragment else 0)
        return max(
            0, len(tokenizer(self.profile).encode(text, add_special_tokens=False).ids) + overhead
        )

    @property
    def factor(self) -> float:
        return float(median(self.ratios)) if self.ratios else 1.0

    def count(self, messages: list[Message], tools: list[ToolSpec]) -> int:
        factor = median(self.ratios) if self.ratios else Fraction(1)
        return math.ceil(self.raw(messages, tools) * factor)

    def reset(self) -> None:
        self.ratios.clear()
        self.last_estimate = self.last_report = None

    def set_scope(self, messages: list[Message], tools: list[ToolSpec]) -> None:
        key = (
            json_text(wire_tools(tools))
            + json_text([wire_message(m) for m in messages if m.role == "system"])
            + json_text(
                sorted({(m.role, bool(m.tool_calls), bool(m.protocol_data)) for m in messages})
            )
        )
        if key != self.scope:
            self.reset()
            self.scope = key

    def observe(self, reported: int | None, messages: list[Message], tools: list[ToolSpec]) -> None:
        self.set_scope(messages, tools)
        self.last_estimate = self.raw(messages, tools)
        self.last_report = reported
        if reported is None or reported <= 0:
            self.ratios.clear()
            self.last_report = None
        elif self.last_estimate >= 256:
            self.ratios.append(
                min(
                    Fraction(115, 100),
                    max(Fraction(85, 100), Fraction(reported, self.last_estimate)),
                )
            )
