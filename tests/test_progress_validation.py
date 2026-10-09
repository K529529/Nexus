from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path

import pytest

from nexus.core.agent import run_turn
from nexus.core.progress import ProgressLedger
from nexus.core.types import ExecutionContext, Limits, Message, Session, ToolResult
from nexus.tools.execution import execute
from nexus.tools.registry import default_tools
from nexus.tools.validation import direct_pytest_report, validation_evidence
from tests.conftest import Recorder, ScriptedModel, call, python_command, reply


def pytest_command(report: str) -> str:
    prefix = "& " if os.name == "nt" else ""
    return prefix + shlex.quote(sys.executable) + " -m pytest test_app.py -q --junitxml=" + report


def junit(failure: str = "", skipped: bool = False) -> str:
    return (
        f'<testsuite tests="1" failures="{int(bool(failure))}" errors="0" '
        f'skipped="{int(skipped)}"><testcase name="behavior">'
        + ("<failure>assertion failed</failure>" if failure else "")
        + ("<skipped/>" if skipped else "")
        + "</testcase></testsuite>"
    )


async def test_fresh_test_report_and_scope_are_required(execution: ExecutionContext) -> None:
    (execution.workspace / "app.py").write_text("x=1")
    ledger = ProgressLedger()
    ledger.observe(
        call("apply_patch", {}),
        ToolResult(
            "c1",
            True,
            {
                "files": [{"path": "app.py"}, {"path": "other.py"}],
                "execution_step": 1,
            },
        ),
    )
    (execution.workspace / "report.xml").write_text(junit())
    arguments = {
        "command": python_command("print('ok')"),
        "purpose": "validate",
        "validation_scope": ["app.py"],
        "validation_report": "report.xml",
    }
    stale = await execute(arguments, execution, Recorder())
    assert stale.ok and not stale.data["validation"]["behavioral_pass"]
    ledger.observe(call("exec_command", arguments), stale)
    assert ledger.pending == {"app.py", "other.py"}
    arguments["command"] = python_command(
        "from pathlib import Path; Path('report.xml').write_text(" + repr(junit()) + ")"
    )
    fabricated = await execute(arguments, execution, Recorder())
    assert not fabricated.data["validation"]["behavioral_pass"]
    (execution.workspace / "test_app.py").write_text("def test_app(): assert 1+1==2")
    arguments["command"] = pytest_command("report.xml")
    fresh = await execute(arguments, execution, Recorder())
    assert fresh.data["validation"]["tests"]["passed"] == 1
    ledger.observe(call("exec_command", arguments), fresh)
    assert ledger.pending == {"other.py"}


@pytest.mark.parametrize(
    "report",
    [
        junit("failed"),
        junit(skipped=True),
        "<testsuite/>",
        '<testsuite tests="2"><testcase/></testsuite>',
        "broken",
    ],
)
async def test_bad_or_empty_reports_do_not_clear_debt(
    execution: ExecutionContext,
    report: str,
) -> None:
    path = execution.workspace / "r.xml"
    path.write_text(report)
    evidence = validation_evidence(
        {
            "command": "pytest --junitxml=r.xml",
            "cwd": str(execution.workspace),
            "exit_code": 0,
            "timed_out": False,
            "cancelled": False,
            "cleanup_incomplete": False,
        },
        path.resolve(),
        None,
    )
    assert not evidence["behavioral_pass"]


async def test_shell_mutation_fingerprints_and_failed_command(execution: ExecutionContext) -> None:
    result = await execute(
        {
            "command": python_command(
                "from pathlib import Path; Path('a.py').write_text('x=1'); raise SystemExit(1)"
            ),
            "purpose": "mutate",
            "mutation_scope": ["a.py"],
        },
        execution,
        Recorder(),
    )
    assert not result.ok and result.data["observed_changed_files"] == ["a.py"]
    ledger = ProgressLedger()
    ledger.observe(call("exec_command", {}), result)
    assert ledger.pending == {"a.py"}
    assert len(ledger.errors) == 1


async def test_real_pytest_proves_execution_but_import_does_not(
    execution: ExecutionContext,
) -> None:
    (execution.workspace / "app.py").write_text("def add(a,b): return a+b")
    (execution.workspace / "test_app.py").write_text(
        "from app import add\ndef test_add(): assert add(1,2)==3\n"
    )
    partial = await execute(
        {
            "command": python_command("import app"),
            "purpose": "validate",
            "validation_scope": ["app.py"],
        },
        execution,
        Recorder(),
    )
    assert partial.ok and partial.data["validation"]["level"] == "partial"
    result = await execute(
        {
            "command": pytest_command("r.xml"),
            "purpose": "validate",
            "validation_scope": ["app.py"],
            "validation_report": "r.xml",
        },
        execution,
        Recorder(),
    )
    assert result.ok, result.data
    assert result.data["validation"]["behavioral_pass"]
    assert result.data["validation"]["tests"]["passed"] == 1


async def test_debt_reminder_is_request_only_and_does_not_gate_finish(tmp_path: Path) -> None:
    session = Session(tmp_path, messages=[Message("system", "rules")])
    model = ScriptedModel(
        [
            reply(
                call(
                    "apply_patch",
                    {"patch": "*** Begin Patch\n*** Add File: a.py\n+x=1\n*** End Patch"},
                )
            ),
            reply(text="Unverified change; checks not run."),
        ]
    )
    result = await run_turn(
        session, "implement", model, default_tools(session), Recorder(), Limits(max_steps=2)
    )
    assert result.outcome == "completed" and len(model.requests) == 2
    assert '"validation_debt":true' in model.requests[1][-1].content
    assert all("Progress and validation evidence" not in m.content for m in session.messages)
    restored = ProgressLedger.restore(session)
    assert restored.pending == {"a.py"} and restored.last_mutation_step == 1
    session.run_id = "new-run"
    assert not ProgressLedger.restore(session).pending


def test_partial_patch_and_unknown_scope_cannot_be_erased_by_unrelated_tests(
    tmp_path: Path,
) -> None:
    ledger = ProgressLedger()
    ledger.observe(
        call("apply_patch", {}),
        ToolResult(
            "c1",
            False,
            {
                "files": [{"path": "a.py"}],
                "omitted_files": 1,
                "partial": True,
            },
            "patch_io_error",
        ),
    )
    ledger.observe(
        call("exec_command", {}),
        ToolResult(
            "c2",
            True,
            {
                "purpose": "validate",
                "validation_scope": ["a.py"],
                "validation": {"behavioral_pass": True},
            },
        ),
    )
    assert not ledger.pending and ledger.data(Session(tmp_path), 3, 50)["validation_debt"]


async def test_scope_must_not_escape_and_unknown_purpose_rejected(
    execution: ExecutionContext,
) -> None:
    for args in ({"purpose": "test"}, {"mutation_scope": ["../outside"]}):
        result = await execute({"command": "echo unused", **args}, execution, Recorder())
        assert result.error_code == "invalid_arguments"


def test_ambiguous_commands_are_not_validation_evidence(tmp_path: Path) -> None:
    report = (tmp_path / "r.xml").resolve()
    for command in (
        "pytest --junitxml=r.xml | tail",
        "pytest --junitxml=r.xml; echo ok",
        "python -c 'print(1)'",
        "pytest --junitxml=other.xml",
        "pytest --junitxml=r.xml --junitxml=r.xml",
    ):
        assert not direct_pytest_report(command, report, str(tmp_path))
    assert direct_pytest_report("python -m pytest -q --junitxml=r.xml", report, str(tmp_path))


async def test_validation_mutation_does_not_clear_debt(execution: ExecutionContext) -> None:
    (execution.workspace / "app.py").write_text("x=1")
    (execution.workspace / "test_app.py").write_text(
        "from pathlib import Path\ndef test_mutate(): Path('app.py').write_text('x=2')\n"
    )
    result = await execute(
        {
            "command": pytest_command("r.xml"),
            "purpose": "validate",
            "validation_scope": ["app.py"],
            "validation_report": "r.xml",
        },
        execution,
        Recorder(),
    )
    assert result.ok and result.data["validation"]["behavioral_pass"]
    ledger = ProgressLedger()
    ledger.observe(call("exec_command", {}), result)
    assert ledger.pending == {"app.py"}
