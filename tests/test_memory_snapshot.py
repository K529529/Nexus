from __future__ import annotations

import pytest

from nexus.core.memory_snapshot import HEADER, MAX_SNAPSHOT_TOKENS, build_snapshot
from nexus.core.tokens import TokenEstimator
from nexus.core.types import Message


@pytest.mark.parametrize(
    "estimator",
    [
        TokenEstimator("deepseek-flash", "https://api.deepseek.com"),
        TokenEstimator("Qwen/Qwen3.5-35B-A3B", "https://api.siliconflow.cn"),
        TokenEstimator("test"),
    ],
)
def test_snapshot_uses_model_tokens_including_labels(estimator: TokenEstimator) -> None:
    snapshot = build_snapshot(
        [
            ("SCRATCHPAD.md (open)", "未完成 " * 6000, 2048, "head"),
            ("daily/today.md", "old " * 6000 + "LATEST_TODAY", 2048, "tail"),
            ("MEMORY.md", "FIRST_MEMORY " + "technical 技术 " * 6000 + "LAST_MEMORY", 4096, "ends"),
            ("daily/yesterday.md", "old " * 6000 + "LATEST_YESTERDAY", 1024, "tail"),
        ],
        estimator,
    )
    assert snapshot["tokens"] == estimator.raw([Message("system", snapshot["content"])], [])
    assert snapshot["tokens"] <= MAX_SNAPSHOT_TOKENS
    for name, budget in [
        ("SCRATCHPAD.md (open)", 2048),
        ("daily/today.md", 2048),
        ("MEMORY.md", 4096),
        ("daily/yesterday.md", 1024),
    ]:
        assert snapshot["source_tokens"][name] <= budget
    for text in ["LATEST_TODAY", "FIRST_MEMORY", "LAST_MEMORY", "LATEST_YESTERDAY"]:
        assert text in snapshot["content"]


def test_empty_snapshot_has_no_empty_source_sections() -> None:
    value = build_snapshot([("MEMORY.md", "", 4096, "ends")], TokenEstimator())
    assert value["content"] == HEADER
    assert value["source_tokens"] == {}
