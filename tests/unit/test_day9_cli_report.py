from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from nexus.evaluation.models import EvalSuiteReport
from nexus.evaluation.report import write_report
from nexus.interfaces.cli.app import app

runner = CliRunner()


def test_eval_cli_help_and_unknown_case() -> None:
    help_result = runner.invoke(app, ["eval", "--help"], color=False)
    unknown = runner.invoke(app, ["eval", "--case", "EVAL-999"], color=False)

    assert help_result.exit_code == 0
    assert "--case" in help_result.stdout
    assert unknown.exit_code == 1
    assert "Unknown evaluation case: EVAL-999" in unknown.stderr


def test_report_writes_timestamp_and_never_silently_overwrites_baseline(
    tmp_path: Path,
) -> None:
    first = EvalSuiteReport("1", datetime.now(UTC), (), (), 0, 0, 0, 0, 0)
    timestamped, baseline = write_report(first, tmp_path, write_baseline=True)
    assert timestamped.is_file()
    assert baseline == tmp_path / "baseline-v1.json"
    original = baseline.read_text(encoding="utf-8")

    second = EvalSuiteReport("1", datetime.now(UTC), (), (), 0, 0, 0, 0, 0)
    _, second_baseline = write_report(second, tmp_path, write_baseline=True)

    assert second_baseline is None
    assert baseline.read_text(encoding="utf-8") == original
