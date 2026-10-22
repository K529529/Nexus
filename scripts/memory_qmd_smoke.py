"""Opt-in real QMD acceptance; isolated authority and raw receipts, no remote LLM."""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import time
from pathlib import Path
from typing import Any

from nexus.app.memory import MemoryStore
from nexus.app.memory_index import MemoryIndex


async def wait_ready(index: MemoryIndex) -> None:
    async with asyncio.timeout(1800):
        while index.state.get("status") != "ready" or index.client is None:
            if index.task and index.task.done():
                raise RuntimeError(str(index.state))
            if index.state.get("status") in {"failed", "unavailable"}:
                raise RuntimeError(str(index.state))
            index.updated.clear()
            await asyncio.wait_for(index.updated.wait(), 1800)


async def run(args: argparse.Namespace) -> None:
    root = Path(args.home)
    if (root / "MEMORY.md").exists():
        raise ValueError(
            "Use a fresh dedicated acceptance directory; never overwrite prior attempts"
        )
    output = Path(args.output)
    store = MemoryStore(root)
    store.write(
        "MEMORY.md",
        "Nexus engineering preference: use pytest for Python regression tests. "
        "Keep failed evaluation attempts and never count mocks as real acceptance.",
    )
    store.write(
        "daily/2026-09-01.md",
        "FilePilot filesystem decision: do not traverse directory "
        "junctions during cleanup. Preserve inaccessible paths and report permission errors.",
    )
    store.write(
        "daily/2026-09-02.md",
        "The development web server listens on port 4317. "
        "The client polls operation status every two seconds.",
    )
    store.write(
        "daily/2026-09-03.md",
        "The build uses TypeScript strict mode. "
        "Keep package-lock.json committed for reproducible npm installs.",
    )
    store.write(
        "daily/2026-09-04.md",
        "Nexus uses one message-driven agent loop. "
        "Session JSONL recovery restores conversation without replaying shell side effects.",
    )
    index = MemoryIndex(store, (shutil.which("node") or "node", args.qmd_entry))
    events: list[dict[str, Any]] = []
    report: dict[str, Any] = {"status": "RUNNING", "queries": []}

    async def emit(kind: str, data: dict[str, Any], **kwargs: Any) -> int:
        events.append({"kind": kind, "data": data})
        print(json.dumps({"kind": kind, "status": data.get("status")}), flush=True)
        return len(events)

    started = time.monotonic()
    try:
        index.start(emit)
        await wait_ready(index)
        for query in [
            "FilePilot 清理目录时如何处理 junction 和拒绝访问？",
            "恢复 session 是否会重新执行之前的 shell 命令",
            "Python regression pytest",
        ]:
            before = time.monotonic()
            result = await index.search(query, 5)
            report["queries"].append(
                {"query": query, "seconds": time.monotonic() - before, "response": result}
            )
        status = await index.client.call_tool("status", {})
        report["qmd_status"] = status.structured_content
        expected = ["2026-09-01.md", "2026-09-04.md", "MEMORY.md"]
        for row, suffix in zip(report["queries"], expected, strict=True):
            assert row["response"]["results"][0]["file"].endswith(suffix)
        same_client = index.client
        added = store.write(
            "daily/2026-09-05.md", "FilePilot acceptance sentinel: cobalt-otter-927."
        )
        index.changed()
        await wait_ready(index)
        report["after_write"] = await index.search("cobalt-otter-927", 5)
        assert "cobalt-otter-927" in str(report["after_write"])
        removed = store.forget("daily/2026-09-05.md", added["id"], added["revision"])
        index.changed()
        await wait_ready(index)
        report["after_forget"] = await index.search("cobalt-otter-927", 5)
        assert "cobalt-otter-927" not in str(report["after_forget"])
        store.restore(removed["recovery_id"])
        index.changed()
        await wait_ready(index)
        report["after_restore"] = await index.search("cobalt-otter-927", 5)
        assert "cobalt-otter-927" in str(report["after_restore"])
        assert index.client is same_client
        report["reused_mcp_connection"] = True
        await index.close()
        # CLI explain is a separate official pipeline receipt, not a mock or MCP score claim.
        explanation = await index._command(
            "query",
            "lex: FilePilot junction cleanup\nvec: How to safely clean directory junctions",
            "--explain",
            "--format",
            "json",
            "-c",
            "nexus-memory",
        )
        (output / "qmd-explain.txt").write_text(explanation, encoding="utf-8")
        report["status"] = "PASS"
    except BaseException as exc:
        report["status"] = "FAIL"
        report["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        await index.close()
        report["seconds"] = time.monotonic() - started
        report["events"] = events
        (output / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2).replace(
                json.dumps(str(root))[1:-1], "<acceptance-memory>"
            ),
            encoding="utf-8",
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", required=True)
    parser.add_argument("--qmd-entry", required=True)
    parser.add_argument("--output", required=True)
    options = parser.parse_args()
    options.home = str(Path(options.home).resolve())
    options.output = str(Path(options.output).resolve())
    Path(options.output).mkdir(parents=True, exist_ok=False)
    asyncio.run(run(options))
