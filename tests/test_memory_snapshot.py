from __future__ import annotations

import pytest

from nexus.core.memory_snapshot import HEADER, MAX_SNAPSHOT_TOKENS, build_snapshot
from nexus.core.tokens import TokenEstimator
from nexus.core.types import Message
from nexus.tools.memory import SPECS


def test_memory_instructions_have_no_final_response_write_gate() -> None:
    instructions = build_snapshot([], TokenEstimator())["content"]
    for old_instruction in ("Before the final response", "decide whether this task"):
        assert old_instruction not in instructions
    assert "Choose when to write or retrieve memory as needed" in instructions
    assert (
        "When the user explicitly asks you to remember information, save it promptly"
        in instructions
    )
    assert "there is no need to write long-term memory just to summarize a task" in instructions
    assert "Ordinary answers, temporary check results, and current task progress" in instructions
    assert "Memory may be stale or wrong" in instructions


@pytest.mark.parametrize(
    "label, purpose, tool",
    [
        (
            "MEMORY.md",
            "long-term facts, project decisions, preferences, and reusable lessons",
            "memory_write",
        ),
        (
            "Daily Notes (daily/YYYY-MM-DD.md)",
            "the day's progress, interim notes, and running context",
            "memory_write",
        ),
        (
            "SCRATCHPAD.md",
            "to-do items and follow-up actions to track across sessions",
            "scratchpad",
        ),
    ],
)
def test_memory_roles_are_explicit_in_prompt_and_tool_descriptions(
    label: str, purpose: str, tool: str
) -> None:
    assert f"- {label}: {purpose}" in HEADER
    description = next(spec.description for spec in SPECS if spec.name == tool)
    assert label in description
    assert "cross-session to-do items and follow-up actions" in description


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
