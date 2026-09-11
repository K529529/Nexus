from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from uuid import UUID

import pytest
from langsmith import Client

from nexus.config.loader import load_runtime_config
from nexus.domain.planning import PlanApprovalResumeInput, TerminalStatus
from nexus.domain.runtime_events import FinalResult, RunInterrupted, TaskStarted
from nexus.domain.tooling import ApprovalDecision
from nexus.infrastructure.bootstrap import bootstrap_application

pytestmark = [pytest.mark.langsmith_e2e, pytest.mark.postgres]

_SENTINEL = "NEXUS_DAY8_REMOTE_REDACTION_SENTINEL_7A91"


@pytest.mark.asyncio
async def test_real_langsmith_nontrivial_coding_trace_is_safe(
    tmp_path: Path,
) -> None:
    if os.environ.get("NEXUS_RUN_LANGSMITH_E2E") != "1":
        pytest.skip("Requires explicit NEXUS_RUN_LANGSMITH_E2E=1 export authorization.")
    required = {
        "NEXUS_MODEL_NAME",
        "NEXUS_MODEL_API_KEY",
        "NEXUS_DATABASE_URL",
        "NEXUS_LANGSMITH_API_KEY",
    }
    if missing := sorted(name for name in required if not os.environ.get(name)):
        pytest.skip(f"Missing explicit live acceptance configuration: {', '.join(missing)}")
    git = shutil.which("git")
    if git is None:
        pytest.skip("Git is required for the disposable live fixture.")

    (tmp_path / "alpha.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "test_alpha.py").write_text(
        "from alpha import VALUE\n\ndef test_value():\n    assert VALUE == 2\n",
        encoding="utf-8",
    )
    _git(git, tmp_path, "init")
    _git(git, tmp_path, "config", "core.autocrlf", "false")
    _git(git, tmp_path, "add", "alpha.py", "test_alpha.py")
    _git(
        git,
        tmp_path,
        "-c",
        "user.name=Nexus Test",
        "-c",
        "user.email=nexus@example.invalid",
        "commit",
        "-m",
        "fixture",
    )

    environment = dict(os.environ)
    environment["NEXUS_LANGSMITH_TRACING_ENABLED"] = "true"
    config = load_runtime_config(
        repo_root=tmp_path,
        user_config_path=tmp_path / "absent-user-config.toml",
        environ=environment,
    )
    task = (
        "In this disposable fixture, change alpha.py VALUE from 1 to 2 and run "
        f"pytest -q test_alpha.py. Never copy this private marker: {_SENTINEL}"
    )
    async with bootstrap_application(config, workspace_path=tmp_path) as application:
        first = [event async for event in application.runtime.run(task)]
        interrupted = first[-1]
        assert isinstance(interrupted, RunInterrupted)
        assert interrupted.session_id is not None
        resumed = [
            event
            async for event in application.runtime.resume(
                interrupted.session_id,
                resume_input=PlanApprovalResumeInput(ApprovalDecision.APPROVED, None),
            )
        ]

    started = next(event for event in first if isinstance(event, TaskStarted))
    final = resumed[-1]
    assert isinstance(final, FinalResult)
    assert final.terminal_status is TerminalStatus.SUCCEEDED

    client = Client(
        api_url=config.langsmith_endpoint,
        api_key=config.langsmith_api_key.get_secret_value()
        if config.langsmith_api_key is not None
        else None,
        workspace_id=config.langsmith_workspace_id,
        hide_inputs=True,
        auto_batch_tracing=False,
    )
    roots = list(
        client.list_runs(
            project_name=config.langsmith_project,
            is_root=True,
            limit=20,
        )
    )
    root = next(
        run
        for run in roots
        if (run.extra or {}).get("metadata", {}).get("nexus.run_id") == started.run_id
    )
    retrieved = client.read_run(UUID(str(root.id)), load_child_runs=True)
    remote = repr(retrieved)
    assert _SENTINEL not in remote
    assert "VALUE = 2" not in remote
    assert "test_alpha.py" not in remote
    assert {child.run_type for child in retrieved.child_runs or []} >= {
        "llm",
        "tool",
        "chain",
    }
    client.close(timeout=2.0)


def _git(executable: str, cwd: Path, *arguments: str) -> None:
    completed = subprocess.run(
        [executable, *arguments],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise AssertionError("Could not prepare disposable Git fixture.")
