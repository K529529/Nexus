"""Real Windows console PTY / PowerShell -> WSL input and rendered-screen probe."""
# ruff: noqa: B023 -- callbacks are used synchronously within each joined probe case

from __future__ import annotations

import argparse
import hashlib
import json
import os
import select
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pyte
from winpty import PtyProcess


def main(args):
    script = Path(__file__).with_name("stage4_tui_terminal_probe.py").resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    report = {"backend": args.backend, "wsl": args.wsl, "rows": []}
    for case in [args.case] if args.case else ("rounds", "raw-paste", "bracketed-paste"):
        with tempfile.TemporaryDirectory(prefix="nx4d-") as directory:
            state = Path(directory) / "state.json"
            if args.wsl:

                def linux(path):
                    p = path.as_posix()
                    return "/mnt/" + p[0].lower() + p[2:]

                command = subprocess.list2cmdline(
                    [
                        "wsl.exe",
                        "-d",
                        "Ubuntu",
                        "--",
                        args.python,
                        "-I",
                        linux(script),
                        "--child",
                        linux(state),
                        "--case",
                        case,
                    ]
                )
            else:
                command = "& " + " ".join(
                    "'" + s.replace("'", "''") + "'"
                    for s in (args.python, "-I", str(script), "--child", str(state), "--case", case)
                )
            proc = PtyProcess.spawn(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-Command",
                    command,
                ],
                dimensions=(30, 90),
                backend=args.backend,
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            )
            screen = pyte.Screen(90, 30)
            stream = pyte.Stream(screen)
            raw = []

            def pump(duration=0.05):
                end = time.monotonic() + duration
                while time.monotonic() < end:
                    if select.select([proc.fileobj], [], [], 0.01)[0]:
                        try:
                            data = proc.read(65536)
                        except EOFError:
                            return
                        raw.append(data)
                        stream.feed(data)

            def status():
                if not state.exists():
                    return {}
                lines = state.read_text(encoding="utf-8").splitlines(keepends=True)
                return next((json.loads(s) for s in reversed(lines) if s.endswith("\n")), {})

            def until(predicate):
                end = time.monotonic() + 15
                while time.monotonic() < end:
                    pump()
                    if predicate(status()):
                        return
                raise TimeoutError(case)

            snapshots = []
            console_snapshots = []
            try:
                until(lambda s: s.get("running") or s.get("approval_running"))
                body = (
                    "# 中文完整任务 🐍\n"
                    + "- 保留代码与 Markdown café\n" * 100
                    + "```python\nprint('你好')\n```"
                )
                expected = (
                    [f"第{i}轮 English，中文 🐍" for i in range(10)]
                    if case.startswith("rounds")
                    else [body]
                )
                if case == "approval":
                    expected = ["deny", "allow", "cancel", "deny"]
                for i, text in enumerate(expected):
                    if case == "approval":
                        until(lambda s: s.get("approval_running") and s.get("approval_index") == i)
                        if i == 1:
                            proc.write("\x1b[200~allow\t\r\x1b[201~")
                            pump(0.2)
                            assert len(status()["submitted"]) == 1
                            proc.write("\t")
                            pump(0.1)
                            proc.write("d")
                    elif case.startswith("rounds"):
                        until(lambda s: s.get("running") and len(s["submitted"]) == i)
                        proc.write(text)
                        until(lambda s: s.get("buffer") == text)
                    else:
                        proc.write(
                            "\x1b[200~" + text + "\x1b[201~"
                            if case == "bracketed-paste"
                            else text.replace("\n", "\r\n")
                        )
                    pump(0.5)
                    snapshots.append(list(screen.display))
                    if "console_rows" in status():
                        console_snapshots.append(status()["console_rows"])
                    proc.write("\x1b" if case == "approval" and i == 2 else "\r")
                    until(lambda s: len(s.get("submitted", [])) == i + 1)
                until(lambda s: s.get("done"))
                actual = status()["submitted"]
                report["rows"].append(
                    {
                        "case": case,
                        "pass": actual == expected and status().get("submitted_utf8_sha256") == [
                            hashlib.sha256(t.encode()).hexdigest() for t in expected
                        ],
                        "diagnostic": status().get("diagnostic"),
                        "frame_counts": [sum("┌" in line for line in shot) for shot in snapshots],
                        "layout_pass": all(
                            sum("┌" in line for line in shot) == 1
                            and any(f"第{i}轮 English，中文" in line and "Nexus" in line
                                    for line in shot)
                            for i, shot in enumerate(snapshots)
                        ) if case.startswith("rounds") else None,
                        "expected_sha256": [
                            hashlib.sha256(t.encode()).hexdigest() for t in expected
                        ],
                        "actual_sha256": status().get("submitted_utf8_sha256"),
                        "console_layout_pass": all(
                            sum("┌" in line for line in shot) == 1
                            and any(f"第{i}轮 English，中文" in line and "Nexus" in line
                                    for line in shot)
                            for i, shot in enumerate(console_snapshots)
                        ) if case.startswith("rounds") and console_snapshots else None,
                    }
                )
            except Exception as exc:
                report["rows"].append({"case": case, "pass": False, "error": str(exc)})
            finally:
                (args.output / (case + ".ansi")).write_bytes("".join(raw).encode("utf-8"))
                (args.output / (case + "-screens.json")).write_text(
                    json.dumps(snapshots, ensure_ascii=False), encoding="utf-8"
                )
                if console_snapshots:
                    (args.output / (case + "-console.json")).write_text(
                        json.dumps(console_snapshots, ensure_ascii=True), encoding="utf-8"
                    )
                if proc.isalive():
                    try:
                        proc.terminate(force=True)
                    except OSError as exc:
                        report["rows"][-1]["cleanup_error"] = type(exc).__name__
                        report["rows"][-1]["pid"] = proc.pid
                (args.output / "report.json").write_text(
                    json.dumps(report, indent=2), encoding="utf-8"
                )
    print(json.dumps(report))
    return all(row["pass"] and row.get("layout_pass") is not False
               for row in report["rows"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", required=True)
    parser.add_argument("--wsl", action="store_true")
    parser.add_argument("--backend", type=int, default=0)
    parser.add_argument("--case")
    parser.add_argument("--output", type=Path, required=True)
    sys.exit(0 if main(parser.parse_args()) else 1)
