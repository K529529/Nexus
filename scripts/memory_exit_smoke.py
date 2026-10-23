"""Opt-in paid Exit Summary acceptance; copied session, fixed production timeout."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import tempfile
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
from openai import AsyncOpenAI
from rich.console import Console

from nexus.app import bootstrap, memory_daily
from nexus.app.config import ModelConfig, config_path, load_config
from nexus.app.memory import entries
from nexus.app.session import read_records, session_directory
from nexus.app.tui import Transcript
from nexus.core.model import ChatModel


async def run(source: Path, output: Path) -> None:
    await asyncio.to_thread(output.mkdir, parents=True, exist_ok=False)
    original = await asyncio.to_thread(source.read_bytes)
    first = json.loads(original.splitlines()[0])
    workspace = Path(first["data"]["workspace"])
    home = Path(tempfile.mkdtemp(prefix="nexus-exit-low-"))
    copied = session_directory(workspace, home) / source.name
    copied.parent.mkdir(parents=True)
    copied.write_bytes(original)
    cfg = load_config()
    cfg.memory_enabled = True
    before = asdict(cfg)
    config_bytes = config_path().read_bytes()
    assert cfg.model.name == "deepseek-flash" and cfg.limits.context_window == 128000
    assert memory_daily.EXIT_TIMEOUT == 10 and memory_daily.EXIT_OUTPUT_TOKENS == 2048
    wire: list[dict[str, Any]] = []

    async def capture(request: httpx.Request) -> None:
        body = json.loads(request.content)
        wire.append(
            {
                "model": body["model"],
                "reasoning_effort": body.get("reasoning_effort"),
                "max_tokens": body.get("max_tokens"),
                "tools_present": "tools" in body,
                "system_prompt_sha256": hashlib.sha256(
                    body["messages"][0]["content"].encode()
                ).hexdigest(),
                "request_body_sha256": hashlib.sha256(request.content).hexdigest(),
            }
        )

    def factory(value: ModelConfig) -> ChatModel:
        return ChatModel(
            value,
            client=AsyncOpenAI(
                api_key=value.key(),
                base_url=value.base_url,
                timeout=value.request_timeout_seconds,
                max_retries=0,
                http_client=httpx.AsyncClient(event_hooks={"request": [capture]}),
            ),
        )

    terminal = io.StringIO()
    transcript = Transcript(Console(file=terminal))
    convo = bootstrap.Conversation(workspace, cfg, transcript, home=home, resume=copied)
    report: dict[str, Any] = {
        "source": str(source),
        "source_sha256": hashlib.sha256(original).hexdigest(),
        "isolated_home": str(home),
        "copied_session": str(copied),
        "exit_timeout_seconds": 10,
        "output_limit": 2048,
        "coding_reasoning_effort": cfg.model.reasoning_effort,
        "compaction_called": False,
        "qmd_started": False,
        "wire_requests": wire,
    }
    try:
        assert convo.memory is not None
        prepared = memory_daily.exit_request(
            convo.session,
            convo.context.main_estimator,
            cfg.limits.context_window,
            convo.secret_values(),
        )
        assert prepared is not None
        request, budget = prepared
        report["input_budget"] = budget
        report["estimated_input_tokens"] = convo.context.main_estimator.count(request, [])
        assert budget == 25600 and report["estimated_input_tokens"] <= budget
        name = memory_daily.daily_name(datetime.now().astimezone())
        transcript.stop_activity()
        started = time.monotonic()
        with patch.object(bootstrap, "ChatModel", factory):
            await convo.summarize_exit("slash-exit")
        report["summary_and_model_cleanup_seconds"] = round(time.monotonic() - started, 3)
        if convo.memory.read(name):
            (output / "exit-summary.md").write_text(
                entries(convo.memory.read(name))[-1].content + "\n", encoding="utf-8"
            )
    finally:
        await convo.close()
        with copied.open("rb") as stream:
            records, _ = read_records(stream)
        events = [r for r in records if r["seq"] > json.loads(original.splitlines()[-1])["seq"]]
        diagnostics = [r for r in events if r["kind"] in {"memory_daily", "warning"}]
        (output / "exit-events.jsonl").write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in diagnostics), encoding="utf-8"
        )
        report["exit_events"] = [{"kind": r["kind"], "data": r["data"]} for r in diagnostics]
        report["daily_written"] = any(
            r["kind"] == "memory_daily" and r["data"].get("status") == "written"
            for r in diagnostics
        )
        report["terminal_output"] = terminal.getvalue()
        report["source_unchanged"] = await asyncio.to_thread(source.read_bytes) == original
        report["config_unchanged"] = (
            asdict(cfg) == before and config_path().read_bytes() == config_bytes
        )
        (output / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(json.dumps(report, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    asyncio.run(run(args.source, args.output))
