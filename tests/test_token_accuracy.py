from __future__ import annotations

import gzip
import json
import math
from copy import deepcopy
from pathlib import Path

import httpx
import pytest
from openai import AsyncOpenAI

from nexus.app.config import ConfigError, ModelConfig, load_config
from nexus.app.events import Events
from nexus.app.session import SessionLog, read_records, replay
from nexus.core.agent import append_message, run_turn
from nexus.core.compaction import split_history, summary_input
from nexus.core.context import Context, ContextBuilder
from nexus.core.model import ChatModel, context_overflow
from nexus.core.tokens import TokenEstimator, estimate
from nexus.core.types import Json, Limits, Message, ModelError, Session, ToolCall, ToolSpec, Usage
from tests.conftest import reply
from tests.test_model import chunk, sse
from tests.test_session_compaction import SummaryModel, ignore, seed, summary_reply

FIXTURES = Path(__file__).parent / "fixtures" / "token_usage.json"


def estimator(profile: str) -> TokenEstimator:
    if profile == "deepseek":
        return TokenEstimator("deepseek-flash", "https://api.deepseek.com", reasoning_effort="high")
    return TokenEstimator(
        "Qwen/Qwen3.5-35B-A3B", "https://api.siliconflow.cn/v1", enable_thinking=False
    )


def input_messages(payload: Json) -> tuple[list[Message], list[ToolSpec]]:
    messages = [
        Message(
            m["role"],
            m["content"],
            tool_calls=[
                ToolCall(c["id"], c["function"]["name"], c["function"]["arguments"])
                for c in m.get("tool_calls", [])
            ],
            tool_call_id=m.get("tool_call_id"),
            protocol_data={"fields": {"reasoning_content": m["reasoning_content"]}}
            if "reasoning_content" in m
            else None,
        )
        for m in payload["messages"]
    ]
    tools = [
        ToolSpec(t["function"]["name"], t["function"]["description"], t["function"]["parameters"])
        for t in payload.get("tools", [])
    ]
    return messages, tools


@pytest.mark.parametrize(
    "sample", json.loads(FIXTURES.read_text(encoding="utf-8")), ids=lambda s: s["id"]
)
def test_provider_receipts_with_roles_tools_and_content_types(sample: Json) -> None:
    messages, tools = input_messages(sample["payload"])
    before = deepcopy(messages)
    actual = sample["prompt_tokens"]
    measured = estimator(sample["profile"]).raw(messages, tools)
    assert abs(measured - actual) / actual < 0.05
    assert messages == before  # Rendering must not mutate tool arguments or protocol fields.


@pytest.mark.parametrize("stage", ["A", "B"])
@pytest.mark.parametrize("case", ["ABF-original", "CDE-initial", "DE-update"])
def test_frozen_native_compaction_usage(stage: str, case: str) -> None:
    root = Path("docs/next/evidence/compaction-simplification") / f"stage-{stage}"
    messages, tools = input_messages(
        {"messages": json.loads((root / f"{case}.request.json").read_text(encoding="utf-8"))}
    )
    for repeat in [1, 2]:
        result = json.loads(
            (root / f"{case}-qwen-{repeat}/result.json").read_text(encoding="utf-8")
        )
        assert estimator("qwen").raw(messages, tools) == result["raw_usage"]["prompt_tokens"]


def test_independent_bounded_calibration_and_cache_accounting() -> None:
    context = Context(
        Limits(context_window=128000),
        main_estimator=estimator("deepseek"),
        compaction_estimator=estimator("qwen"),
    )
    messages = [Message("user", "Historical evidence. " * 500)]
    raw = context.tokens(messages, [])
    response = reply(text="ok")
    response.usage = Usage(raw + 100, 1, raw + 101, "reported", cached_input_tokens=raw)
    context.observe(response, messages, [])
    assert context.tokens(messages, []) == raw + 100
    assert context.compaction_estimator.factor == 1
    for reported in [raw * 100, raw * 100, raw * 100]:
        context.main_estimator.observe(reported, messages, [])
    assert context.tokens(messages, []) == math.ceil(raw * 1.15)
    context.compaction_estimator.observe(1, messages, [])
    assert context.compaction_estimator.factor == 0.85
    assert context.main_estimator.factor == 1.15
    context.main_estimator.observe(None, messages, [])
    assert context.tokens(messages, []) == raw
    assert TokenEstimator("deepseek-flash", "https://another.example/v1").profile == "fallback"
    assert TokenEstimator("unmeasured", "https://api.deepseek.com").profile == "fallback"


def test_model_default_windows_and_independent_overrides(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[model]\nname="main"\n[compaction_model]\nname="compact"\n')
    config = load_config(path, require_key=False)
    assert config.model.context_window == config.compaction_model.context_window == 128000  # type: ignore[union-attr]
    path.write_text(
        '[model]\nname="main"\ncontext_window=64000\n'
        '[compaction_model]\nname="compact"\ncontext_window=96000\n'
    )
    config = load_config(path, require_key=False)
    assert config.model.context_window == 64000
    assert config.compaction_model and config.compaction_model.context_window == 96000
    for section in ["model", "compaction_model"]:
        text = '[model]\nname="main"\n[compaction_model]\nname="compact"\n'
        text = text.replace(f"[{section}]", f"[{section}]\nmax_output_tokens=126976")
        path.write_text(text)
        if section == "compaction_model":
            with pytest.raises(ConfigError, match="must exceed"):
                load_config(path, require_key=False)
        else:
            assert load_config(path, require_key=False).model.max_output_tokens == 126976


async def test_capacity_boundary_preserves_history_and_durable_log(tmp_path: Path) -> None:
    session = Session(tmp_path)
    writer = SessionLog.create(session, "boundary", {}, tmp_path)
    events = Events(session, writer, ignore)
    context = Context(
        Limits(context_window=128000),
        main_estimator=estimator("deepseek"),
        compaction_estimator=estimator("qwen"),
    )
    try:
        # Repeated inspection evidence: the legacy estimate exceeds 128K, native input fits.
        session.run_id = "boundary"
        await events("run_started", {})
        for i in range(12):
            await append_message(
                session, Message("user", f"Inspect round {i}; do not merge main."), events
            )
            await append_message(
                session, Message("assistant", "Unchanged source inspection. " * 2100), events
            )
        original = deepcopy(session.messages)
        active = ContextBuilder().build_active_context(session)
        prefix, _ = split_history(active, 20000, count=context.main_estimator.count)
        request = summary_input(session, prefix, 4096, ())
        native = context.compaction_estimator.raw(request, [])
        assert estimate(request, []) + 5120 > 128000
        assert native + 5120 < 128000
        compactor = SummaryModel([summary_reply()])
        context.compaction_window = native + 5120 - 1
        with pytest.raises(ModelError, match="compaction_input_limit"):
            await context.prepare(session, active, [], compactor, events, force=True)
        assert not compactor.requests and session.checkpoint is None
        assert session.messages == original
        records, _ = read_records(writer.stream)
        failed = [r for r in records if r["kind"] == "compaction_failed"]
        assert failed[-1]["data"]["reason"] == "compaction_input_limit"
        assert failed[-1]["data"]["compaction_token_count_calls"] == 1
        assert failed[-1]["data"]["history_split_ms"] >= 0
        assert replay(records, tmp_path).checkpoint is None
        # Equality is allowed. Same exact source, no history truncation or retry mutation.
        context.compaction_window += 1
        assert await context.prepare(session, active, [], compactor, events, force=True)
        assert len(compactor.requests) == 1 and session.checkpoint is not None
        assert session.messages == original
        records, _ = read_records(writer.stream)
        assert replay(records, tmp_path).checkpoint is not None
    finally:
        writer.close()


OVERFLOW = {
    "message": "Input token exceed the limit (request id: fixed)",
    "type": "api_error",
    "param": "",
    "code": "quota_limit_reached",
}


@pytest.mark.parametrize(
    "body",
    [
        OVERFLOW,
        {"error": OVERFLOW},
        {"error": {"code": "context_length_exceeded"}},
        {
            "message": "This model's maximum context length is 1048576 tokens. "
            "However, you requested 1049000 tokens."
        },
    ],
)
def test_known_overflow_shapes(body: Json) -> None:
    assert context_overflow(400, body, "https://api.deepseek.com")


@pytest.mark.parametrize(
    "body",
    [
        {"code": "quota_limit_reached", "message": "input tokens exceed the monthly usage limit"},
        {"code": "quota_limit_reached", "message": "Insufficient balance"},
        {"message": "Invalid messages format"},
        {"message": "max_tokens must be positive"},
        {"message": "Unknown model"},
        {},
    ],
)
def test_ordinary_400_is_not_overflow(body: Json) -> None:
    assert not context_overflow(400, body, "https://api.deepseek.com")
    assert not context_overflow(400, OVERFLOW, "https://unverified.example")


def test_near_128k_frozen_compaction_preflight() -> None:
    rows = json.loads(gzip.decompress(FIXTURES.with_name("token_boundaries.json.gz").read_bytes()))
    for row in rows:
        messages, tools = input_messages(row["payload"])
        actual = estimator(row["profile"]).raw(messages, tools)
        if row["id"].endswith("119000"):
            assert actual == row["prompt_tokens"]
            assert actual + 4096 + 1024 <= 128000
            assert estimate(messages, tools) > 128000  # Previously rejected valid compaction.
        else:
            assert actual + 4096 + 1024 > 128000  # Correctly reserve summary output/headroom.


@pytest.mark.parametrize("retry_overflows", [False, True])
async def test_fixed_provider_error_recovers_once_with_same_main(
    tmp_path: Path,
    retry_overflows: bool,
) -> None:
    requests: list[Json] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        if len(requests) == 1 or retry_overflows:
            return httpx.Response(400, json={"error": OVERFLOW})
        return httpx.Response(200, content=sse([chunk({"content": "done"}, "stop")]))

    config = ModelConfig("deepseek-flash", 128000, base_url="https://api.deepseek.com")
    client = AsyncOpenAI(
        api_key="test",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )
    main = ChatModel(config, client=client)
    session = Session(tmp_path)
    writer = SessionLog.create(session, "overflow", {}, tmp_path)
    events = Events(session, writer, ignore)
    try:
        await seed(session, events)
        compact = SummaryModel([summary_reply()])
        result = await run_turn(
            session,
            "continue",
            main,
            {},
            events,
            Limits(context_window=128000),
            compaction_model=compact,
        )
        assert len(requests) == 2 and len(compact.requests) == 1
        assert {r["model"] for r in requests} == {"deepseek-flash"}
        assert result.outcome == ("limited" if retry_overflows else "completed")
        assert session.checkpoint is not None
        records, _ = read_records(writer.stream)
        projections = [r["data"] for r in records if r["kind"] == "context_projection"]
        assert len(projections) == 2
        assert projections[0]["step"] == projections[1]["step"] == 1
        assert projections[1]["token_count_calls"] > projections[0]["token_count_calls"]
        if not retry_overflows:
            usage = next(r["data"] for r in records if r["kind"] == "context_usage")
            assert usage["token_count_calls"] == projections[1]["token_count_calls"] + 3
    finally:
        writer.close()
        await main.close()
