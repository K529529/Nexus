"""Opt-in real install acceptance. No paid API, isolated dependencies and GGUF cache."""
# ruff: noqa: ASYNC240 -- one-shot evidence script, local fixture file I/O
# ruff: noqa: ASYNC240 -- one-shot evidence script, local fixture file I/O

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import time
from pathlib import Path

from nexus.app.memory import MemoryStore
from nexus.app.memory_index import MemoryIndex
from nexus.app.memory_setup import DependencyBootstrap
from scripts.memory_qmd_smoke import wait_ready


async def run(args: argparse.Namespace) -> None:
    root, output = Path(args.home), Path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    if args.phase == "fresh" and root.exists():
        raise ValueError("Fresh phase requires a nonexistent home")
    env = dict(os.environ)
    env["PATH"] = (
        str(Path(env.get("SystemRoot", "C:/Windows")) / "System32") if os.name == "nt" else ""
    )
    env["XDG_CACHE_HOME"] = str(root / "cache")
    env["npm_config_cache"] = str(root / "npm-cache")
    assert not shutil.which("node", path=env["PATH"])
    assert not shutil.which("qmd", path=env["PATH"])
    installer = DependencyBootstrap(root / "dependencies", env=env)
    store = MemoryStore(root / "memory")
    if args.phase == "fresh":
        store.write(
            "daily/2026-09-01.md",
            "FilePilot directory cleanup must skip junctions and preserve inaccessible paths; "
            "never change permissions to bypass access denied.",
        )
    index = MemoryIndex(store, prepare=installer.ensure)
    index.env.update({key: env[key] for key in ("PATH", "XDG_CACHE_HOME")})
    events = []
    report = {"phase": args.phase, "status": "RUNNING", "node_on_path": False, "qmd_on_path": False}
    before = {
        str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns)
        for p in (root / "cache").rglob("*.gguf")
    }
    started = time.monotonic()

    async def emit(kind: str, data: dict, **kwargs: object) -> int:
        events.append({"kind": kind, "data": data})
        print(json.dumps({"kind": kind, **data}, ensure_ascii=False), flush=True)
        return len(events)

    try:
        index.start(emit)
        await wait_ready(index)
        result = await index.search(
            "FilePilot 清理目录遇到 junction 和 access denied 应如何处理？", 3
        )
        assert result["results"], result
        after = {
            str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns)
            for p in (root / "cache").rglob("*.gguf")
        }
        report.update(
            status="PASS",
            command=index.command,
            search=result,
            models=after,
            models_unchanged=before == after if before else None,
            index_reused=not any(
                e["kind"] == "memory_index" and e["data"]["status"] in {"updating", "embedding"}
                for e in events
            ),
            dependencies_reused=not any(
                e["kind"] == "memory_setup" and e["data"]["stage"] in {"node", "qmd"}
                for e in events
            ),
        )
        if args.phase == "reuse":
            assert (
                report["models_unchanged"]
                and report["index_reused"]
                and report["dependencies_reused"]
            )
    except BaseException as exc:
        report.update(status="FAIL", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        await index.close()
        report.update(seconds=time.monotonic() - started, events=events)
        (output / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=["fresh", "reuse"], required=True)
    parser.add_argument("--home", required=True)
    parser.add_argument("--output", required=True)
    options = parser.parse_args()
    options.home, options.output = (
        str(Path(options.home).resolve()),
        str(Path(options.output).resolve()),
    )
    asyncio.run(run(options))
