from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from nexus.app.config import ConfigError, load_config
from nexus.core.model import CompactionModel
from nexus.core.types import Message, ModelError, ToolSpec
from tests.conftest import Recorder


def test_independent_config_and_legacy(tmp_path: Path, monkeypatch: Any) -> None:
    path = tmp_path / "config.toml"
    main = '[model]\nname="main"\ncontext_window=128000\napi_key_env="TEST_MAIN"\n'
    path.write_text(main)
    assert load_config(path, require_key=False).compaction_model is None
    path.write_text(
        main + '[compaction_model]\nname="summary"\ncontext_window=128000\n'
        'api_key_env="TEST_SUMMARY"\nrequest_timeout_seconds=60\n'
    )
    monkeypatch.setenv("TEST_MAIN", "main-secret")
    with pytest.raises(ConfigError, match="TEST_SUMMARY"):
        load_config(path)
    monkeypatch.setenv("TEST_SUMMARY", "summary-secret")
    monkeypatch.setenv("NEXUS_MODEL_NAME", "main-override")
    config = load_config(path)
    assert config.model.name == "main-override"
    assert config.model.key() == "main-secret"
    assert config.compaction_model is not None
    assert config.compaction_model.name == "summary"
    assert config.compaction_model.key() == "summary-secret"
    assert config.compaction_model.request_timeout_seconds == 60
    path.write_text(main + '[compaction_model]\napi_key="plaintext"\n')
    with pytest.raises(ConfigError, match="compaction_model: unknown fields: api_key"):
        load_config(path, require_key=False)


async def test_compaction_adapter_rejects_tools_and_private_protocol() -> None:
    from nexus.app.config import ModelConfig

    model = CompactionModel(ModelConfig("summary", 128000), client=object())
    for messages, tools in [
        ([Message("user", "text")], [ToolSpec("exec_command", "execute", {})]),
        ([Message("assistant", "text", protocol_data={"private": "secret"})], []),
        ([Message("tool", "text")], []),
    ]:
        with pytest.raises(ModelError, match="invalid_compaction_request"):
            await model.complete(messages, tools, Recorder())


async def test_independent_clients_keys_wire_options_and_hidden_summary(monkeypatch: Any) -> None:
    import json

    import httpx
    from openai import AsyncOpenAI

    from nexus.app.config import ModelConfig
    from nexus.core.compaction import COMPACTION_PROMPT
    from nexus.core.model import ChatModel
    from tests.test_model import chunk, sse

    requests: list[tuple[str, str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(
            (request.url.host, request.headers["authorization"], json.loads(request.content))
        )
        return httpx.Response(200, content=sse([chunk({"content": "{}"}, "stop")]))

    configs = [
        ModelConfig("main", 128000, "https://main.example/v1", "MAIN_KEY", reasoning_effort="high"),
        ModelConfig(
            "summary", 128000, "https://summary.example/v1", "SUMMARY_KEY", enable_thinking=False
        ),
    ]
    monkeypatch.setenv("MAIN_KEY", "main-secret")
    monkeypatch.setenv("SUMMARY_KEY", "summary-secret")
    main_client, summary_client = [
        AsyncOpenAI(
            api_key=c.key(),
            base_url=c.base_url,
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
        )
        for c in configs
    ]
    main = ChatModel(configs[0], client=main_client)
    compactor = CompactionModel(configs[1], client=summary_client)
    try:
        await main.complete(
            [Message("user", "task")], [ToolSpec("exec_command", "run", {})], Recorder()
        )
        events = Recorder()
        await compactor.complete(
            [Message("system", COMPACTION_PROMPT), Message("user", "history")], [], events
        )
        assert requests[0][:2] == ("main.example", "Bearer main-secret")
        assert requests[1][:2] == ("summary.example", "Bearer summary-secret")
        assert requests[0][2]["reasoning_effort"] == "high"
        assert "enable_thinking" not in requests[0][2]
        assert requests[1][2]["enable_thinking"] is False
        assert requests[1][2]["response_format"] == {"type": "json_object"}
        assert "reasoning_effort" not in requests[1][2] and "tools" not in requests[1][2]
        assert all(
            k != "assistant_delta" and d["purpose"] == "compaction" for k, d in events.events
        )
    finally:
        await main.close()
        await compactor.close()
