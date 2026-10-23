from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from nexus.evaluation.cases import CASE_IDS
from nexus.evaluation.history import build_history, collect_history


def saved_run(home: Path, name: str, selected: list[str], status: str = "PASS") -> Path:
    directory = home / "results" / name
    directory.mkdir(parents=True)
    cases = [
        dict(
            case_id=cid,
            status=status,
            metrics=dict(
                available=True,
                duration_ms=100,
                usage=dict(input_tokens=dict(reported=20, coverage=1, calls=2)),
            ),
        )
        for cid in selected
    ]
    (directory / "summary.json").write_text(
        json.dumps(dict(selected=selected, cases=cases)), encoding="utf-8"
    )
    (directory / "manifest.json").write_text(
        json.dumps(dict(model=dict(name="</script><script>alert(1)</script>"))), encoding="utf-8"
    )
    return directory


def test_history_retains_attempts_and_partial_usage(tmp_path: Path) -> None:
    saved_run(tmp_path, "a", list(CASE_IDS))
    saved_run(tmp_path, "b", list(CASE_IDS), "NOT_RUN")
    saved_run(tmp_path, "c", [CASE_IDS[0]])
    data = collect_history(tmp_path)
    assert len(data["runs"]) == 3
    assert [r["complete"] for r in data["runs"]] == [True, False, False]
    assert data["runs"][0]["metrics"]["input_tokens"] == 160
    assert data["runs"][0]["partial"]["input_tokens"] is True
    assert data["runs"][0]["metrics"]["output_tokens"] is None


def test_history_skips_bad_record_and_preserves_evidence(tmp_path: Path) -> None:
    good = saved_run(tmp_path, "good", list(CASE_IDS))
    original = (good / "summary.json").read_bytes()
    bad = saved_run(tmp_path, "bad", [CASE_IDS[0]])
    (bad / "summary.json").write_text("{broken", encoding="utf-8")
    assert len(collect_history(tmp_path)["warnings"]) == 1
    html = build_history(tmp_path).read_text(encoding="utf-8")
    assert "</script><script>alert(1)</script>" not in html
    assert "\\u003c/script>" in html
    assert (good / "summary.json").read_bytes() == original
    assert not list(tmp_path.glob("*.tmp"))


def test_empty_history_builds_offline_page(tmp_path: Path) -> None:
    assert collect_history(tmp_path)["runs"] == []
    assert build_history(tmp_path).is_file()


async def test_dashboard_failure_does_not_change_evaluation_result(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    from nexus.evaluation import history, runner
    from nexus.evaluation.environment import Docker, EnvironmentError
    from tests.test_conversation import config

    async def unavailable(cache: Path) -> None:
        raise EnvironmentError("test environment unavailable")

    def broken_report(home: Path) -> Path:
        raise OSError("test report write failure")

    monkeypatch.setattr(Docker, "connect", unavailable)
    monkeypatch.setattr(history, "build_history", broken_report)
    assert await runner.evaluate(list(CASE_IDS), config(monkeypatch), tmp_path) == 2
    assert len(list((tmp_path / "results").glob("*/summary.json"))) == 1
    assert "History report could not be updated" in capsys.readouterr().out
