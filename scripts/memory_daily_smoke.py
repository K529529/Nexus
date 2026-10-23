"""Opt-in paid acceptance on an isolated copy of a real completed session.

Never executes a coding Run or writes to the original workspace/log/memory.
The diagnostic timeout override is for measured experiments, not a product setting.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from nexus.app import memory_daily
from nexus.app.bootstrap import Conversation
from nexus.app.config import load_config
from nexus.app.memory import entries
from nexus.app.memory_index import installed_command
from nexus.app.session import session_directory
from nexus.core.history import ContextBuilder
from nexus.core.types import RuntimeEvent


async def run(args: argparse.Namespace) -> None:
    output = Path(args.output)
    await asyncio.to_thread(output.mkdir, parents=True, exist_ok=False)
    source = Path(args.source)
    original = await asyncio.to_thread(source.read_bytes)
    first = json.loads(original.splitlines()[0])
    workspace = Path(first["data"]["workspace"])
    home = Path(tempfile.mkdtemp(prefix="nexus-daily-acceptance-"))
    copied = session_directory(workspace, home) / source.name
    copied.parent.mkdir(parents=True)
    copied.write_bytes(original)
    cfg = load_config()
    cfg.memory_enabled = True
    cfg.memory_qmd_command = installed_command()
    memory_daily.EXIT_TIMEOUT = args.timeout
    events: list[dict[str, Any]] = []
    report: dict[str, Any] = {
        "source_sha256": hashlib.sha256(original).hexdigest(),
        "source_session": first["session_id"],
        "isolated_home": str(home),
        "mode": "real completed dogfood session, isolated resume; no coding Run",
        "exit_timeout_seconds": args.timeout,
        "model": cfg.model.name,
        "context_window": cfg.limits.context_window,
        "max_output_tokens": cfg.model.max_output_tokens,
        "summary_output_tokens": memory_daily.EXIT_OUTPUT_TOKENS,
        "prompt_system_sha256": hashlib.sha256(memory_daily.SYSTEM_PROMPT.encode()).hexdigest(),
        "qmd_command": list(cfg.memory_qmd_command),
    }
    cache = Path.home() / ".cache/qmd/models"

    def models() -> dict[str, Any]:
        return {p.name: [p.stat().st_size, p.stat().st_mtime_ns] for p in cache.glob("*.gguf")}

    cache_before = models()

    async def consumer(event: RuntimeEvent) -> None:
        if event.kind in {
            "memory_daily",
            "memory_index",
            "compaction_usage",
            "compaction_failed",
            "context_compacted",
            "warning",
        }:
            events.append({"kind": event.kind, "data": event.data})
            print(
                json.dumps(
                    {
                        "kind": event.kind,
                        "status": event.data.get("status"),
                        "detail": event.data.get("detail"),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

    convo = Conversation(workspace, cfg, consumer, home=home, resume=copied)
    assert convo.memory is not None
    name = memory_daily.daily_name(datetime.now().astimezone())
    # Controlled setup, explicitly labelled; factual TODO comes from this user's
    # original FilePilot session. These notes do not assert a completed fix.
    convo.memory.scratchpad(
        "add",
        "Nexus: improve patch matching and long-context patch history; "
        "ask FilePilot dogfooding for feedback after the change.",
    )
    convo.memory.write(
        name,
        "Acceptance fixture: FilePilot Rename dogfood session; preserve failures. "
        "Nexus patch/history improvements are pending, not implemented by this test.",
    )
    prior = convo.memory.read(name)
    started = time.monotonic()
    try:
        active = ContextBuilder().build_active_context(convo.session)
        report["active_messages_before"] = len(active)
        report["tokens_before"] = convo.context.tokens(active, [])
        report["compacted"] = await convo.compact() if not args.skip_compaction else False
        if report["compacted"]:
            (output / "handoff.md").write_text(
                entries(convo.memory.read(name))[-1].content + "\n", encoding="utf-8"
            )
        report["snapshot_has_handoff"] = "Session Handoff" in (
            convo.session.memory_snapshot or {}
        ).get("content", "")
        request = memory_daily.exit_request(
            convo.session,
            convo.context.main_estimator,
            cfg.limits.context_window,
            convo.secret_values(),
        )
        assert request is not None
        report["exit_estimated_input_tokens"] = convo.context.main_estimator.count(request[0], [])
        report["exit_input_budget"] = request[1]
        report["input_truncated"] = "Transcript truncated" in request[0][1].content
        await convo.summarize_exit("slash-exit")
        report["exit_written"] = any(
            e["kind"] == "memory_daily" and e["data"].get("status") == "written" for e in events
        )
        report["original_daily_preserved"] = convo.memory.read(name).startswith(prior)
        if report["exit_written"]:
            (output / "exit-summary.md").write_text(
                entries(convo.memory.read(name))[-1].content + "\n", encoding="utf-8"
            )
        await convo.close()
        fresh = Conversation(workspace, cfg, consumer, home=home)
        try:
            fresh.ensure_events("Daily retrieval acceptance")
            prefix = await fresh.memory_prefix()
            report["fresh_snapshot_has_daily"] = "Session Summary" in prefix
            await fresh.start_memory()
            assert fresh.memory_index is not None
            await fresh.memory_index.wait_ready()
            if args.lexical:
                response = await fresh.memory_index.client.call_tool(
                    "query",
                    {
                        "searches": [{"type": "lex", "query": "FilePilot"}],
                        "collections": ["nexus-memory"],
                        "limit": 5,
                        "rerank": False,
                    },
                )
                assert not response.is_error
                report["search"] = response.structured_content
                report["search_mode"] = "qmd-mcp-lexical-no-rerank"
            else:
                report["search"] = await fresh.memory_index.search(
                    "FilePilot Rename patch history pending", 5
                )
                report["search_mode"] = "nexus-hybrid-rerank"
            report["daily_retrieved"] = any(
                str(row.get("file", "")).endswith(name) for row in report["search"]["results"]
            )
        finally:
            await fresh.close()
    finally:
        await convo.close()
        report["source_unchanged"] = await asyncio.to_thread(source.read_bytes) == original
        report["gguf_unchanged"] = cache_before == models()
        report["duration_seconds"] = round(time.monotonic() - started, 3)
        report["events"] = events
        (output / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(
            json.dumps(
                {
                    key: report.get(key)
                    for key in [
                        "compacted",
                        "exit_written",
                        "daily_retrieved",
                        "source_unchanged",
                        "gguf_unchanged",
                        "duration_seconds",
                    ]
                }
            ),
            flush=True,
        )


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--timeout", type=int, default=10)
    parser.add_argument("--skip-compaction", action="store_true")
    parser.add_argument(
        "--lexical",
        action="store_true",
        help="Index verification without GPU inference; not a hybrid-search acceptance",
    )
    asyncio.run(run(parser.parse_args()))
