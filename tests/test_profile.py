from __future__ import annotations

import argparse
from copy import deepcopy
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from rich.console import Console

from nexus.app import bootstrap, cli
from nexus.app import profile as profile_module
from nexus.app.config import Config, ModelConfig
from nexus.app.events import Events
from nexus.app.profile import ProfileConsumer, RunProfiler, render_profile
from nexus.app.session import SessionLog, read_records
from nexus.core import agent
from nexus.core.context import estimate
from nexus.core.types import (
    Emit,
    ExecutionContext,
    Json,
    Limits,
    Message,
    RuntimeEvent,
    Session,
    Tool,
    ToolResult,
    ToolSpec,
)
from tests.conftest import ScriptedModel, call, reply


def event(kind: str, **data: Any) -> RuntimeEvent:
    return RuntimeEvent(kind, "2026-10-02T00:00:00+00:00", "session", "run", data)


def usage(input_tokens: int = 100, output_tokens: int = 10) -> Json:
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "source": "reported",
    }


def fixture_profiler() -> RunProfiler:
    profiler = RunProfiler()
    for item in [
        event("run_started"),
        event("model_started", step=1, attempt=1),
        event("model_finished", step=1, attempt=1, usage=usage(), duration_ms=1900),
        event(
            "tool_started", step=1, call_id="a", name="exec_command", arguments_json="PRIVATE_ARGS"
        ),
        event(
            "tool_finished",
            step=1,
            call_id="a",
            ok=False,
            error_code="command_exit_nonzero",
            exit_code=7,
            result_bytes=2048,
            duration_ms=4200,
            truncated=True,
        ),
        event("tool_started", step=1, call_id="b", name="mcp_test"),
        event(
            "tool_finished",
            step=1,
            call_id="b",
            ok=True,
            result_bytes=1024,
            duration_ms=20,
            truncated=False,
        ),
        event("model_started", step=2, attempt=1, purpose="compaction"),
        event(
            "model_finished",
            step=2,
            attempt=1,
            purpose="compaction",
            usage=usage(900, 30),
            duration_ms=800,
        ),
        event("context_compacted"),
        event("model_started", step=2, attempt=1),
        # Zero is explicitly reported here, never assumed for a failed request.
        event(
            "model_finished",
            step=2,
            attempt=1,
            error="model_transport",
            usage=usage(0, 0),
            duration_ms=50,
        ),
        event("model_started", step=2, attempt=2),
        event("model_finished", step=2, attempt=2, usage=usage(200, 20), duration_ms=2300),
        event("run_finished", outcome="completed", reason=None, steps=2, duration_ms=10000),
    ]:
        profiler.consume(item)
    return profiler


def test_aggregation_and_report_separate_cost_context_and_compaction() -> None:
    profiler = fixture_profiler()
    p = profiler.profile
    assert p.outcome == "completed" and p.reason is None and p.duration_ms == 10000
    assert p.steps == 2 and len(p.models) == 4 and sum(m.compaction for m in p.models) == 1
    assert p.tokens("input_tokens") == 1200
    assert p.tokens("output_tokens") == 60 and p.tokens("total_tokens") == 1260
    assert p.context_inputs == (100, 200, 200) and p.compactions == 1
    completion = p.completion(2)
    assert completion is not None and completion is p.models[-1]
    assert completion.duration_ms == 2300
    assert len(p.tools) == 2 and p.tools["a"].exit_code == 7 and p.tools["a"].truncated
    assert sum(t.result_bytes or 0 for t in p.tools.values()) == 3072
    output = StringIO()
    path = Path("actual/session.jsonl")
    render_profile(p, Console(file=output, width=110, color_system=None), 5000, path)
    text = output.getvalue()
    for value in [
        "Run Profile",
        "1,200",
        "1,260",
        "4.0%",
        "3.0 KiB",
        "4.2s",
        "command_exit_nonzero",
        "7 ×1",
        "Trajectory:",
        str(path),
    ]:
        assert value in text
    assert "PRIVATE_ARGS" not in text
    assert "2.3s" in text and "0.8s" not in text  # Summary latency is not the step completion.


@pytest.mark.parametrize("steps", [1, 10, 11, 37])
def test_timeline_is_bounded(steps: int) -> None:
    profiler = RunProfiler()
    profiler.consume(event("run_started"))
    for step in range(1, steps + 1):
        profiler.consume(event("model_started", step=step))
        profiler.consume(event("model_finished", step=step, usage=usage(step, 1), duration_ms=20))
    profiler.consume(event("run_finished", outcome="completed", steps=steps))
    expected = (
        list(range(1, steps + 1)) if steps <= 10 else [1, 2, 3, None, *range(steps - 6, steps + 1)]
    )
    assert profiler.profile.timeline() == expected
    out = StringIO()
    render_profile(profiler.profile, Console(file=out, width=120), 1000, None)
    assert ("middle steps omitted" in out.getvalue()) == (steps > 10)


@pytest.mark.parametrize("same_run_id", [False, True])
def test_each_run_started_resets_even_resumed_run(same_run_id: bool) -> None:
    profiler = fixture_profiler()
    next_event = event("run_started")
    if not same_run_id:
        next_event.run_id = "run2"
    profiler.consume(next_event)
    assert not profiler.profile.models and not profiler.profile.tools
    assert profiler.profile.compactions == 0 and profiler.profile.steps == 0
    assert profiler.profile.context_inputs == (None, None, None)
    assert profiler.profile.outcome == "unknown"


def test_missing_usage_and_failed_step_are_unknown_not_zero() -> None:
    profiler = RunProfiler()
    profiler.consume(event("run_started"))
    profiler.consume(event("model_started", step=1))
    profiler.consume(event("model_finished", step=1, usage={"source": "unknown"}))
    profiler.consume(event("model_started", step=2))
    profiler.consume(event("model_finished", step=2, usage=usage(200, 20)))
    profiler.consume(event("model_started", step=3))
    profiler.consume(event("model_finished", step=3, error="model_transport"))
    profiler.consume(event("run_finished", outcome="failed", reason="model_transport", steps=3))
    p = profiler.profile
    assert all(p.tokens(key) is None for key in ("input_tokens", "output_tokens", "total_tokens"))
    assert p.context_inputs == (None, None, 200)
    assert p.completion(3) is p.models[-1] and p.models[-1].duration_ms is None
    out = StringIO()
    render_profile(p, Console(file=out, width=120), 1000, None)
    assert "unknown (model_transport)" in out.getvalue()
    assert "Trajectory: unknown" in out.getvalue()


@pytest.mark.parametrize("profiled", [False, True])
async def test_runtime_metadata_and_equivalence(
    tmp_path: Path, monkeypatch: Any, profiled: bool
) -> None:
    monkeypatch.setattr(agent, "time", SimpleNamespace(monotonic=lambda: 10.0))
    monkeypatch.setattr(agent, "uuid4", lambda: SimpleNamespace(hex="fixed-run"))
    runs: list[Any] = []
    for enabled in [False, profiled]:
        captured: list[RuntimeEvent] = []
        invoked: list[str] = []

        async def consumer(item: RuntimeEvent, collected: list[RuntimeEvent] = captured) -> None:
            collected.append(item)

        async def execute(
            args: Json, context: ExecutionContext, emit: Emit, calls: list[str] = invoked
        ) -> ToolResult:
            calls.append(context.call_id)
            failed = context.call_id == "shell"
            return ToolResult(
                context.call_id,
                not failed,
                {"stdout": "中文结果", "exit_code": 7 if failed else None},
                "command_exit_nonzero" if failed else None,
                25,
                failed,
            )

        registry = {
            name: Tool(ToolSpec(name, "", {}), execute)
            for name in ["exec_command", "apply_patch", "mcp_test"]
        }
        model = ScriptedModel(
            [
                reply(
                    call("exec_command", {}, "shell"),
                    call("apply_patch", {}, "patch"),
                    call("mcp_test", {}, "remote"),
                ),
                reply(text="finished"),
            ]
        )
        session = Session(tmp_path)
        writer = SessionLog.create(session, "test", {}, tmp_path)
        wrapper = ProfileConsumer(consumer) if enabled else None
        try:
            result = await agent.run_turn(
                session,
                "test",
                model,
                registry,
                Events(session, writer, wrapper or consumer),
                Limits(),
            )
            records, _ = read_records(writer.stream)
        finally:
            writer.close()
        relevant = [
            r
            for r in records
            if r["kind"] in {"model_started", "model_finished", "tool_started", "tool_finished"}
        ]
        assert all(r["data"]["step"] in {1, 2} for r in relevant)
        assert [r["data"]["step"] for r in relevant if r["kind"] == "model_finished"] == [1, 2]
        for record in [r for r in relevant if r["kind"] == "tool_finished"]:
            data = record["data"]
            message = next(m for m in session.messages if m.seq == data["message_seq"])
            assert (
                data["result_bytes"] == len(message.content.encode("utf-8")) > len(message.content)
            )
            assert data["exit_code"] == (7 if data["call_id"] == "shell" else None)
            assert data["truncated"] == (data["call_id"] == "shell")
            assert "stdout" not in data and "content" not in data
        if wrapper:
            assert len(wrapper.profiler.profile.models) == result.model_calls == 2
            assert len(wrapper.profiler.profile.tools) == result.tool_calls == 3
        runs.append(
            (
                result,
                session.messages,
                model.requests,
                invoked,
                [(e.kind, e.data) for e in captured],
            )
        )
    assert runs[0] == runs[1]


async def test_profile_preserves_real_safety_compaction(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(agent, "time", SimpleNamespace(monotonic=lambda: 10.0))
    runs: list[Any] = []
    for enabled in [False, True]:
        captured: list[RuntimeEvent] = []

        async def consumer(item: RuntimeEvent, collected: list[RuntimeEvent] = captured) -> None:
            collected.append(item)

        session = Session(tmp_path, run_id="interrupted", resume_run_id="interrupted")
        writer = SessionLog.create(session, "test", {}, tmp_path)
        wrapper = ProfileConsumer(consumer)
        events = Events(session, writer, wrapper if enabled else consumer)
        try:
            await agent.append_message(session, Message("system", "repository rules"), events)
            await agent.append_message(session, Message("user", "original task"), events)
            for i in range(3):
                ident = f"read-{i}"
                await agent.append_message(
                    session, reply(call("exec_command", {}, ident)).message, events
                )
                await agent.append_message(
                    session,
                    ToolResult(
                        ident, True, {"stdout": "observation " * (500 if i == 0 else 1)}
                    ).message(),
                    events,
                )
            original = deepcopy(session.messages)
            budget = int(estimate(original + [Message("user", "continue")], []) / 0.90)
            limits = Limits(context_window=budget + 500 + 1024, max_output_tokens=500)
            model = ScriptedModel([reply(text="Read code; continue the task."), reply(text="done")])
            result = await agent.run_turn(session, "continue", model, {}, events, limits)
            assert result.outcome == "completed" and result.model_calls == 2
            assert session.messages[: len(original)] == original
            assert len(session.compactions) == 1
            assert model.requests[0][-1].content.startswith("Summarize the earlier")
            assert not any(m.tool_call_id == "read-0" for m in model.requests[1])
            model_events = [e for e in captured if e.kind in {"model_started", "model_finished"}]
            assert all(e.data["step"] == 1 for e in model_events)
            assert [e.data.get("purpose") for e in model_events] == [
                "compaction",
                "compaction",
                None,
                None,
            ]
            if enabled:
                p = wrapper.profiler.profile
                assert p.compactions == 1 and len(p.models) == 2
                assert sum(m.compaction for m in p.models) == 1
                assert p.completion(1) is p.models[-1]
            runs.append((result, session.messages, session.compactions, model.requests))
        finally:
            writer.close()
    assert runs[0] == runs[1]


@pytest.mark.parametrize(
    "argv,enabled",
    [
        ([], False),
        (["--profile"], True),
        (["exec", "task"], False),
        (["exec", "task", "--profile"], True),
        (["--profile", "exec", "task"], True),
        (["resume", "--profile"], True),
    ],
)
def test_cli_profile_parse(monkeypatch: Any, argv: list[str], enabled: bool) -> None:
    observed: list[argparse.Namespace] = []

    async def app(args: argparse.Namespace) -> int:
        observed.append(args)
        return 0

    monkeypatch.setattr(cli, "application", app)
    monkeypatch.setattr("sys.argv", ["nexus", *argv])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0 and observed[0].profile is enabled


@pytest.mark.parametrize(
    "argv", [["exec", "task", "--profile", "--json"], ["--profile", "exec", "task", "--json"]]
)
def test_cli_rejects_profile_with_json(monkeypatch: Any, argv: list[str]) -> None:
    monkeypatch.setattr("sys.argv", ["nexus", *argv])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2


@pytest.fixture
def offline_cli(tmp_path: Path, monkeypatch: Any) -> SimpleNamespace:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TEST_PROFILE_KEY", "test-only")
    cfg = Config(ModelConfig("test", 32768, api_key_env="TEST_PROFILE_KEY"), Limits())
    output = StringIO()
    console = Console(file=output, width=120, color_system=None)
    conversations: list[bootstrap.Conversation] = []
    models: list[ScriptedModel] = []

    class Model(ScriptedModel):
        def __init__(self, config: Any) -> None:
            super().__init__([reply(text="FINAL-A"), reply(text="FINAL-B")])
            models.append(self)

        async def close(self) -> None:
            pass

    def conversation(
        workspace: Path, config: Config, consumer: Any, **kwargs: Any
    ) -> bootstrap.Conversation:
        result = bootstrap.Conversation(
            workspace, config, consumer, home=tmp_path / "sessions-home", **kwargs
        )
        conversations.append(result)
        return result

    monkeypatch.setattr(bootstrap, "ChatModel", Model)
    monkeypatch.setattr(cli, "Conversation", conversation)
    monkeypatch.setattr(cli, "Console", lambda **kwargs: console)
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    monkeypatch.setattr(cli, "read_toml", lambda _: {"model": {"name": "test"}})
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    return SimpleNamespace(output=output, conversations=conversations, models=models)


@pytest.mark.parametrize("enabled", [False, True])
async def test_exec_renders_only_with_profile(offline_cli: SimpleNamespace, enabled: bool) -> None:
    assert (
        await cli.application(
            argparse.Namespace(command="exec", task="task", json=False, profile=enabled)
        )
        == 0
    )
    output = offline_cli.output.getvalue()
    assert ("Run Profile" in output) is enabled
    if enabled:
        assert output.index("FINAL-A") < output.index("Run Profile")
        assert str(offline_cli.conversations[0].writer.path) in output
    assert len(offline_cli.models[0].requests) == 1


async def test_interactive_profile_resets_after_each_drive(
    offline_cli: SimpleNamespace, monkeypatch: Any
) -> None:
    answers = iter(["task A", "task B", "/exit"])
    profiles: list[Any] = []
    original_render = profile_module.render_profile

    class Prompt:
        async def prompt_async(self, _: str) -> str:
            return next(answers)

    def render(*args: Any, **kwargs: Any) -> None:
        profiles.append(deepcopy(args[0]))
        original_render(*args, **kwargs)

    monkeypatch.setattr(cli, "PromptSession", Prompt)
    monkeypatch.setattr(profile_module, "render_profile", render)
    assert await cli.application(argparse.Namespace(command=None, profile=True)) == 0
    assert len(profiles) == 2 and profiles[0].run_id != profiles[1].run_id
    assert all(
        len(p.models) == 1 and p.steps == 1 and p.tokens("total_tokens") == 15 for p in profiles
    )
    assert offline_cli.output.getvalue().count("Run Profile") == 2
    assert len(offline_cli.models[0].requests) == 2


@pytest.mark.parametrize("failure", ["collection", "render"])
async def test_diagnostic_failure_does_not_fail_agent(
    offline_cli: SimpleNamespace, monkeypatch: Any, failure: str
) -> None:
    def fail(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("diagnostic failure")

    if failure == "collection":
        monkeypatch.setattr(RunProfiler, "consume", fail)
    else:
        monkeypatch.setattr(profile_module, "render_profile", fail)
    assert (
        await cli.application(
            argparse.Namespace(command="exec", task="task", json=False, profile=True)
        )
        == 0
    )
    text = offline_cli.output.getvalue()
    assert "FINAL-A" in text and "Run Profile unavailable" in text
    assert len(offline_cli.models[0].requests) == 1
