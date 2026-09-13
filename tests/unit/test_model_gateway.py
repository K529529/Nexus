from collections.abc import AsyncIterator
from typing import Any, cast

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from nexus.application.model_call_context import bind_model_call_phase
from nexus.config.models import RuntimeConfig
from nexus.domain.model import ModelCallPhase, ModelMessage, UsageAvailability
from nexus.errors import ConfigurationError
from nexus.infrastructure.model_gateway.openai_compatible import OpenAICompatibleModelGateway


class FakeChatClient:
    def __init__(self) -> None:
        self.invocation_options: list[dict[str, Any]] = []

    async def ainvoke(self, messages: object, **kwargs: Any) -> AIMessage:
        self.invocation_options.append(kwargs)
        return AIMessage(
            content="Normalized response",
            usage_metadata={"input_tokens": 2, "output_tokens": 3, "total_tokens": 5},
        )

    async def astream(self, messages: object) -> AsyncIterator[AIMessageChunk]:
        yield AIMessageChunk(content="Normal")
        yield AIMessageChunk(
            content="ized",
            usage_metadata={"input_tokens": 2, "output_tokens": 3, "total_tokens": 5},
        )


class MalformedUsageChatClient:
    async def ainvoke(self, messages: object, **kwargs: Any) -> AIMessage:
        return AIMessage(
            content="Still a valid response",
            usage_metadata={"input_tokens": 2, "output_tokens": 3, "total_tokens": 99},
        )


@pytest.fixture
def gateway() -> OpenAICompatibleModelGateway:
    config = RuntimeConfig(model_name="test-model", model_api_key=SecretStr("test-secret"))
    model_gateway = OpenAICompatibleModelGateway(config)
    model_gateway._client = cast(ChatOpenAI, FakeChatClient())
    return model_gateway


@pytest.mark.asyncio
async def test_complete_normalizes_provider_response(
    gateway: OpenAICompatibleModelGateway,
) -> None:
    response = await gateway.complete([ModelMessage(role="user", content="hello")])
    assert response.content == "Normalized response"
    assert response.usage is not None
    assert response.usage.availability is UsageAvailability.REPORTED
    assert response.usage.total_tokens == 5


@pytest.mark.asyncio
async def test_stream_normalizes_provider_chunks(gateway: OpenAICompatibleModelGateway) -> None:
    chunks = [
        chunk
        async for chunk in gateway.stream([ModelMessage(role="user", content="hello")])
    ]
    assert [chunk.content for chunk in chunks] == ["Normal", "ized"]
    assert chunks[0].usage is None
    assert chunks[1].usage is not None and chunks[1].usage.total_tokens == 5


@pytest.mark.asyncio
async def test_malformed_usage_degrades_without_failing_model_response() -> None:
    config = RuntimeConfig(
        model_name="test-model", model_api_key=SecretStr("test-secret")
    )
    gateway = OpenAICompatibleModelGateway(config)
    gateway._client = cast(ChatOpenAI, MalformedUsageChatClient())

    response = await gateway.complete([ModelMessage(role="user", content="hello")])

    assert response.content == "Still a valid response"
    assert response.usage is not None
    assert response.usage.availability is UsageAvailability.PARTIAL
    assert response.usage.input_tokens == 2
    assert response.usage.output_tokens == 3
    assert response.usage.total_tokens is None


@pytest.mark.asyncio
async def test_missing_model_configuration_is_safe_configuration_error() -> None:
    gateway = OpenAICompatibleModelGateway(RuntimeConfig())
    with pytest.raises(ConfigurationError, match="model name"):
        await gateway.complete([ModelMessage(role="user", content="hello")])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("phase", "schema_name"),
    [
        (ModelCallPhase.PLAN, "nexus_plan"),
        (ModelCallPhase.REPLAN, "nexus_plan"),
        (ModelCallPhase.REPAIR, "nexus_repair"),
        (ModelCallPhase.AGENT_STEP, "nexus_agent_decision"),
        (ModelCallPhase.SKILL_SELECTION, "nexus_skill_selection"),
    ],
)
async def test_qwen_structured_phases_use_strict_json_schema_without_thinking(
    phase: ModelCallPhase,
    schema_name: str,
) -> None:
    config = RuntimeConfig(
        model_name="qwen3.7-plus",
        model_api_key=SecretStr("test-secret"),
    )
    client = FakeChatClient()
    gateway = OpenAICompatibleModelGateway(config)
    gateway._client = cast(ChatOpenAI, client)

    with bind_model_call_phase(phase):
        await gateway.complete([ModelMessage(role="user", content="hello")])

    assert len(client.invocation_options) == 1
    options = client.invocation_options[0]
    assert set(options) == {"response_format", "extra_body"}
    assert options["extra_body"] == {"enable_thinking": False}
    response_format = options["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["name"] == schema_name
    assert response_format["json_schema"]["strict"] is True
    assert isinstance(response_format["json_schema"]["schema"], dict)


@pytest.mark.asyncio
async def test_qwen_fixed_schemas_are_closed_and_agent_arguments_remain_open() -> None:
    config = RuntimeConfig(
        model_name="qwen3.7-plus-2026-05-26",
        model_api_key=SecretStr("test-secret"),
    )
    client = FakeChatClient()
    gateway = OpenAICompatibleModelGateway(config)
    gateway._client = cast(ChatOpenAI, client)

    for phase in (
        ModelCallPhase.PLAN,
        ModelCallPhase.REPAIR,
        ModelCallPhase.AGENT_STEP,
        ModelCallPhase.SKILL_SELECTION,
    ):
        with bind_model_call_phase(phase):
            await gateway.complete([ModelMessage(role="user", content="hello")])

    plan_schema = client.invocation_options[0]["response_format"]["json_schema"]["schema"]
    assert plan_schema["additionalProperties"] is False
    assert plan_schema["required"] == ["rationale_summary", "steps"]
    plan_step = plan_schema["properties"]["steps"]["items"]
    variants = plan_step["oneOf"]
    assert len(variants) == 3
    for variant in variants:
        assert variant["additionalProperties"] is False
        assert set(variant["required"]) == {
            "description",
            "tool_name",
            "target_paths",
            "command_argv",
            "command_cwd",
        }
    assert variants[0]["properties"]["target_paths"]["minItems"] == 1
    assert variants[0]["properties"]["target_paths"]["maxItems"] == 1
    assert variants[1]["properties"]["tool_name"]["enum"] == ["shell"]
    assert variants[1]["properties"]["target_paths"]["maxItems"] == 0
    assert variants[1]["properties"]["command_argv"]["minItems"] == 1
    assert variants[2]["properties"]["tool_name"]["not"] == {
        "enum": ["apply_patch", "write_file", "shell"]
    }
    assert variants[2]["properties"]["target_paths"]["maxItems"] == 0

    repair_schema = client.invocation_options[1]["response_format"]["json_schema"]["schema"]
    assert repair_schema["additionalProperties"] is False
    repair_variants = repair_schema["properties"]["steps"]["items"]["oneOf"]
    assert len(repair_variants) == 3
    assert all(variant["additionalProperties"] is False for variant in repair_variants)

    agent_schema = client.invocation_options[2]["response_format"]["json_schema"]["schema"]
    assert agent_schema["additionalProperties"] is False
    assert agent_schema["required"] == ["kind", "summary", "action"]
    action_schema = agent_schema["properties"]["action"]
    assert action_schema["additionalProperties"] is False
    assert action_schema["required"] == ["tool_name", "arguments"]
    assert action_schema["properties"]["arguments"] == {
        "type": "object",
        "additionalProperties": True,
    }

    skill_schema = client.invocation_options[3]["response_format"]["json_schema"]["schema"]
    assert skill_schema["additionalProperties"] is False
    assert skill_schema["required"] == [
        "selected_skill_ids",
        "selection_reason_summary",
    ]
    assert skill_schema["properties"]["selected_skill_ids"] == {
        "type": "array",
        "items": {
            "type": "string",
            "pattern": r"^[a-z][a-z0-9-]{0,63}$",
        },
        "maxItems": 2,
    }
    assert skill_schema["properties"]["selection_reason_summary"] == {
        "type": "string",
        "minLength": 1,
        "maxLength": 1000,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phase",
    [ModelCallPhase.DIRECT_RESPONSE],
)
async def test_qwen_non_structured_phases_remain_in_text_mode(
    phase: ModelCallPhase,
) -> None:
    config = RuntimeConfig(
        model_name="qwen3.7-plus",
        model_api_key=SecretStr("test-secret"),
    )
    client = FakeChatClient()
    gateway = OpenAICompatibleModelGateway(config)
    gateway._client = cast(ChatOpenAI, client)

    with bind_model_call_phase(phase):
        await gateway.complete([ModelMessage(role="user", content="hello")])

    assert client.invocation_options == [{}]


@pytest.mark.asyncio
async def test_other_models_keep_existing_text_mode_for_structured_phases() -> None:
    client = FakeChatClient()
    gateway = OpenAICompatibleModelGateway(
        RuntimeConfig(model_name="other-model", model_api_key=SecretStr("test-secret"))
    )
    gateway._client = cast(ChatOpenAI, client)

    with bind_model_call_phase(ModelCallPhase.PLAN):
        await gateway.complete([ModelMessage(role="user", content="hello")])

    assert client.invocation_options == [{}]
