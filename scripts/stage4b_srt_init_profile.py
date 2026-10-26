"""Opt-in init-only root attribution. No exec, provisioning, or direct ACL calls.

Run sequentially with an externally installed wheel and stage4b_srt_trace.mjs.
Reduced configurations measure initialization only; they are never production
policies and never execute a shell or Python worker.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from uuid import uuid4

from nexus.core.security import SecurityPolicy
from nexus.tools.sandbox import BridgeClient, SRTBackend


async def run(output: Path, parent: Path, shell: str) -> None:
    root = parent / ("nexus-p0-init-profile-" + uuid4().hex)
    root.mkdir()
    rows = []
    for label, candidates in (
        ("shell-only", [str(Path(shell).parent)]),
        ("shell-and-venv", [str(Path(shell).parent), sys.prefix]),
        ("shell-and-base-python", [str(Path(shell).parent), sys.base_prefix]),
    ):
        workspace = root / label
        workspace.mkdir()
        backend = SRTBackend(SecurityPolicy(mode="workspace-write"))
        argv, params = backend.installation(workspace, shell)
        params["read_roots"] = candidates
        client = BridgeClient(argv, cwd=Path(argv[1]).parent)
        row = {"case": label, "workspace": str(workspace), "read_candidates": candidates}
        started = time.perf_counter()
        try:
            await client.initialize(params)
            row["initialized"] = True
        except Exception as exc:
            row["error"] = str(exc)
        finally:
            row["initialize_ms"] = (time.perf_counter() - started) * 1000
            started = time.perf_counter()
            await client.close()
            row["close_ms"] = (time.perf_counter() - started) * 1000
            row["cleanup_confirmed"] = client.cleanup_confirmed
            rows.append(row)
            await asyncio.to_thread(output.write_text, json.dumps(rows, indent=2), encoding="utf-8")
            print(json.dumps(row), flush=True)
        if not row.get("initialized") or not client.cleanup_confirmed:
            raise RuntimeError("Stop profiling after failed initialization or cleanup")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--temp-root", type=Path, required=True)
    parser.add_argument("--shell", required=True)
    args = parser.parse_args()
    asyncio.run(run(args.output, args.temp_root, args.shell))
