"""Real CLI processes and QMD, isolated user profile; no model API calls."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

from nexus.app.memory import MemoryStore


def run(args: argparse.Namespace) -> None:
    root, output = Path(args.home).resolve(), Path(args.output).resolve()
    root.mkdir(parents=True, exist_ok=False)
    output.mkdir(parents=True, exist_ok=False)
    store = MemoryStore(root / ".nexus/memory")
    item = store.write("MEMORY.md", "FilePilot retains evidence for seven days.")
    recovery = store.forget("MEMORY.md", item["id"], item["revision"])
    store.restore(recovery["recovery_id"])
    config = root / ".nexus/config.toml"
    config.write_text(
        '# Preserve this comment\n[model]\nname="not-called"\n[runtime]\nmax_steps=9\n'
        "[memory]\nenabled=true\nqmd_command=" + json.dumps([args.node, args.qmd_entry]) + "\n",
        encoding="utf-8",
    )
    original = tomllib.loads(config.read_text(encoding="utf-8"))
    env = {
        **os.environ,
        "USERPROFILE": str(root),
        "HOME": str(root),
        "LOCALAPPDATA": str(root / "local"),
        "XDG_CACHE_HOME": args.cache,
        "PATH": str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32")
        if os.name == "nt"
        else "",
    }
    authority = {
        str(p.relative_to(store.root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in [*store.root.glob("*.md"), *store.root.glob("recovery/*.json")]
    }
    models = {
        p.name: (p.stat().st_size, p.stat().st_mtime_ns) for p in Path(args.cache).rglob("*.gguf")
    }
    receipts = []
    report = {"status": "RUNNING"}
    try:
        for action in ("off", "status", "on", "status", "off", "status"):
            result = subprocess.run(
                [sys.executable, "-m", "nexus", "memory", action],
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=1800,
            )
            receipts.append(
                {
                    "action": action,
                    "returncode": result.returncode,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                }
            )
            print(action, result.returncode, flush=True)
            assert result.returncode == 0, receipts[-1]
            data = tomllib.loads(config.read_text(encoding="utf-8"))
            if action != "status":
                assert data["memory"]["enabled"] == (action == "on")
            data["memory"]["enabled"] = original["memory"]["enabled"]
            assert data == original
            assert "# Preserve this comment" in config.read_text(encoding="utf-8")
        after = {
            str(p.relative_to(store.root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in [*store.root.glob("*.md"), *store.root.glob("recovery/*.json")]
        }
        assert authority == after
        assert (store.root / ".retrieval/index.sqlite").is_file()
        assert models == {
            p.name: (p.stat().st_size, p.stat().st_mtime_ns)
            for p in Path(args.cache).rglob("*.gguf")
        }
        report.update(
            status="PASS",
            authority_and_recovery_preserved=True,
            index_preserved=True,
            model_files_unchanged=True,
            unrelated_config_preserved=True,
        )
    except BaseException as exc:
        report.update(status="FAIL", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        report["receipts"] = receipts
        (output / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    for option in ("home", "output", "node", "qmd-entry", "cache"):
        parser.add_argument("--" + option, required=True)
    run(parser.parse_args())
