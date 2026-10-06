from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from nexus.app import bootstrap, cli
from nexus.app.profile import ModelCall, RunProfile, ToolCall, profile_metrics
from nexus.core.context import instructions
from nexus.core.types import (
    Emit,
    ExecutionContext,
    Json,
    Message,
    ModelReply,
    Tool,
    ToolResult,
    ToolSpec,
)
from nexus.evaluation import runner
from nexus.evaluation.cases import CASE_IDS, load_case, validator_spec
from nexus.evaluation.environment import CleanupError, Docker, Environment
from nexus.evaluation.report import public_url, render_html, save_report
from nexus.evaluation.validation import grade, hidden_paths
from nexus.tools.execution import execute as native_execute
from nexus.tools.registry import native_tools
from tests.conftest import Recorder, ScriptedModel, call, reply
from tests.test_conversation import config, ignore


def test_fixed_cases_have_concrete_images_and_product_free_hidden_tests() -> None:
    assert len(CASE_IDS) == 8
    for cid in CASE_IDS:
        case = load_case(cid)
        spec = validator_spec(case)
        assert case.task.read_text(encoding="utf-8").strip()
        assert spec["targets"]["fail_to_pass"] and spec["targets"]["pass_to_pass"]
        assert hidden_paths(
            (case.validator.parent / spec["test_patch"]).read_text(encoding="utf-8")
        )
    with pytest.raises(ValueError, match="Unknown"):
        load_case("random")


@pytest.mark.parametrize(
    "observed,code,expected",
    [
        ({"fix": "passed", "retained": "passed"}, 0, "passed"),
        ({"fix": "failed", "retained": "passed"}, 1, "failed"),
        ({"fix": "passed"}, 0, "error"),
        ({"fix": "skipped", "retained": "passed"}, 0, "error"),
        ({"fix": "error", "retained": "passed"}, 1, "error"),
        ({"fix": "passed", "retained": "passed"}, 2, "error"),
        ({"fix": "passed", "retained": "passed"}, 1, "failed"),
    ],
)
def test_fixed_grade_requires_actual_targets(observed: Json, code: int, expected: str) -> None:
    assert (
        grade({"fail_to_pass": ["fix"], "pass_to_pass": ["retained"]}, observed, code)[0]
        == expected
    )


def test_profile_export_uses_existing_unknown_semantics() -> None:
    profile = RunProfile(
        run_id="run",
        models=[
            ModelCall(
                1, False, 1, finished=True, input_tokens=100, output_tokens=10, total_tokens=110
            ),
            ModelCall(2, False, 1, finished=True),
            ModelCall(3, False, 2, finished=True, input_tokens=300),
            ModelCall(
                None, True, 1, finished=True, input_tokens=20, output_tokens=3, total_tokens=23
            ),
        ],
        tools={"t": ToolCall(1, "exec_command", started=True, result_bytes=None)},
    )
    data = profile_metrics(profile)
    assert data["peak_context"] is None
    assert data["final_context"] == 300
    assert data["tool_result_bytes"] is None
    assert data["usage"] == {
        name: {
            "reported": profile.tokens(name),
            "coverage": sum(getattr(m, name) is not None for m in profile.models),
            "calls": 4,
        }
        for name in ("input_tokens", "output_tokens", "total_tokens", "cached_input_tokens")
    }
    assert data["retries"] == 1 and data["compaction_calls"] == 1


async def test_injected_tool_and_environment_reach_real_loop(
    tmp_path: Path, monkeypatch: Any
) -> None:
    seen = []

    async def execute(args: Json, context: ExecutionContext, emit: Any) -> ToolResult:
        seen.append(args)
        return ToolResult(context.call_id, True, {"stdout": "container"})

    class Model(ScriptedModel):
        async def complete(
            self, messages: list[Message], tools: list[ToolSpec], emit: Emit
        ) -> ModelReply:
            spec = next(t for t in tools if t.name == "exec_command")
            native = native_tools()["exec_command"].spec
            assert spec.input_schema == native.input_schema
            assert "Internet access is disabled" in spec.description
            assert "Internet access is disabled" not in native.description
            return await super().complete(messages, tools, emit)

        async def close(self) -> None:
            pass

    model = Model([reply(call("exec_command", {"command": "pwd"})), reply(text="done")])
    monkeypatch.setattr(bootstrap, "ChatModel", lambda _: model)
    cfg = config(monkeypatch)
    env = Environment(Docker([]), load_case(CASE_IDS[-1]), tmp_path)
    registry = env.registry()
    registry["exec_command"] = Tool(registry["exec_command"].spec, execute)
    convo = bootstrap.Conversation(
        tmp_path,
        cfg,
        ignore,
        home=tmp_path / "history",
        registry=registry,
        environment_display=("Linux", "/workspace"),
    )
    try:
        assert (await convo.turn("test injection")).outcome == "completed"
        assert seen == [{"command": "pwd"}]
        assert "OS=Linux;" in model.requests[0][0].content
        assert "workspace=/workspace." in model.requests[0][0].content
    finally:
        await convo.close()
    log = next((tmp_path / "history").rglob("*.jsonl"))
    records = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    configuration = next(r["data"] for r in records if r["kind"] == "configuration")
    assert configuration["context_policy"] == "Context Runtime V0.2.1"
    assert configuration["observation_projection"]["version"] == "compact-v1"
    assert str(tmp_path) in instructions(tmp_path, cfg.limits.shell)


async def test_startup_failure_preserves_injected_registry(
    tmp_path: Path, monkeypatch: Any
) -> None:
    class Model:
        async def close(self) -> None:
            pass

    async def fail(*args: Any) -> list[str]:
        args[1].clear()
        raise RuntimeError("fixture startup failure")

    monkeypatch.setattr(bootstrap, "ChatModel", lambda _: Model())
    monkeypatch.setattr(bootstrap, "connect_servers", fail)
    convo = bootstrap.Conversation(
        tmp_path, config(monkeypatch), ignore, home=tmp_path, registry={}
    )
    try:
        with pytest.raises(RuntimeError):
            await convo.turn("task")
        assert convo.registry == {}  # Must not fall back to host exec_command.
    finally:
        await convo.close()


@pytest.mark.parametrize("args", [["eval"], ["eval", "--all", CASE_IDS[0]]])
def test_cli_selection_errors_before_config(args: list[str], monkeypatch: Any) -> None:
    monkeypatch.setattr(sys, "argv", ["nexus", *args])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2


async def test_cli_dispatch_does_not_require_tty(monkeypatch: Any) -> None:
    cfg = config(monkeypatch)
    captured = []

    async def evaluate(selected: list[str], supplied: Any) -> int:
        captured.extend(selected)
        assert supplied is cfg
        return 0

    monkeypatch.setattr(runner, "evaluate", evaluate)
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    assert await cli.application(argparse.Namespace(command="eval", all=True, case=None)) == 0
    assert captured == list(CASE_IDS)


@pytest.mark.parametrize("limited", [False, True])
@pytest.mark.parametrize("cleanup_failure", [None, "conversation", "validator"])
async def test_runner_disables_mcp_and_keeps_limited_separate_from_pass(
    tmp_path: Path,
    monkeypatch: Any,
    limited: bool,
    cleanup_failure: str | None,
) -> None:
    cfg = config(monkeypatch)
    cfg.mcp_servers = {"must-not-connect": {"command": "never"}}
    if limited:
        cfg.limits.max_steps = 1
    environments = []

    class FakeEnvironment:
        def __init__(self, *args: Any) -> None:
            self.workspace = tmp_path / "workspace"
            self.workspace.mkdir(exist_ok=True)
            self.tree, self.base_commit, self.broken = "tree", "base", False
            environments.append(self)

        async def prepare(self) -> None:
            pass

        def registry(self) -> dict[str, Tool]:
            return {}

        async def collect_patch(self, destination: Path) -> None:
            await asyncio.to_thread(destination.write_text, "candidate", encoding="utf-8")

        async def close(self) -> None:
            pass

    class Model(ScriptedModel):
        async def complete(self, messages: Any, tools: Any, emit: Any) -> Any:
            assert "update_plan" in {tool.name for tool in tools}
            return await super().complete(messages, tools, emit)

        async def close(self) -> None:
            if cleanup_failure == "conversation":
                raise RuntimeError("fixture cleanup failure")

    connected = []

    async def connect(configured: Any, *args: Any) -> list[str]:
        connected.append(configured)
        return []

    async def validate(*args: Any) -> Json:
        if cleanup_failure == "validator":
            raise CleanupError("fixture validator cleanup failed")
        return dict(status="passed", reason="fixture", checks=[], duration_s=1)

    monkeypatch.setattr(runner, "Environment", FakeEnvironment)
    monkeypatch.setattr(runner, "validate", validate)
    monkeypatch.setattr(
        bootstrap,
        "ChatModel",
        lambda _: Model([reply(call("missing", {})) if limited else reply(text="done")]),
    )
    monkeypatch.setattr(bootstrap, "connect_servers", connect)
    case = load_case(CASE_IDS[-1])
    result, stop = await runner.run_case(
        case, "eval", tmp_path, cfg, Docker([]), tmp_path / "cache"
    )
    assert result["status"] == ("ERROR" if cleanup_failure else "PASS")
    assert stop is (cleanup_failure == "validator")
    assert result["agent"]["outcome"] == ("limited" if limited else "completed")
    assert connected == [{}] and "must-not-connect" in cfg.mcp_servers
    assert result["metrics"]["available"]
    assert (tmp_path / case.id / result["artifacts"]["session"]).is_file()


def test_eight_row_report_required_columns_and_unknown(tmp_path: Path) -> None:
    results = [runner.empty_result(load_case(cid), "run") for cid in CASE_IDS]
    results[0]["metrics"] = profile_metrics(
        RunProfile(
            run_id="run",
            models=[
                ModelCall(1, False, 1, finished=True, input_tokens=123),
                ModelCall(2, False, 1),
            ],
        )
    )
    results[0]["status"] = "FAIL"
    report = save_report(tmp_path, results, list(CASE_IDS))
    assert "FinalCtx" in report and "ToolResultBytes" in report and ">=123" in report
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert len(summary["cases"]) == 8 and summary["counts"]["NOT_RUN"] == 7
    assert summary["totals"]["input_tokens"]["reported"] == 123
    html = (tmp_path / "report.html").read_text(encoding="utf-8")
    assert "FinalCtx" in html and "ToolResultBytes" in html and "&gt;=123" in html
    assert "部分统计" in html and "unknown" in html
    assert html.count("<details>") == 8
    assert public_url("https://user:password@example.com/v1?key=secret") == "https://example.com/v1"


def test_html_report_escapes_content_and_keeps_local_artifact_links(tmp_path: Path) -> None:
    result = runner.empty_result(load_case(CASE_IDS[0]), "run")
    result["status"] = "PASS"
    result["agent"] = dict(outcome="limited", reason="max_steps")
    result["diagnostics"] = [dict(category="validation", reason='<script>alert("x")</script>')]
    result["artifacts"] = {"patch": "a #1.diff", "unsafe": "../../outside"}
    save_report(tmp_path, [result], [result["case_id"]])
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    html = render_html(summary, {"model": {"name": '<img src=x onerror="alert(1)">'}})
    assert "<script>" not in html and "<img" not in html
    assert "&lt;script&gt;" in html and "&lt;img" in html
    assert f'href="{result["case_id"]}/a%20%231.diff"' in html
    assert 'href="../../outside"' not in html and "/../../" not in html
    assert 'class="badge pass">PASS' in html and "max_steps" in html
    assert "<script" not in html and "<link" not in html


@pytest.mark.parametrize("stop_after", [None, 2])
async def test_suite_sequential_persistence_and_stop(
    tmp_path: Path, monkeypatch: Any, stop_after: int | None
) -> None:
    cfg = config(monkeypatch)
    calls: list[str] = []

    async def connect(cache: Path) -> Docker:
        return Docker([])

    async def run_case(case: Any, run_id: str, directory: Path, *args: Any) -> tuple[Json, bool]:
        if calls:
            previous = directory / calls[-1] / "result.json"
            assert previous.is_file(), (
                "Each preceding result must be persisted before the next case"
            )
        calls.append(case.id)
        result = runner.empty_result(case, run_id)
        stop = len(calls) == stop_after
        result["status"] = "ABORTED" if stop else "ERROR" if len(calls) == 1 else "PASS"
        return result, stop

    monkeypatch.setattr(Docker, "connect", connect)
    monkeypatch.setattr(runner, "run_case", run_case)
    code = await runner.evaluate(list(CASE_IDS), cfg, tmp_path)
    assert code == (130 if stop_after else 2)
    summary_path = next((tmp_path / "results").glob("*/summary.json"))
    manifest = json.loads((summary_path.parent / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["context_policy"] == "Context Runtime V0.2.1"
    assert manifest["observation_projection"] == {
        "version": "compact-v1",
        "working_set_tokens": 16384,
        "working_set_fraction": 0.25,
        "success_max_bytes": 1024,
        "failure_max_bytes": 2048,
    }
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert len(summary["cases"]) == 8
    assert calls == list(CASE_IDS[:stop_after] if stop_after else CASE_IDS)
    assert summary["counts"]["NOT_RUN"] == (6 if stop_after else 0)


def test_report_cache_aggregation_includes_old_reports_as_unknown(tmp_path: Path) -> None:
    results = [runner.empty_result(load_case(cid), "run") for cid in CASE_IDS[:3]]
    for result, cached in zip(results, (None, 0, 80), strict=True):
        result["metrics"] = profile_metrics(
            RunProfile(
                run_id="run",
                models=[
                    ModelCall(
                        1, False, 1, finished=True, input_tokens=100, cached_input_tokens=cached
                    )
                ],
            )
        )
    del results[0]["metrics"]["usage"]["cached_input_tokens"]  # Pre-telemetry report.
    report = save_report(tmp_path, results, list(CASE_IDS[:3]))
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert summary["totals"]["cached_input_tokens"] == dict(reported=80, coverage=2, calls=3)
    assert summary["totals"]["input_tokens"]["reported"] == 300
    assert "Cached input (subset) >=80" in report and "Cache usage coverage 2/3" in report
    assert summary["counts"]["NOT_RUN"] == 3  # Usage never changes a verdict.


@pytest.mark.parametrize("container", [False, True])
async def test_wrong_command_field_is_rejected_before_execution(
    tmp_path: Path, monkeypatch: Any, container: bool
) -> None:
    async def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Invalid tool arguments must not start a process")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    env = Environment(Docker([]), load_case(CASE_IDS[-1]), tmp_path)
    execute = env.execute if container else native_execute
    recorder = Recorder()
    result = await execute(
        {"cmd": "echo should-not-run"}, ExecutionContext(tmp_path, "invalid", "/bin/sh"), recorder
    )
    assert not result.ok and result.error_code == "invalid_arguments"
    assert "non-empty 'command' string" in result.data["detail"]
    assert "no other fields" in result.data["detail"]
    assert recorder.events == []
