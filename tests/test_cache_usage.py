from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from nexus.app.profile import RunProfiler, profile_metrics
from nexus.core.agent import run_turn
from nexus.core.types import Json, Limits, Message, Session
from tests.test_model import Recorder, chunk, model_for, sse
from tests.test_profile import event, usage


@pytest.mark.parametrize("cached", [None, 0, 80, -1, 101])
async def test_sdk_cache_usage_is_optional_subset(cached: int | None) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        reported: Json = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}
        if cached is not None:
            reported["prompt_tokens_details"] = {"cached_tokens": cached}
        ending = {**chunk({}), "choices": [], "usage": reported}
        return httpx.Response(200, content=sse([chunk({"content": "done"}, "stop"), ending]))

    model, emit = model_for(handle), Recorder()
    try:
        response = await model.complete([Message("user", "task")], [], emit)
    finally:
        await model.close()
    expected = cached if cached in (0, 80) else None
    assert response.usage.cached_input_tokens == expected
    assert response.usage.input_tokens == 100 and response.usage.total_tokens == 120
    assert emit.events[-1][1]["usage"]["cached_input_tokens"] == expected


async def test_cache_counts_retries_without_replaying_actions(tmp_path: Path) -> None:
    requests = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        reported = {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
            "prompt_tokens_details": {"cached_tokens": 80},
        }
        ending = {**chunk({}), "choices": [], "usage": reported}
        text = "" if requests == 1 else "done"
        return httpx.Response(200, content=sse([chunk({"content": text}, "stop"), ending]))

    model = model_for(handle)
    try:
        result = await run_turn(Session(tmp_path), "task", model, {}, Recorder(), Limits())
    finally:
        await model.close()
    assert result.outcome == "completed" and result.model_calls == 2
    assert result.usage.cached_input_tokens == 160
    assert result.usage.input_tokens == 200 and result.usage.total_tokens == 240


def test_profile_cache_coverage_preserves_unknown_and_zero() -> None:
    profiler = RunProfiler()
    profiler.consume(event("run_started"))
    for reported in (
        usage(),  # Old JSONL / provider without the field.
        {**usage(), "cached_input_tokens": 0},
        {**usage(), "cached_input_tokens": 80},
        {**usage(), "cached_input_tokens": 999},
    ):
        profiler.consume(event("model_started", step=1, attempt=1))
        profiler.consume(event("model_finished", usage=reported))
    assert profiler.profile.tokens("input_tokens") == 400
    assert [m.cached_input_tokens for m in profiler.profile.models] == [None, 0, 80, None]
    metrics = profile_metrics(profiler.profile)
    assert metrics["usage"] == {
        "input_tokens": {"reported": 400, "coverage": 4, "calls": 4},
        "output_tokens": {"reported": 40, "coverage": 4, "calls": 4},
        "total_tokens": {"reported": 440, "coverage": 4, "calls": 4},
        "cached_input_tokens": {"reported": 80, "coverage": 2, "calls": 4},
    }
