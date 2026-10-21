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
