from __future__ import annotations

import asyncio
import hashlib
import io
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from prompt_toolkit import PromptSession
from prompt_toolkit.application import create_app_session
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.output.vt100 import Vt100_Output

from nexus.app.events import Events
from nexus.app.input import TaskInput
from nexus.app.session import SessionLog
from nexus.app.tui import approval_text
from nexus.core.security import ApprovalDecision, ApprovalRequest, SecurityGateway, SecurityPolicy
from nexus.core.types import ExecutionContext, RuntimeEvent, Session, ToolCancelled
from nexus.tools.registry import native_tools
from tests.conftest import call
from tests.test_cli_ux import TASK, until
from tests.test_input_v02 import editor_session, paste
from tests.test_security_stage4 import Backend


@pytest.mark.parametrize("reworked", [False, True])
async def test_native_console_help_flows_with_editor_except_reworked(
    monkeypatch: pytest.MonkeyPatch, reworked: bool,
) -> None:
    class ConsoleOutput(DummyOutput):
        # The same capability exposed by Win32Output / Windows10_Output.
        def __init__(self) -> None:
            self.positions: list[tuple[int, int]] = []
            self.width = 80

        def get_win32_screen_buffer_info(self) -> SimpleNamespace:
            assert not reworked
            return SimpleNamespace(dwSize=SimpleNamespace(X=self.width),
                                   dwCursorPosition=SimpleNamespace(X=25, Y=9))

        def cursor_goto(self, row: int = 0, column: int = 0) -> None:
            self.positions.append((row, column))

        def get_rows_below_cursor_position(self) -> int:
            return 40

    monkeypatch.setenv("INTELLIJ_TERMINAL_COMMAND_BLOCKS_REWORKED", "1" if reworked else "0")
    output = ConsoleOutput()
    async with editor_session(output) as (editor, prompt, pipe, pending):
        assert editor.inline_toolbar is not reworked
        if not reworked:
            assert prompt.bottom_toolbar is None
            screen = prompt.app.renderer.last_rendered_screen
            assert screen is not None
            rows = ["".join(c.char for _, c in sorted(screen.data_buffer[y].items()))
                    for y in range(screen.height)]
            assert "Nexus" in rows[1]
            assert "Enter send" in rows[3]
            assert all(not row.strip() for row in rows[4:])
            assert output.positions[-1] == (9, prompt.app.renderer._cursor_pos.x)
        pipe.send_text("中文\r")
        assert await pending == "中文"


@pytest.mark.parametrize("ending", ["\n", "\r", "\r\n"])
@pytest.mark.parametrize("chunks", [False, True])
async def test_unmarked_stream_paste_preserves_entire_draft(ending: str, chunks: bool) -> None:
    async with editor_session() as (editor, prompt, pipe, pending):
        raw = TASK.replace("\n", ending)
        if chunks:
            for chunk in (raw[:40], raw[40:100], raw[100:]):
                pipe.send_text(chunk)
                await asyncio.sleep(0.02)
        else:
            pipe.send_text(raw)
        await until(lambda: editor.expand(prompt.default_buffer.text) == TASK)
        await asyncio.sleep(0.2)
        assert not pending.done()
        pipe.send_text("\r")
        result = await pending
        assert hashlib.sha256(result.encode()).digest() == hashlib.sha256(TASK.encode()).digest()


async def test_stream_paste_then_ordinary_typing_and_newline() -> None:
    async with editor_session() as (_, prompt, pipe, pending):
        pipe.send_text("first\rsecond")
        await until(lambda: prompt.default_buffer.text == "first\nsecond")
        await asyncio.sleep(0.2)
        pipe.send_text(" edit\x0atail")
        await until(lambda: prompt.default_buffer.text == "first\nsecond edit\ntail")
        pipe.send_text("\r")
        assert await pending == "first\nsecond edit\ntail"


async def test_split_crlf_stream_boundary_is_one_newline() -> None:
    async with editor_session() as (_, prompt, pipe, pending):
        pipe.send_text("first\r")
        await asyncio.sleep(0.02)
        pipe.send_text("\nsecond")
        await until(lambda: prompt.default_buffer.text == "first\nsecond")
        await asyncio.sleep(0.2)
        assert not pending.done()
        pipe.send_text("\r")
        assert await pending == "first\nsecond"


async def test_console_utf16_pairs_are_rendered_and_submitted_as_unicode() -> None:
    from prompt_toolkit.key_binding import KeyPress

    async with editor_session() as (editor, prompt, pipe, pending):
        # A real Win32 VT reader yields these two UTF-16 code units separately.
        processor = prompt.app.key_processor
        processor.feed_multiple([KeyPress("\ud83d", "\ud83d"), KeyPress("\udc0d", "\udc0d")])
        # Let the real application's input callback process the queued key events.
        pipe.send_text(" ")
        await until(lambda: prompt.default_buffer.text == "🐍 ")
        editor.paste("\ud83d\udc0d\n")
        pipe.send_text("\r")
        assert await pending == "🐍 🐍\n"


async def test_vt_toolbar_stays_below_editor_after_cpr_and_resize() -> None:
    captured = io.StringIO()
    sizes = [Size(rows=30, columns=90)]
    output = Vt100_Output(captured, lambda: sizes[0])
    async with editor_session(output) as (_, prompt, pipe, pending):
        for size in (Size(rows=30, columns=90), Size(rows=42, columns=60)):
            sizes[0] = size
            prompt.app.renderer.report_absolute_cursor_row(1)
            prompt.app.invalidate()
            await asyncio.sleep(0.08)
            screen = prompt.app.renderer.last_rendered_screen
            assert screen is not None
            rows = ["".join(c.char for _, c in sorted(screen.data_buffer[y].items()))
                    for y in range(screen.height)]
            assert "Nexus" in rows[1]
            assert "Enter send" in rows[3]
            assert all(not row.strip() for row in rows[4:])
        pipe.send_text("中文\r")
        assert await pending == "中文"


@pytest.mark.parametrize(
    "keys,expected",
    [
        ([], "deny"),
        (["\t"], "allow"),
        (["\x1b[Z"], "cancel"),
        (["\x1b[B"], "allow"),
        (["\x1b[A"], "cancel"),
        (["\t", "\t", "\t"], "deny"),
        (["\x1b"], "cancel"),
        (["\x03"], "cancel"),
    ],
)
async def test_visible_approval_navigation_and_focus(keys: list[str], expected: str) -> None:
    captured = io.StringIO()
    output = Vt100_Output(captured, lambda: Size(rows=30, columns=90))
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=output):
        editor = TaskInput(PromptSession(output=output))
        with editor.busy(lambda: pytest.fail("Busy must not own approval stdin")):
            decision = asyncio.create_task(
                editor.approve("Operation: apply_patch", "完整 Patch\n+x\n")
            )
            await until(lambda: editor.approval is not None and editor.approval.is_running)
            assert "Deny" in captured.getvalue() and "Allow once" in captured.getvalue()
            for key in keys:
                pipe.send_text(key)
                await asyncio.sleep(0.08)
            if not decision.done():
                pipe.send_text("\r")
            assert await asyncio.wait_for(decision, 3) == expected
        pending = asyncio.create_task(editor.read())
        await until(lambda: editor.prompt.app.is_running)
        pipe.send_text("恢复中文焦点\r")
        assert await pending == "恢复中文焦点"


async def test_approval_drops_typeahead_paste_and_does_not_reuse_selection() -> None:
    captured = io.StringIO()
    output = Vt100_Output(captured, lambda: Size(rows=30, columns=90))
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=output):
        editor = TaskInput(PromptSession(output=output))
        with editor.busy(lambda: None):
            pipe.send_text("\t\r")  # typed before the request: must be drained
            pending = asyncio.create_task(editor.approve("Review", "details"))
            await until(lambda: editor.approval is not None and editor.approval.is_running)
            pipe.send_text(paste("\tallow\r") + "\r")
            await asyncio.sleep(0.1)
            assert not pending.done()
            pipe.send_text("\t\r")  # invisible burst cannot confirm unseen selection
            await asyncio.sleep(0.1)
            assert not pending.done()
            pipe.send_text("d")
            await until(lambda: "details" in captured.getvalue())
            pipe.send_text("\r")
            assert await pending == "allow"
            pending = asyncio.create_task(editor.approve("Second request"))
            await until(lambda: editor.approval is not None and editor.approval.is_running)
            pipe.send_text("\r")
            assert await pending == "deny"


async def test_approval_timeout_restores_busy_reader() -> None:
    async with editor_session() as (editor, prompt, pipe, first):
        pipe.send_text("task\r")
        await first
        interrupted = []
        with editor.busy(lambda: interrupted.append(True)):
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(editor.approve("Timeout request"), 0.08)
            assert editor.approval is None
            pipe.send_text("ignored\r\x03")
            await until(lambda: bool(interrupted))
        pending = asyncio.create_task(editor.read())
        await until(lambda: prompt.app.is_running)
        assert prompt.default_buffer.text == ""
        pipe.send_text("fresh\r")
        assert await pending == "fresh"


def test_approval_summary_uses_host_operation_scope_and_redacts_before_truncation() -> None:
    request = ApprovalRequest(
        "r",
        "s",
        "run",
        "permission-call",
        "request_permissions",
        "DIGEST",
        json.dumps(
            {
                "tool": "apply_patch",
                "mode": "read-only",
                "workspace": "/workspace",
                "grant": "one apply_patch only; no shell; baseline restored afterwards",
            }
        ),
        time.monotonic() + 120,
        json.dumps(
            {
                "request": {"reason": "I can change the whole session SECRET"},
                "operation": {
                    "patch": "*** Begin Patch\n*** Add File: 中文.py\n+SECRET\n*** End Patch"
                },
            }
        ),
    )
    summary, detail = approval_text(request, ("SECRET",))
    assert "Operation: apply_patch" in summary
    assert "Target:    中文.py" in summary
    assert "Scope:     one apply_patch only; no shell" in summary
    assert "Reason (agent):" in summary
    assert "SECRET" not in summary + detail
    assert "*** Add File: 中文.py\n+" in detail
    assert "Tool call: permission-call" in detail and "DIGEST" in detail


@pytest.mark.parametrize("choice", ["allow", "deny", "cancel", "timeout", "mutated"])
async def test_visible_approval_keeps_gateway_checks_and_jsonl(tmp_path: Path, choice: str) -> None:
    from prompt_toolkit.output import DummyOutput

    session = Session(tmp_path, run_id="run")
    writer = SessionLog.create(session, "ui", {}, tmp_path / "audit")

    async def consume(event: RuntimeEvent) -> None:
        pass

    events = Events(session, writer, consume)
    backend = Backend()
    target = call("exec_command", {"command": "test"})
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        editor = TaskInput(PromptSession(output=DummyOutput()))

        async def approve(request: ApprovalRequest) -> ApprovalDecision:
            summary, details = approval_text(request, ())
            value = await editor.approve(summary, details)
            return ApprovalDecision(
                request.request_id,
                "allow_once" if value == "allow" else "cancel" if value == "cancel" else "deny",
            )

        gateway = SecurityGateway(
            SecurityPolicy("workspace-write", ask_tools=("exec_command",)),
            backend=backend,
            approver=approve,
            approval_timeout=0.2 if choice == "timeout" else 3,
        )
        try:
            with editor.busy(lambda: None):
                pending = asyncio.create_task(
                    gateway.execute(
                        target,
                        native_tools(),
                        session,
                        ExecutionContext(tmp_path, "c1", ""),
                        events,
                    )
                )
                await until(lambda: editor.approval is not None and editor.approval.is_running)
                if choice in {"allow", "mutated"}:
                    pipe.send_text("\t")
                    await asyncio.sleep(0.06)
                    if choice == "mutated":
                        target.arguments_json = '{"command":"changed"}'
                    pipe.send_text("\r")
                elif choice == "deny":
                    pipe.send_text("\r")
                elif choice == "cancel":
                    pipe.send_text("\x03")
                if choice == "cancel":
                    with pytest.raises(ToolCancelled):
                        await pending
                else:
                    result = await pending
                    assert result.ok == (choice == "allow")
                    if choice == "mutated":
                        assert result.error_code == "approval_invalid"
            assert backend.calls == (["exec_command"] if choice == "allow" else [])
        finally:
            writer.close()
    rows = [json.loads(line) for line in writer.path.read_text(encoding="utf-8").splitlines()]
    kinds = [row["kind"] for row in rows]
    assert kinds.count("approval_requested") == kinds.count("approval_resolved") == 1
    if choice == "allow":
        assert kinds.index("approval_resolved") < kinds.index("tool_execution")
