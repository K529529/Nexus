"""Opt-in paid Stage7 model/tool/switch/OTLP smoke using local credentials."""

from __future__ import annotations

import asyncio
import json
import tempfile
from dataclasses import replace
from pathlib import Path

from nexus.app.bootstrap import Conversation
from nexus.app.config import ModelConfig, load_config
from nexus.app.session import read_records
from nexus.app.tracing import project_run
from nexus.core.types import RuntimeEvent


async def main() -> None:
    root = Path(tempfile.mkdtemp(prefix="nexus-stage7-model-smoke-"))
    models = {
        "qwen3_8flash": ModelConfig(
            "qwen3.8-flash",
            128000,
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "DASHSCOPE_API_KEY",
            max_output_tokens=2048,
            enable_thinking=True,
            provider="dashscope",
        ),
        "deepseek_v4_pro": ModelConfig(
            "deepseek-v4-pro",
            128000,
            "https://api.deepseek.com/v1",
            "DEEPSEEK_API_KEY",
            max_output_tokens=2048,
            reasoning_effort="high",
            provider="deepseek",
        ),
    }
    config = load_config()
    config = replace(
        config,
        models=models,
        memory_enabled=False,
        mcp_servers={},
        web=replace(config.web, enabled=False),
    )

    async def consume(event: RuntimeEvent) -> None:
        pass

    conversation = Conversation(root, config, consume, home=root / "state")
    evidence = []
    try:
        for alias in models:
            await conversation.select_model(alias)
            record: dict[str, object] = {
                "alias": alias,
                "model": conversation.config.model.name,
                "provider": conversation.config.model.provider,
            }
            try:
                result = await conversation.turn(
                    f"Use exec_command once to print nexus-stage7-{alias}. "
                    "Then reply briefly with the printed text. Do not read or change files. "
                    "This is a new verification: execute the command even if a similar task "
                    "appears earlier."
                )
                record.update(
                    outcome=result.outcome,
                    reason=result.reason,
                    tool_calls=result.tool_calls,
                    model_calls=result.model_calls,
                )
            except Exception as exc:
                record.update(outcome="exception", exception_type=type(exc).__name__)
            if conversation.writer:
                with conversation.writer.path.open("rb") as stream:
                    rows, _ = read_records(stream)
                run_id = conversation.session.run_id or ""
                spans, diagnostics = project_run(rows, run_id)
                record.update(
                    run_id=run_id,
                    session_path=str(conversation.writer.path),
                    diagnostics=diagnostics,
                    trace_id=f"{spans[0]['trace_id']:032x}" if spans else None,
                    root_span_id=f"{spans[0]['span_id']:016x}" if spans else None,
                    private_continuation_saved=any(
                        m.protocol_data for m in conversation.session.messages if m.run_id == run_id
                    ),
                )
            evidence.append(record)
            print(json.dumps(record), flush=True)
    finally:
        await conversation.close()
        output = Path("docs/next/evidence/stage7-otel/litellm-live.json")
        await asyncio.to_thread(output.write_text, json.dumps(evidence, indent=2), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
