from __future__ import annotations

import asyncio
import io
import json
import time
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from openai import AsyncOpenAI
from rich.console import Console

from nexus.app import bootstrap, memory_daily
from nexus.app.memory import MemoryStore
from nexus.app.session import read_records
from nexus.app.tui import Transcript
from nexus.core.model import ChatModel
from nexus.core.types import RuntimeEvent
from tests.conftest import reply
from tests.test_conversation import config, ignore
from tests.test_memory_daily import SUMMARY, seed_short
from tests.test_model import chunk, sse


async def test_exit_low_on_wire_preserves_coding_and_compaction(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    cfg = config(monkeypatch)
    cfg.model.reasoning_effort = "high"
    cfg.compaction_model = replace(cfg.model, name="independent-compactor", reasoning_effort=None)
    before = asdict(cfg)
    requests: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, content=sse([chunk({"content": SUMMARY}, "stop")]))

    def factory(value: Any) -> ChatModel:
        return ChatModel(
            value,
            client=AsyncOpenAI(
                api_key="test-only",
                max_retries=0,
                http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
            ),
        )

    async def start(self: Any) -> None:
        pass

    monkeypatch.setattr(bootstrap, "ChatModel", factory)
    monkeypatch.setattr(bootstrap.Conversation, "start_memory", start)
    convo = bootstrap.Conversation(tmp_path, cfg, ignore, home=tmp_path)
    try:
        assert (await convo.turn("Keep Markdown as authority")).outcome == "completed"
        assert (
            await convo.turn("Preserve failures and leave dogfood pending")
        ).outcome == "completed"
        assert convo.model and convo.compaction_model
        main, compactor = convo.model, convo.compaction_model
        await convo.summarize_exit("slash-exit")
        assert [r["reasoning_effort"] for r in requests] == ["high", "high", "low"]
        assert requests[-1]["model"] == cfg.model.name
        assert requests[-1]["max_tokens"] == 2048 and "tools" not in requests[-1]
        assert requests[-1]["messages"][0]["content"] == memory_daily.SYSTEM_PROMPT
        assert requests[0]["max_tokens"] == cfg.model.max_output_tokens
        assert asdict(cfg) == before
        assert convo.model is main and main.config is cfg.model
        assert convo.compaction_model is compactor and compactor.config is cfg.compaction_model
    finally:
        await convo.close()


@pytest.mark.parametrize("reason", ["slash-exit", "eof"])
@pytest.mark.parametrize("case", ["success", "timeout", "api", "disk", "invalid", "cleanup"])
async def test_exit_is_silent_but_persisted(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
    reason: str,
    case: str,
) -> None:
    output = io.StringIO()
    transcript = Transcript(Console(file=output))
    delivered: list[RuntimeEvent] = []

    async def consumer(event: RuntimeEvent) -> None:
        delivered.append(event)
        await transcript(event)

    convo = bootstrap.Conversation(tmp_path, config(monkeypatch), consumer, home=tmp_path)
    await seed_short(convo)
    assert convo.events and convo.writer
    await convo.events("warning", {"detail": "Ordinary warning remains visible"})
    assert "Ordinary warning remains visible" in output.getvalue()
    output.seek(0)
    output.truncate()
    delivered.clear()
    transcript.stop_activity()
    closed: list[bool] = []

    class Model:
        async def complete(self, messages: Any, tools: Any, emit: Any) -> Any:
            if case == "timeout":
                await asyncio.sleep(30)
            if case == "api":
                raise RuntimeError("provider failed")
            return reply(text="invalid" if case == "invalid" else SUMMARY)

        async def close(self) -> None:
            try:
                if case == "cleanup":
                    await asyncio.sleep(30)
            finally:
                closed.append(True)

    monkeypatch.setattr(bootstrap, "ChatModel", lambda _: Model())
    assert memory_daily.EXIT_TIMEOUT == 10
    if case == "timeout":
        monkeypatch.setattr(memory_daily, "EXIT_TIMEOUT", 0.02)
        # A previously failed terminal must not make exit warnings use stderr fallback.
        convo.events.consumer_failed = True
    if case == "disk":

        def fail(*args: Any) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(MemoryStore, "write", fail)
    started = time.monotonic()
    await convo.summarize_exit(reason)
    await convo.close()
    assert time.monotonic() - started < 2
    assert closed and convo.closed
    assert not delivered and output.getvalue() == ""
    assert capsys.readouterr() == ("", "")
    with convo.writer.path.open("rb") as stream:
        records, _ = read_records(stream)
    daily = [r["data"] for r in records if r["kind"] == "memory_daily"]
    assert daily[0]["status"] == "summarizing"
    assert daily[-1]["duration_ms"] >= 0
    assert daily[-1]["status"] == ("written" if case in {"success", "cleanup"} else "skipped")
    if case in {"timeout", "api", "disk"}:
        assert (
            daily[-1]["reason"]
            == {
                "timeout": "TimeoutError",
                "api": "RuntimeError",
                "disk": "OSError",
            }[case]
        )
        assert any(
            r["kind"] == "warning" and "Memory exit summary skipped" in r["data"]["detail"]
            for r in records
        )
    else:
        assert "usage" in daily[-1]
