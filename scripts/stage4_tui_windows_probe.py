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

            def native_screen():
                if args.wsl:
                    # An independent Windows reader sees the outer Console,
                    # avoiding pyte's lossy resize reconstruction for WSL.
                    return json.loads(subprocess.check_output(
                        [sys.executable, str(script.with_name("stage4_console_snapshot.py")),
                         str(proc.pid)], text=True, encoding="utf-8",
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    ))
                return status().get("console_rows", list(screen.display))

            snapshots = []
            console_snapshots = []
            resize_snapshots = []
            resize_checks = []
            rendered_states = []
            try:
                until(lambda s: s.get("running") or s.get("approval_running"))
                body = (
                    "# 中文完整任务 🐍\n"
                    + "- 保留代码与 Markdown café\n" * 100
                    + "```python\nprint('你好')\n```"
                )
                if case.startswith("manual-paste"):
                    body = (
                        "这是 Nexus Stage 4D 多行输入完整性测试。\n"
                        "第一项：Windows 和 Linux。\n"
                        "第二项：中文、English、Python 🐱。\n"
                        "第三项：Agent Runtime + Sandbox + HITL。\n"
                        "请不要调用工具，也不要修改文件。\n"
                        "请确认你收到了以上全部内容，并逐项复述。"
                    )
                expected = (
                    [f"第{i}轮 English，中文 🐍" for i in range(10)]
                    if case.startswith("rounds")
                    else [body]
                )
                if case.startswith("approval"):
                    expected = ["deny", "allow", "cancel", "deny"]
                if "multiline" in case:
                    expected = [text + "\n" + "\n".join(
                        f"line {line} 中文内容" for line in range(6)
                    ) for text in expected]
                for i, text in enumerate(expected):
                    if case.startswith("approval"):
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
                        if case == "rounds-resize":
                            # Grow the viewport vertically while editing, as in
                            # the user's top-edge window resize reproduction.
                            height = 30 + i * 2
                            proc.setwinsize(height, 90)
                            screen.resize(lines=height, columns=90)
                            pump(0.2)
                        empty_resize = "empty" in case
                        if not empty_resize:
                            proc.write(text.replace("\n", "\x1b[14~"))
                            until(lambda s: s.get("buffer") == text)
                        if (case.startswith("rounds-drag") or "history" in case) and i in (3, 6):
                            # Resize only AFTER history exists, with an active draft.
                            sizes = ((32, 90), (38, 90), (48, 90), (40, 90), (30, 90))
                            if case.startswith("rounds-drag"):
                                sizes += ((30, 70), (35, 110), (30, 90))
                            if "stress" in case:
                                sizes = tuple((max(10, 30 - n // 2), w) for n, w in enumerate(
                                    list(range(90, 39, -3)) + list(range(40, 131, 3))
                                )) + ((12, 40), (48, 130), (10, 60), (30, 90))
                            for resize_index, (height, width) in enumerate(sizes):
                                proc.setwinsize(height, width)
                                screen.resize(lines=height, columns=width)
                                pump(0.04 if "stress" in case else 0.7)
                                if "stress" in case and resize_index < len(sizes) - 4:
                                    continue
                                if "stress" in case:
                                    pump(1.2)
                                shot = list(screen.display)
                                native = native_screen()
                                resize_snapshots.append({"round": i, "size": [height, width],
                                                         "screen": shot, "console": native})
                                resize_checks.append(
                                    status().get("buffer") == ("" if empty_resize else text)
                                    and sum("┌" in line for line in native) == 1
                                    and any(("line 5 中文内容" in line
                                             if "multiline" in case and height < 12 else
                                             "Nexus" in line and (empty_resize or
                                             f"第{i}轮 English，中文" in line)) for line in native)
                                    and (height < 20 or any(
                                        f"Transcript {i - 1}.11:" in line for line in native))
                                )
                        if empty_resize:
                            proc.write(text.replace("\n", "\x1b[14~"))
                            until(lambda s: s.get("buffer") == text)
                        if "clear" in case and i in (3, 6):
                            proc.setwinsize(28, 85)
                            screen.resize(lines=28, columns=85)
                            proc.write("\x03")
                            until(lambda s: s.get("buffer") == "")
                            proc.write(text.replace("\n", "\x1b[14~"))
                            until(lambda s: s.get("buffer") == text)
                            pump(1.2)
                    else:
                        proc.write(
                            "\x1b[200~" + text + "\x1b[201~"
                            if case.startswith("bracketed-paste")
                            else text.replace("\n", "\r\n")
                        )
                    pump(0.5)
                    snapshots.append(list(screen.display))
                    rendered_states.append(status().get("diagnostic"))
                    console_snapshots.append(native_screen())
                    proc.write("\x1b" if case.startswith("approval") and i == 2 else "\r")
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
                        "rendered_states": rendered_states,
                        "frame_counts": [sum("┌" in line for line in shot) for shot in snapshots],
                        "layout_pass": all(
                            sum("┌" in line for line in shot) == 1
                            and any(f"第{i}轮 English，中文" in line and "Nexus" in line
                                    for line in shot)
                            for i, shot in enumerate(console_snapshots or snapshots)
                        ) if case.startswith("rounds") else None,
                        "capture_layout_pass": all(
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
                        "resize_layout_pass": all(resize_checks) if resize_checks else None,
                        "resize_checks": resize_checks,
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
                if resize_snapshots:
                    (args.output / (case + "-resize.json")).write_text(
                        json.dumps(resize_snapshots, ensure_ascii=True), encoding="utf-8"
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
               and row.get("console_layout_pass") is not False
               and row.get("resize_layout_pass") is not False
               for row in report["rows"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", required=True)
    parser.add_argument("--wsl", action="store_true")
    parser.add_argument("--backend", type=int, default=0)
    parser.add_argument("--case")
    parser.add_argument("--output", type=Path, required=True)
    sys.exit(0 if main(parser.parse_args()) else 1)
