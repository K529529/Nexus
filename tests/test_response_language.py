from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from rich.console import Console

from nexus.app import bootstrap
from nexus.app.config import ConfigError, load_config
from nexus.app.tui import Transcript
from nexus.core.types import Json
from tests.test_model import chunk, model_for, sse


@pytest.mark.parametrize("language", [None, "zh-CN", "en", "auto"])
async def test_language_reaches_wire_without_reasoning_translation(
    tmp_path: Path, monkeypatch: Any, language: str | None
) -> None:
    config_path = tmp_path / "settings.toml"
    source = '[model]\nname="test"\ncontext_window=32768\n'
    if language:
        source += f'[runtime]\nresponse_language="{language}"\n'
    config_path.write_text(source, encoding="utf-8")
    config = load_config(config_path, require_key=False)
    assert config.response_language == (language or "zh-CN")
    monkeypatch.setenv("NEXUS_MODEL_API_KEY", "test-only")
    requests: list[Json] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            content=sse(
                [
                    chunk({"reasoning_content": "PRIVATE ENGLISH REASONING"}),
                    chunk({"content": "已完成检查。"}, "stop"),
                ]
            ),
        )

    model = model_for(handle)
    monkeypatch.setattr(bootstrap, "ChatModel", lambda _: model)
    stream = io.StringIO()
    conversation = bootstrap.Conversation(
        tmp_path, config, Transcript(Console(file=stream)), home=tmp_path, registry={}
    )
    try:
        result = await conversation.turn("检查代码")
        assert result.outcome == "completed" and len(requests) == 1
        prompt = requests[0]["messages"][0]["content"]
        expected = {
            None: "简体中文",
            "zh-CN": "简体中文",
            "en": "English",
            "auto": "latest request",
        }
        assert expected[language] in prompt
        assert "unless the user explicitly requests" in prompt
        assert "已完成检查。" in stream.getvalue() and "PRIVATE" not in stream.getvalue()
        message = conversation.session.messages[-1]
        assert message.protocol_data and "PRIVATE" in str(message.protocol_data)
        assert "response_language" not in requests[0]  # No invented provider parameter.
    finally:
        await conversation.close()


@pytest.mark.parametrize("setting", ['"unknown"', '""', "1", "true", "[]"])
def test_invalid_language_is_an_explicit_config_error(tmp_path: Path, setting: str) -> None:
    path = tmp_path / "settings.toml"
    path.write_text(
        '[model]\nname="test"\ncontext_window=32768\n[runtime]\n'
        + f"response_language={setting}\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="runtime.response_language"):
        load_config(path, require_key=False)
