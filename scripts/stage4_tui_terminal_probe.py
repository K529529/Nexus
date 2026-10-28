"""No-model real POSIX PTY acceptance. pyte is a test-only terminal emulator."""
# ruff: noqa: B023 -- callbacks are used synchronously within each joined probe case

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import os
import sys
import time
from pathlib import Path

BODY = (
    "# 修复任务 Unicode 🐍 café\n\n"
    + "- 中文和 English，保留完整内容。\n" * 100
    + "```python\nprint('你好')\n```\n"
)


async def child(state: Path, case: str) -> None:
    if case.endswith("no-cpr"):
        os.environ["PROMPT_TOOLKIT_NO_CPR"] = "1"
    from prompt_toolkit import PromptSession
    from prompt_toolkit.application.current import get_app_session
    from rich.console import Console

    from nexus.app.input import TaskInput

    prompt = PromptSession(output=get_app_session().output)
    if "preservemode" in case:
        import ctypes
        from ctypes.wintypes import DWORD

        output = prompt.app.output

        def flush_preserving_mode():
            original = DWORD()
            ctypes.windll.kernel32.GetConsoleMode(output._hconsole, ctypes.byref(original))
            ctypes.windll.kernel32.SetConsoleMode(output._hconsole, DWORD(original.value | 4))
            try:
                output.vt100_output.flush()
            finally:
                ctypes.windll.kernel32.SetConsoleMode(output._hconsole, original)

        output.flush = flush_preserving_mode
    editor = TaskInput(prompt)
    if "align" in case:
        def align_cursor(app):
            if not app.is_done:
                info = app.output.get_win32_screen_buffer_info()
                app.output.win32_output.cursor_goto(info.dwCursorPosition.Y,
                                                   app.renderer._cursor_pos.x)
        prompt.app.after_render += align_cursor
    if "reflow" in case:
        from prompt_toolkit.data_structures import Point
        original_resize = prompt.app._on_resize

        def resize_reflow():
            renderer = prompt.app.renderer
            screen = renderer.last_rendered_screen
            cursor = renderer._cursor_pos
            width = prompt.app.output.get_win32_screen_buffer_info().dwSize.X
            if screen is not None:
                extra = sum(max(0, max((x + 1 for x, c in screen.data_buffer[y].items()
                                       if c.char.strip()), default=0) - 1) // width
                            for y in range(cursor.y))
                renderer._cursor_pos = Point(cursor.x % width,
                                             cursor.y + extra + cursor.x // width)
            original_resize()

        prompt.app._on_resize = resize_reflow
    reservations = []
    if "trace" in case:
        def trace_reservation(app):
            size = app.output.get_size()
            reservations.append({"done": app.is_done,
                                 "below": app.output.get_rows_below_cursor_position(),
                                 "preferred": app.layout.container.preferred_height(
                                     size.columns, size.rows).preferred})
        prompt.app.before_render += trace_reservation
    if "reserve" in case:
        original_prepare_reserve = editor.prepare

        def reserve_prepare():
            original_prepare_reserve()
            output = prompt.app.output
            output.write("\r\n" * 4)
            output.cursor_up(4)
            output.flush()

        editor.prepare = reserve_prepare
    if case.endswith("inline"):
        from prompt_toolkit.layout.containers import HSplit, Window
        from prompt_toolkit.layout.controls import FormattedTextControl
        from prompt_toolkit.layout.dimension import Dimension

        root = prompt.layout.container
        prompt.layout.container = HSplit(
            [
                HSplit(
                    [root],
                    height=lambda: Dimension.exact(
                        root.preferred_height(
                            prompt.app.output.get_size().columns, prompt.app.output.get_size().rows
                        ).preferred
                    ),
                ),
                Window(FormattedTextControl(editor.toolbar), height=1),
            ]
        )
        original_prompt = prompt.prompt_async

        async def inline_prompt(*args, **kwargs):
            kwargs["bottom_toolbar"] = None
            return await original_prompt(*args, **kwargs)

        prompt.prompt_async = inline_prompt
        original_prepare = editor.prepare

        def inline_prepare():
            original_prepare()
            prompt.bottom_toolbar = None

        editor.prepare = inline_prepare
    submitted = []
    cpr_rows = []
    original_report = prompt.app.renderer.report_absolute_cursor_row

    def report_row(row):
        cpr_rows.append(row)
        original_report(row)

    prompt.app.renderer.report_absolute_cursor_row = report_row

    def save(**extra):
        def digest(text):
            try:
                return hashlib.sha256(text.encode("utf-8")).hexdigest()
            except UnicodeError:
                return None

        data = {
            "submitted": submitted,
            "submitted_utf8_sha256": [digest(text) for text in submitted],
            "running": prompt.app.is_running,
            "buffer": editor.expand(prompt.default_buffer.text),
            "diagnostic": {
                "input": type(prompt.app.input).__name__,
                "output": type(prompt.app.output).__name__,
                "cpr_rows": cpr_rows,
                "size": list(prompt.app.output.get_size()),
                "cursor": list(prompt.app.renderer._cursor_pos),
                "term": os.environ.get("TERM"),
                "inline_toolbar": getattr(editor, "inline_toolbar", None),
                "toolbar_empty": prompt.bottom_toolbar == "",
                "reservations": reservations[-3:],
            },
            **extra,
        }
        if sys.platform == "win32":
            # Read the real console cells as well as the outer WinPTY transcript.
            # This separates bridge capture errors from application rendering.
            import ctypes
            from ctypes.wintypes import DWORD

            from prompt_toolkit.win32_types import COORD

            output = prompt.app.output
            info = output.get_win32_screen_buffer_info()
            data["diagnostic"]["console_cursor"] = [
                info.dwCursorPosition.X, info.dwCursorPosition.Y,
            ]
            data["diagnostic"]["console_window"] = [info.srWindow.Top, info.srWindow.Bottom]
            rows = []
            for y in range(info.srWindow.Top, info.srWindow.Bottom + 1):
                cells = ctypes.create_unicode_buffer(info.dwSize.X + 1)
                read = DWORD()
                ok = ctypes.windll.kernel32.ReadConsoleOutputCharacterW(
                    output.win32_output.hconsole, cells, DWORD(info.dwSize.X),
                    COORD(0, y), ctypes.byref(read),
                )
                if not ok:
                    raise ctypes.WinError()
                rows.append(cells[:read.value])
            data["console_rows"] = rows
        with state.open("a", encoding="utf-8") as file:
            file.write(json.dumps(data, ensure_ascii=True) + "\n")

    prompt.app.after_render += lambda app: save()
    if case == "approval":
        from nexus.app.tui import approval_text
        from nexus.core.security import ApprovalRequest

        request = ApprovalRequest(
            "fixture",
            "session",
            "run",
            "call",
            "request_permissions",
            "fixture-digest",
            json.dumps(
                {"tool": "apply_patch", "mode": "read-only", "grant": "This patch only; no shell"}
            ),
            time.monotonic() + 120,
            json.dumps(
                {
                    "request": {"reason": "修改测试文件"},
                    "operation": {
                        "patch": "*** Begin Patch\n*** Add File: example.py\n"
                                 "+print('你好')\n*** End Patch"
                    },
                }
            ),
        )
        with editor.busy(lambda: None):
            for i in range(4):
                decision = asyncio.create_task(editor.approve(*approval_text(request, ())))
                while editor.approval is None or not editor.approval.is_running:  # noqa: ASYNC110
                    await asyncio.sleep(0.01)
                save(approval_index=i, approval_running=True)
                submitted.append(await decision)
        save(done=True)
        return
    rounds = 10 if case.startswith("rounds") else 1
    for i in range(rounds):
        text = await editor.read()
        submitted.append(text)
        save()
        with editor.busy(lambda: None):
            Console(highlight=False).print(f"Tool output {i}: 中文完成")
            if "history" in case:
                Console(highlight=False).print("\n".join(
                    f"Transcript {i}.{line}: 多轮历史输出 English" for line in range(12)
                ))
            await asyncio.sleep(0.03)
    save(done=True)


def parent(args):
    import codecs
    import fcntl
    import pty
    import select
    import signal
    import struct
    import subprocess
    import tempfile
    import termios

    import pyte

    args.output.mkdir(parents=True, exist_ok=True)
    report = {
        "python": sys.executable,
        "TERM": "xterm-256color",
        "prompt_toolkit": importlib.metadata.version("prompt_toolkit"),
        "rich": importlib.metadata.version("rich"),
        "rows": [],
    }
    for case in ("raw-paste", "bracketed-paste", "rounds", "f2-expand-resize"):
        with tempfile.TemporaryDirectory(prefix="nexus-tui-pty-") as directory:
            state = Path(directory) / "state.json"
            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 90, 0, 0))
            env = {**os.environ, "TERM": "xterm-256color"}
            proc = subprocess.Popen(
                [sys.executable, "-I", __file__, "--child", str(state), "--case", case],
                stdin=slave,
                stdout=slave,
                stderr=slave,
                env=env,
                start_new_session=True,
            )
            os.close(slave)
            raw = bytearray()

            class Screen(pyte.Screen):
                def write_process_input(self, data):
                    if args.cpr:
                        os.write(master, data.encode())

            screen = Screen(90, 30)
            stream = pyte.Stream(screen)
            decoder = codecs.getincrementaldecoder("utf-8")("replace")

            def pump(duration=0.05):
                end = time.monotonic() + duration
                while time.monotonic() < end:
                    if select.select([master], [], [], 0.01)[0]:
                        try:
                            data = os.read(master, 65536)
                        except OSError:
                            return
                        raw.extend(data)
                        stream.feed(decoder.decode(data))

            def status():
                if not state.exists():
                    return {}
                lines = state.read_text(encoding="utf-8").splitlines(keepends=True)
                return next((json.loads(s) for s in reversed(lines) if s.endswith("\n")), {})

            def until(predicate, seconds=8):
                end = time.monotonic() + seconds
                while time.monotonic() < end:
                    pump()
                    if predicate(status()):
                        return
                raise TimeoutError(case)

            def send(text):
                os.write(master, text.encode())

            snapshots = []
            try:
                until(lambda s: s.get("running"))
                if case == "rounds":
                    expected = [f"第{i}轮 English，中文 🐍" for i in range(10)]
                    for i, body in enumerate(expected):
                        until(lambda s: s.get("running") and len(s["submitted"]) == i)
                        send(body)
                        until(lambda s: s.get("buffer") == body)
                        pump(0.1)
                        snapshots.append(list(screen.display))
                        send("\r")
                        until(lambda s: len(s.get("submitted", [])) == i + 1)
                else:
                    expected = [BODY]
                    if case == "raw-paste":
                        send(BODY.replace("\n", "\r\n"))
                    elif case == "bracketed-paste":
                        send("\x1b[200~" + BODY + "\x1b[201~")
                    else:
                        send("\x1bOQ" + BODY.replace("\n", "\r\n") + "\x1bOQ")
                        pump(0.2)
                        send("\x1bOR")
                        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 35, 55, 0, 0))
                        screen.resize(lines=35, columns=55)
                        os.kill(proc.pid, signal.SIGWINCH)
                    pump(0.5)
                    snapshots.append(list(screen.display))
                    before_enter = status()
                    send("\r")
                until(lambda s: s.get("done"))
                pump(0.1)
                actual = status()["submitted"]
                row = {
                    "case": case,
                    "pass": actual == expected,
                    "expected_sha256": [hashlib.sha256(t.encode()).hexdigest() for t in expected],
                    "actual_sha256": [hashlib.sha256(t.encode()).hexdigest() for t in actual],
                    "frame_counts": [sum("┌" in line for line in shot) for shot in snapshots],
                }
                if case != "rounds":
                    row["submitted_before_enter"] = len(before_enter.get("submitted", []))
                    row["pass"] &= not before_enter.get("submitted")
                report["rows"].append(row)
            except Exception as exc:
                report["rows"].append({"case": case, "pass": False, "error": str(exc)})
            finally:
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=5)
                os.close(master)
                (args.output / (case + ".ansi")).write_bytes(raw)
                (args.output / (case + "-screens.json")).write_text(
                    json.dumps(snapshots, ensure_ascii=False)
                )
                (args.output / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report))
    return all(row["pass"] for row in report["rows"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--child", type=Path)
    parser.add_argument("--case")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--cpr", action="store_true")
    options = parser.parse_args()
    if options.child:
        asyncio.run(child(options.child, options.case))
    else:
        sys.exit(0 if parent(options) else 1)
