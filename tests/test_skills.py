from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from typing import Any

import httpx
import pytest

from nexus.app import bootstrap
from nexus.app.events import Events
from nexus.app.session import SessionError, SessionLog, read_records, replay, resume_session
from nexus.app.skills import SkillCatalog
from nexus.core.agent import append_message, run_turn
from nexus.core.context import Context, ContextBuilder, estimate, groups
from nexus.core.skills import SKILL_HEADER, active_skills, check_active, snapshot
from nexus.core.types import (
    Emit,
    ExecutionContext,
    Json,
    Limits,
    Message,
    ModelError,
    ModelReply,
    RuntimeEvent,
    Session,
    SkillInfo,
    Tool,
    ToolResult,
    ToolSpec,
    Usage,
)
from nexus.tools.registry import default_tools
from nexus.tools.skills import create_load_skill_tool
from tests.conftest import Recorder, ScriptedModel, call, reply
from tests.test_conversation import config, ignore
from tests.test_model import chunk, model_for, sse
from tests.test_safety_compaction import add_tools


def install(
    root: Path, name: str = "regression", body: str = "Unique regression guidance."
) -> Path:
    path = root / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {name}\ndescription: Regression tests\n---\n{body}", encoding="utf-8"
    )
    return path


def body(messages: list[Message]) -> str:
    return "\n".join(m.content for m in messages)


def test_catalog_safe_yaml_limits_and_metadata_only(tmp_path: Path) -> None:
    path = install(tmp_path)
    catalog = SkillCatalog(tmp_path)
    assert catalog.items == (SkillInfo("regression", "Regression tests"),)
    assert "Unique" not in repr(catalog.items)
    assert catalog.load("regression").body == "Unique regression guidance."
    for invalid in ["../regression", "REGRESSION", "", "a--b", "a" * 65]:
        with pytest.raises(ValueError):
            catalog.load(invalid)
    malformed = [
        "no header",
        "---\nname: regression",
        "---\n- list\n---\nbody",
        "---\nname: other\ndescription: mismatch\n---\nbody",
        "---\nname: regression\ndescription: true\n---\nbody",
        "---\nname: regression\ndescription: test\n---\n   ",
        "---\nname: regression\ndescription: !!python/object:evil {}\n---\nbody",
        "---\nname: regression\ndescription: test\n---\n" + "x" * 16385,
        "x" * 20481,
    ]
    for source in malformed:
        path.write_text(source, encoding="utf-8")
        catalog.refresh()
        assert not catalog.items and catalog.warnings
    install(tmp_path, body="x" * 16384)
    catalog.refresh()
    assert len(catalog.load("regression").body) == 16384


def test_snapshot_copy_bounds_and_catalog_count(tmp_path: Path) -> None:
    value = snapshot("one", "description", "body")
    data = asdict(value)
    data["body"] = "mutated"
    assert value.body == "body"
    with pytest.raises(ValueError):
        check_active((value,) * 5)
    with pytest.raises(ValueError):
        check_active(tuple(snapshot(str(i), "desc", "x" * 12000) for i in range(3)))
    for i in range(33):
        install(tmp_path, f"skill-{i:02d}")
    catalog = SkillCatalog(tmp_path)
    assert len(catalog.items) == 32 and catalog.warnings


async def saved_skill(tmp_path: Path) -> tuple[Session, SessionLog, Events]:
    install(tmp_path / "skills")
    catalog = SkillCatalog(tmp_path / "skills")
    session = Session(tmp_path, skill_catalog=catalog.items)
    writer = SessionLog.create(session, "task", {}, tmp_path)
    events = Events(session, writer, ignore)
    await append_message(session, Message("system", "root rules"), events)
    model = ScriptedModel([reply(call("load_skill", {"name": "regression"}))])
    result = await run_turn(
        session,
        "task",
        model,
        default_tools(session, skill_loader=catalog.load),
        events,
        Limits(max_steps=1),
    )
    assert result.outcome == "limited"
    return session, writer, events


async def test_dispatch_dedup_invalid_order_no_workspace_changes(tmp_path: Path) -> None:
    path = install(tmp_path / "skills")
    original = path.read_bytes()
    catalog = SkillCatalog(tmp_path / "skills")
    session = Session(tmp_path, skill_catalog=catalog.items)
    events = Recorder()
    model = ScriptedModel(
        [
            reply(
                call("load_skill", {"name": "unknown"}, "bad"),
                call("load_skill", {"name": "regression"}, "first"),
                call("load_skill", {"name": "regression"}, "again"),
            ),
            reply(text="done"),
        ]
    )
    result = await run_turn(
        session, "task", model, default_tools(session, skill_loader=catalog.load), events, Limits()
    )
    assert result.outcome == "completed" and result.tool_calls == 3 and result.steps == 2
    updates = [data for kind, data in events.events if kind == "skill_loaded"]
    assert len(updates) == 1 and updates[0]["step"] == 1 and updates[0]["call_id"] == "first"
    assert [data["call_id"] for kind, data in events.events if kind == "tool_finished"] == [
        "bad",
        "first",
        "again",
    ]
    assert "Unique regression guidance." not in body(model.requests[0])
    assert body(model.requests[1]).count("Unique regression guidance.") == 1
    assert all("Unique regression guidance." not in m.content for m in session.messages)
    assert path.read_bytes() == original
    kinds = [kind for kind, _ in events.events]
    i = kinds.index("skill_loaded")
    assert kinds[i - 1 : i + 3] == ["tool_started", "skill_loaded", "message", "tool_finished"]


@pytest.mark.parametrize("args", [{}, {"name": 1}, {"name": "regression", "extra": 1}])
async def test_invalid_tool_arguments(tmp_path: Path, args: Json) -> None:
    install(tmp_path)
    session = Session(tmp_path, run_id="run")
    events = Recorder()
    result = await create_load_skill_tool(session, SkillCatalog(tmp_path).load).execute(
        args, ExecutionContext(tmp_path, "call", "unused"), events
    )
    assert result.error_code == "invalid_skill" and not session.loaded_skills and not events.events


@pytest.mark.parametrize("truncated", [False, True])
@pytest.mark.parametrize("missing_result", [False, True])
async def test_commit_resume_and_new_run(
    tmp_path: Path, truncated: bool, missing_result: bool
) -> None:
    session, writer, _ = await saved_skill(tmp_path)
    expected, run_id = session.loaded_skills, session.run_id
    records, _ = read_records(writer.stream)
    assert replay(records, tmp_path).loaded_skills == expected
    if missing_result:
        seq = next(r["seq"] for r in records if r["kind"] == "skill_loaded")
        writer.stream.seek(0)
        lines = writer.stream.readlines()
        writer.stream.seek(0)
        writer.stream.write(b"".join(lines[:seq]))
        writer.stream.truncate()
    path = writer.path
    writer.close()
    if truncated:
        with path.open("ab") as stream:
            stream.write(b'{"partial":')
    restored, writer, _ = resume_session(path, tmp_path, tmp_path)
    try:
        assert restored.loaded_skills == expected and restored.resume_run_id == run_id
        if missing_result:
            assert json.loads(restored.messages[-1].content)["error_code"] == "interrupted_unknown"
        if truncated:
            assert writer.path != path
        records, _ = read_records(writer.stream)
        assert replay(records, tmp_path).loaded_skills == expected
        model = ScriptedModel([reply(text="continued"), reply(text="new task")])
        events = Events(restored, writer, ignore)
        await run_turn(restored, "continue", model, {}, events, Limits())
        assert restored.run_id == run_id and "Unique regression guidance." in body(
            model.requests[0]
        )
        await run_turn(restored, "new task", model, {}, events, Limits())
        assert not restored.loaded_skills and SKILL_HEADER not in body(model.requests[1])
    finally:
        writer.close()


@pytest.mark.parametrize(
    "corrupt", ["run", "call", "step", "digest", "body", "wrong_tool", "duplicate"]
)
async def test_malformed_skill_event_rejected(tmp_path: Path, corrupt: str) -> None:
    _, writer, _ = await saved_skill(tmp_path)
    records, _ = read_records(writer.stream)
    writer.close()
    event = next(r for r in records if r["kind"] == "skill_loaded")
    if corrupt == "run":
        event["run_id"] = "foreign"
    elif corrupt == "call":
        event["data"]["call_id"] = "wrong"
    elif corrupt == "step":
        event["data"]["step"] = True
    elif corrupt in {"body", "digest"}:
        event["data"]["skill"][corrupt] = "changed"
    elif corrupt == "wrong_tool":
        next(r for r in records if r["kind"] == "tool_started")["data"]["name"] = "exec_command"
    else:
        records.insert(records.index(event) + 1, deepcopy(event))
    with pytest.raises(SessionError):
        replay(records, tmp_path)


async def test_write_failure_preserves_memory_and_stops_next_tool(
    tmp_path: Path, monkeypatch: Any
) -> None:
    install(tmp_path / "skills")
    catalog = SkillCatalog(tmp_path / "skills")
    session = Session(tmp_path)
    writer = SessionLog.create(session, "fail", {}, tmp_path)
    events = Events(session, writer, ignore)
    append = writer.append
    dispatched: list[bool] = []

    def fail(event: RuntimeEvent, protocol_data: Json | None = None) -> int:
        if event.kind == "skill_loaded":
            assert not session.loaded_skills
            writer.failed = True
            raise SessionError("injected write failure")
        return append(event, protocol_data)

    async def effect(args: Json, context: ExecutionContext, emit: Emit) -> ToolResult:
        dispatched.append(True)
        return ToolResult(context.call_id, True, {})

    monkeypatch.setattr(writer, "append", fail)
    registry = default_tools(session, skill_loader=catalog.load)
    registry["effect"] = Tool(ToolSpec("effect", "", {}), effect)
    try:
        result = await run_turn(
            session,
            "task",
            ScriptedModel(
                [reply(call("load_skill", {"name": "regression"}), call("effect", {}, "later"))]
            ),
            registry,
            events,
            Limits(),
        )
        assert result.outcome == "failed" and "session_write_failed" in (result.reason or "")
        assert not session.loaded_skills and not dispatched
    finally:
        writer.close()


async def test_selection_persists_off_resume_and_custom_registry_isolated(
    tmp_path: Path, monkeypatch: Any
) -> None:
    cfg = config(monkeypatch)
    path = install(tmp_path / "skills", body="Saved version test-only")
    models: list[ScriptedModel] = []

    class Model(ScriptedModel):
        def __init__(self, _: object) -> None:
            super().__init__([reply(text="done"), reply(text="done"), reply(text="done")])
            models.append(self)

        async def close(self) -> None:
            pass

    monkeypatch.setattr(bootstrap, "ChatModel", Model)
    first = bootstrap.Conversation(tmp_path, cfg, ignore, home=tmp_path)
    second = bootstrap.Conversation(tmp_path, cfg, ignore, home=tmp_path)
    custom = bootstrap.Conversation(tmp_path, cfg, ignore, home=tmp_path, registry={})
    try:
        assert "load_skill" in first.registry and not custom.registry
        assert custom.catalog is None and not custom.session.skill_catalog
        await first.select_skill("regression")
        selected = first.session.selected_skill
        assert selected and "test-only" not in selected.body  # redact before hashing/saving
        assert not second.session.selected_skill
        await first.turn("one")
        await first.turn("two")
        assert all("Saved version" in body(m) for m in models[0].requests)
        assert first.writer is not None
        saved = first.writer.path
    finally:
        await first.close()
        await second.close()
        await custom.close()
    path.unlink()
    restored = bootstrap.Conversation(tmp_path, cfg, ignore, home=tmp_path, resume=saved)
    try:
        assert restored.session.selected_skill == selected
        await restored.turn("three")
        assert "Saved version" in body(models[1].requests[0])
        await restored.select_skill(None)
        await restored.turn("four")
        assert "Saved version" not in body(models[1].requests[1])
        assert restored.writer is not None
        records, _ = read_records(restored.writer.stream)
        assert replay(records, tmp_path).selected_skill is None
    finally:
        await restored.close()


async def test_resume_tool_bound_to_restored_session(tmp_path: Path, monkeypatch: Any) -> None:
    session, writer, _ = await saved_skill(tmp_path)
    path, expected = writer.path, session.loaded_skills
    writer.close()
    install(tmp_path / "skills", "other", "Other body")
    cfg = config(monkeypatch)

    class Model(ScriptedModel):
        async def close(self) -> None:
            pass

    model = Model([reply(call("load_skill", {"name": "other"}, "other")), reply(text="done")])
    monkeypatch.setattr(bootstrap, "ChatModel", lambda _: model)
    convo = bootstrap.Conversation(tmp_path, cfg, ignore, home=tmp_path, resume=path)
    try:
        assert (await convo.turn("continue")).outcome == "completed"
        assert convo.session.loaded_skills[:1] == expected
        assert len(convo.session.loaded_skills) == 2 and session.loaded_skills == expected
    finally:
        await convo.close()


async def test_real_mock_api_projection_usage_and_reasoning(
    tmp_path: Path, monkeypatch: Any
) -> None:
    install(tmp_path)
    catalog = SkillCatalog(tmp_path)
    session = Session(tmp_path, skill_catalog=catalog.items)
    events = Recorder()
    await append_message(session, Message("system", "system"), events)
    requests: list[Json] = []
    observed: list[int] = []
    original = Context.observe

    def observe(
        self: Context, response: ModelReply, messages: list[Message], tools: list[ToolSpec]
    ) -> None:
        original(self, response, messages, tools)
        assert self.last_estimate == estimate(messages, tools)
        observed.append(self.last_estimate)

    monkeypatch.setattr(Context, "observe", observe)

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            delta = {
                "role": "assistant",
                "reasoning_content": "PRIVATE_TEST",
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "skill",
                        "type": "function",
                        "function": {"name": "load_skill", "arguments": '{"name":"regression"}'},
                    }
                ],
            }
            return httpx.Response(200, content=sse([chunk(delta, "tool_calls")]))
        return httpx.Response(200, content=sse([chunk({"content": "done"}, "stop")]))

    model = model_for(handle)
    try:
        result = await run_turn(
            session,
            "task",
            model,
            default_tools(session, skill_loader=catalog.load),
            events,
            Limits(),
        )
    finally:
        await model.close()
    assert result.outcome == "completed" and len(requests) == len(observed) == 2
    assert "Unique regression guidance." not in json.dumps(requests[0])
    assert json.dumps(requests[1]).count("Unique regression guidance.") == 1
    assert (
        next(m for m in requests[1]["messages"] if m["role"] == "assistant")["reasoning_content"]
        == "PRIVATE_TEST"
    )
    assert "PRIVATE_TEST" not in json.dumps(events.events)
    assert all(SKILL_HEADER not in m.content for m in session.messages)
    projected = Context(Limits()).project(session, session.messages).messages
    again = Context(Limits()).project(session, projected).messages
    assert body(again).count(SKILL_HEADER) == 1
    again[-1].content = "mutated"
    assert "Unique regression guidance." in active_skills(session)[0].body


@pytest.mark.parametrize("fallback", [False, True])
async def test_compaction_and_provider_fallback_preserve_skills(
    tmp_path: Path, fallback: bool
) -> None:
    session, writer, events = await saved_skill(tmp_path)
    try:
        expected = session.loaded_skills
        await add_tools(session, events, "old_narrative", 1200)
        await add_tools(session, events, "recent1", 1)
        await add_tools(session, events, "recent2", 1)
        assert session.run_id
        session.resume_run_id = session.run_id
        logical = ContextBuilder().build_active_context(session, session.run_id)
        size = estimate(Context(Limits()).project(session, logical).messages, [])
        limits = Limits(
            context_window=(size * 2 if fallback else int(size / 0.88)) + 1524,
            max_output_tokens=500,
        )

        class Model(ScriptedModel):
            async def complete(
                self, messages: list[Message], tools: list[ToolSpec], emit: Emit
            ) -> ModelReply:
                assert body(messages).count("Unique regression guidance.") == 1
                groups(messages)
                if fallback and not self.requests:
                    self.requests.append(list(messages))
                    raise ModelError("context_limit")
                return await super().complete(messages, tools, emit)

        model = Model([reply(text="Historical summary"), reply(text="done")])
        result = await run_turn(session, "continue", model, {}, events, limits)
        assert result.outcome == "completed" and len(model.requests) == (3 if fallback else 2)
        assert session.loaded_skills == expected
        records, _ = read_records(writer.stream)
        compacted = next(r["data"] for r in records if r["kind"] == "context_compacted")
        assert compacted["after_estimate"] == estimate(model.requests[-1], [])
        assert all(
            SKILL_HEADER not in m.content for m in session.compactions[session.run_id].messages
        )
    finally:
        writer.close()


def test_skill_capacity_and_calibration(tmp_path: Path) -> None:
    session = Session(tmp_path, run_id="run", selected_skill=snapshot("large", "desc", "x" * 15000))
    context = Context(Limits(context_window=3524, max_output_tokens=500))
    projected = context.project(session, [Message("system", "rules")]).messages
    with pytest.raises(ModelError, match="context_limit"):
        context.check(projected, [])
    response = reply(text="done")
    response.usage = Usage(23456, 1, 23457, "reported")
    context.observe(response, projected, [])
    assert (
        context.last_estimate == estimate(projected, []) and context.tokens(projected, []) == 23456
    )
