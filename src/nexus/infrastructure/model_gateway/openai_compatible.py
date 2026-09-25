"""LangChain-backed adapter for the approved OpenAI-compatible provider."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

from langchain_openai import ChatOpenAI

from nexus.application.model_call_context import current_model_call_phase
from nexus.config.models import SUPPORTED_MODEL_PROVIDER, RuntimeConfig
from nexus.domain.model import (
    ModelCallPhase,
    ModelChunk,
    ModelMessage,
    ModelResponse,
    TokenUsage,
    UsageAvailability,
)
from nexus.errors import ConfigurationError, ModelError


class OpenAICompatibleModelGateway:
    """Normalize LangChain/OpenAI-compatible calls into Nexus-owned values."""

    def __init__(self, config: RuntimeConfig) -> None:
        self._config = config
        self._client: ChatOpenAI | None = None

    async def complete(self, messages: Sequence[ModelMessage]) -> ModelResponse:
        try:
            response = await self._get_client().ainvoke(
                self._convert_messages(messages),
                **self._structured_output_options(),
            )
            content = _normalize_content(response.content)
        except ConfigurationError:
            raise
        except Exception as exc:
            raise ModelError(
                "The configured model request failed.",
                retryable=True,
            ) from exc
        if not content.strip():
            raise ModelError(
                "The configured model returned empty output.",
                code="EMPTY_MODEL_OUTPUT",
            )
        return ModelResponse(content=content, usage=_normalize_usage(response.usage_metadata))

    async def stream(self, messages: Sequence[ModelMessage]) -> AsyncIterator[ModelChunk]:
        pending_content: str | None = None
        terminal_usage: TokenUsage | None = None
        try:
            async for chunk in self._get_client().astream(self._convert_messages(messages)):
                if pending_content is not None:
                    yield ModelChunk(content=pending_content)
                pending_content = _normalize_content(chunk.content)
                usage = _normalize_usage(chunk.usage_metadata)
                if usage is not None:
                    terminal_usage = usage
            if pending_content is not None:
                yield ModelChunk(content=pending_content, usage=terminal_usage)
        except ConfigurationError:
            raise
        except Exception as exc:
            raise ModelError(
                "The configured model stream failed.",
                retryable=True,
            ) from exc

    def _get_client(self) -> ChatOpenAI:
        if self._client is not None:
            return self._client
        if self._config.model_provider != SUPPORTED_MODEL_PROVIDER:
            raise ConfigurationError("Only the openai_compatible provider is supported on Day 1.")
        if self._config.model_name is None:
            raise ConfigurationError("Configure a model name before running nexus chat.")
        if self._config.model_api_key is None:
            raise ConfigurationError("Set NEXUS_MODEL_API_KEY before running nexus chat.")

        try:
            self._client = ChatOpenAI(
                model=self._config.model_name,
                base_url=self._config.model_base_url,
                api_key=self._config.model_api_key,
            )
        except Exception as exc:
            raise ModelError("The configured model could not be initialized.") from exc
        return self._client

    def _structured_output_options(self) -> dict[str, Any]:
        if not _is_qwen_3_7_plus(self._config.model_name):
            return {}
        try:
            phase = current_model_call_phase()
        except RuntimeError:
            return {}
        response_format = _response_format_for_phase(phase)
        if response_format is None:
            return {}
        return {
            "response_format": response_format,
            "extra_body": {"enable_thinking": False},
        }

    @staticmethod
    def _convert_messages(messages: Sequence[ModelMessage]) -> list[tuple[str, str]]:
        return [(message.role, message.content) for message in messages]


def _normalize_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""

    text_parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            text_parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                text_parts.append(text)
    return "".join(text_parts)


def _normalize_usage(value: object) -> TokenUsage | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        return TokenUsage.unavailable()
    raw_values = (
        value.get("input_tokens"),
        value.get("output_tokens"),
        value.get("total_tokens"),
    )
    normalized = tuple(
        item
        if isinstance(item, int) and not isinstance(item, bool) and item >= 0
        else None
        for item in raw_values
    )
    input_tokens, output_tokens, total_tokens = normalized
    if (
        input_tokens is not None
        and output_tokens is not None
        and total_tokens is not None
        and total_tokens != input_tokens + output_tokens
    ):
        normalized = (input_tokens, output_tokens, None)
    known = sum(item is not None for item in normalized)
    if known == 0:
        return TokenUsage.unavailable()
    availability = (
        UsageAvailability.REPORTED if known == 3 else UsageAvailability.PARTIAL
    )
    return TokenUsage(normalized[0], normalized[1], normalized[2], availability)


def _is_qwen_3_7_plus(model_name: str | None) -> bool:
    return model_name == "qwen3.7-plus" or (
        model_name is not None and model_name.startswith("qwen3.7-plus-")
    )


def _response_format_for_phase(phase: ModelCallPhase) -> dict[str, Any] | None:
    if phase in {ModelCallPhase.PLAN, ModelCallPhase.REPLAN}:
        return _json_schema_response_format("nexus_plan", _plan_schema())
    if phase is ModelCallPhase.REPAIR:
        return _json_schema_response_format("nexus_repair", _repair_schema())
    if phase is ModelCallPhase.AGENT_STEP:
        return _json_schema_response_format("nexus_agent_decision", _agent_schema())
    if phase is ModelCallPhase.SKILL_SELECTION:
        return _json_schema_response_format("nexus_skill_selection", _skill_selection_schema())
    return None


def _json_schema_response_format(name: str, schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": name,
            "strict": True,
            "schema": schema,
        },
    }


def _plan_step_schema(*, allow_legacy_patch: bool = False) -> dict[str, Any]:
    return {
        "oneOf": [
            _plan_step_variant_schema(
                tool_name={
                    "type": "string",
                    "enum": ["edit_file", "write_file"]
                    + (["apply_patch"] if allow_legacy_patch else []),
                },
                target_paths={
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 1,
                },
                command_argv={"type": "null"},
                command_cwd={"type": "null"},
            ),
            _plan_step_variant_schema(
                tool_name={"type": "string", "enum": ["shell"]},
                target_paths={
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 0,
                },
                command_argv={
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                    "minItems": 1,
                },
                command_cwd={"type": "string", "minLength": 1},
            ),
            _plan_step_variant_schema(
                tool_name={
                    "type": ["string", "null"],
                    "not": {"enum": ["edit_file", "write_file", "apply_patch", "shell"]},
                },
                target_paths={
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 0,
                },
                command_argv={"type": "null"},
                command_cwd={"type": "null"},
            ),
        ]
    }


def _plan_step_variant_schema(
    *,
    tool_name: dict[str, Any],
    target_paths: dict[str, Any],
    command_argv: dict[str, Any],
    command_cwd: dict[str, Any],
) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "description": {"type": "string", "minLength": 1},
            "tool_name": tool_name,
            "target_paths": target_paths,
            "command_argv": command_argv,
            "command_cwd": command_cwd,
        },
        "required": [
            "description",
            "tool_name",
            "target_paths",
            "command_argv",
            "command_cwd",
        ],
    }


def _plan_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "rationale_summary": {"type": "string", "minLength": 1},
            "steps": {
                "type": "array",
                "items": _plan_step_schema(),
                "minItems": 1,
            },
        },
        "required": ["rationale_summary", "steps"],
    }


def _repair_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "failure_summary": {"type": "string", "minLength": 1},
            "steps": {
                "type": "array",
                "items": _plan_step_schema(allow_legacy_patch=True),
                "minItems": 1,
            },
        },
        "required": ["failure_summary", "steps"],
    }


def _agent_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "kind": {
                "type": "string",
                "enum": ["TOOL_ACTION", "CONTINUE", "TASK_READY"],
            },
            "summary": {"type": "string", "minLength": 1},
            "action": {
                "type": ["object", "null"],
                "additionalProperties": False,
                "properties": {
                    "tool_name": {"type": "string", "minLength": 1},
                    "arguments": {
                        "type": "object",
                        "additionalProperties": True,
                    },
                },
                "required": ["tool_name", "arguments"],
            },
        },
        "required": ["kind", "summary", "action"],
    }


def _skill_selection_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "selected_skill_ids": {
                "type": "array",
                "items": {
                    "type": "string",
                    "pattern": r"^[a-z][a-z0-9-]{0,63}$",
                },
                "maxItems": 2,
            },
            "selection_reason_summary": {
                "type": "string",
                "minLength": 1,
                "maxLength": 1000,
            },
        },
        "required": ["selected_skill_ids", "selection_reason_summary"],
    }
