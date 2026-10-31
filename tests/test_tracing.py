from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from nexus.app.config import ConfigError
from nexus.app.tracing import TraceConfig, export_rows, parse_trace, project_run, readable_spans
from nexus.core.types import Json


def records() -> list[Json]:
    events: list[tuple[str, Json]] = [
        ("run_started", {"workspace": "SECRET"}),
        ("model_started", {"request_id": "r1", "attempt": 1, "model": "test", "step": 1}),
        ("model_finished", {"error": "model_http_429", "retry": True}),
        ("model_started", {"request_id": "r1", "attempt": 2, "model": "test", "step": 1}),
        ("model_finished", {"usage": {"input_tokens": 10, "output_tokens": 2}}),
        ("tool_started", {"call_id": "c1", "name": "exec_command", "arguments_json": "SECRET"}),
        ("tool_finished", {"call_id": "c1", "ok": False, "error_code": "nonzero_exit"}),
        ("compaction_started", {}),
        ("model_started", {"request_id": "r2", "attempt": 1, "purpose": "compaction"}),
        ("model_finished", {}),
        ("context_compacted", {"summary": "SECRET"}),
        ("run_finished", {"outcome": "completed", "final_text": "SECRET"}),
    ]
    return [
        {
            "seq": i + 1,
            "kind": k,
            "data": d,
            "session_id": "s",
            "run_id": "run",
            "timestamp": f"2026-10-31T00:00:{i:02d}+00:00",
        }
        for i, (k, d) in enumerate(events)
    ]


def test_hierarchy_retry_usage_and_privacy() -> None:
    spans, diagnostics = project_run(records(), "run")
    assert diagnostics == []
    assert len(spans) == 8
    root = spans[0]
    attempts = [s for s in spans if s["name"] == "nexus.model.attempt"]
    requests = [s for s in spans if s["name"] == "nexus.model.request"]
    assert attempts[0]["error"] == "model_http_429"
    assert requests[0]["error"] is None and root["error"] is None
    assert attempts[0]["parent_id"] == attempts[1]["parent_id"] == requests[0]["span_id"]
    assert sum(s["attributes"].get("gen_ai.usage.input_tokens", 0) for s in spans) == 10
    compact = next(s for s in spans if s["name"] == "nexus.compaction")
    assert requests[1]["parent_id"] == compact["span_id"]
    tool = next(s for s in spans if s["name"] == "nexus.tool")
    assert tool["parent_id"] == root["span_id"]
    assert tool["attributes"]["nexus.request_id"] == "r1"
    assert "SECRET" not in json.dumps(spans)
    assert root["end"] - root["start"] == 11_000_000_000
    assert len(readable_spans(spans)) == len(spans)


def test_stable_ids_and_run_isolation() -> None:
    rows = records()
    other = [{**r, "run_id": "other"} for r in rows]
    assert project_run(rows + other, "run") == project_run(rows, "run")
    a = project_run(rows, "run")[0]
    b = project_run(other, "other")[0]
    assert a[0]["trace_id"] != b[0]["trace_id"]


@pytest.mark.parametrize("outcome", ["aborted", "failed", "interrupted_unknown"])
def test_cancel_and_failure(outcome: str) -> None:
    rows = records()
    rows[-1]["data"]["outcome"] = outcome
    assert project_run(rows, "run")[0][0]["error"] == "run_" + outcome


def test_old_logs_missing_end_and_recovered_tool() -> None:
    rows = records()
    assert not project_run(rows[:-1], "run")[0]
    for r in rows:
        r["data"].pop("request_id", None)
    rows = [r for r in rows if r["kind"] != "tool_started"]
    spans, diagnostics = project_run(rows, "run")
    assert any("legacy_model" in d for d in diagnostics)
    assert any("recovered" in d for d in diagnostics)
    assert not any(s["name"] == "nexus.tool" for s in spans)


async def test_disabled_no_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("must not initialize transport")

    monkeypatch.setattr("asyncio.create_subprocess_exec", forbidden)
    assert await export_rows(records(), "run", TraceConfig()) == (True, [])


async def test_missing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NEXUS_TRACE_TEST_KEY", raising=False)
    ok, diagnostics = await export_rows(
        records(), "run", TraceConfig(True, api_key_env="NEXUS_TRACE_TEST_KEY")
    )
    assert not ok and "missing_api_key" in diagnostics


async def test_unreachable_export_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    monkeypatch.setenv("NEXUS_TRACE_TEST_KEY", "test-key")
    config = TraceConfig(
        True, "http://127.0.0.1:1/v1/traces", "NEXUS_TRACE_TEST_KEY", timeout_seconds=1
    )
    start = time.monotonic()
    ok, _ = await export_rows(records(), "run", config)
    assert not ok
    assert time.monotonic() - start < 5


def test_config_validation() -> None:
    assert parse_trace({}) == TraceConfig()
    for data in (
        {"enabled": "yes"},
        {"timeout_seconds": 0},
        {"api_key_env": "BAD KEY"},
        {"endpoint": "https://example.com/otel"},
        {"agent_id": "a"},
        {"project": "p", "agent_id": "a", "environment": "local"},
    ):
        with pytest.raises(ConfigError):
            parse_trace(data)


async def test_file_export_tail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus.app.tracing import export_file

    rows = [{"seq": 0, "kind": "session_created", "data": {}, "session_id": "s"}, *records()]
    for i, row in enumerate(rows):
        row.update(schema_version=1, seq=i + 1)
    path = tmp_path / "session.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows) + '{"broken"', encoding="utf-8")
    captured: list[Json] = []

    async def capture(
        values: list[Json], run_id: str, config: TraceConfig
    ) -> tuple[bool, list[str]]:
        captured.extend(project_run(values, run_id)[0])
        return True, []

    monkeypatch.setattr("nexus.app.tracing.export_rows", capture)
    ok, diagnostics = await export_file(path, "run", replace(TraceConfig(), enabled=True))
    assert ok and "damaged_tail" in diagnostics[0]
    assert captured == project_run(rows, "run")[0]


def test_resume_unknown_tool_is_not_an_execution() -> None:
    rows = records()[:6]  # crash after tool_started
    extra = [
        ("run_started", {}),
        ("tool_finished", {"call_id": "c1", "ok": True}),
        ("run_finished", {"outcome": "completed"}),
    ]
    for i, (kind, data) in enumerate(extra, 6):
        rows.append(
            {
                "seq": i + 1,
                "kind": kind,
                "data": data,
                "session_id": "s",
                "run_id": "run",
                "timestamp": f"2026-10-31T00:00:{i:02d}+00:00",
            }
        )
    spans, diagnostics = project_run(rows, "run")
    assert spans[0]["attributes"]["nexus.resumed"] is True
    assert len([s for s in spans if s["name"] == "nexus.model.attempt"]) == 2
    assert not any(s["name"] == "nexus.tool" for s in spans)
    assert any("recovered" in d for d in diagnostics)


async def test_401_and_sensitive_response_not_logged(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b"SECRET-SERVER-RESPONSE")

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    monkeypatch.setenv("NEXUS_TRACE_TEST_KEY", "SECRET-KEY")
    try:
        config = TraceConfig(
            True,
            f"http://127.0.0.1:{server.server_port}/v1/traces",
            "NEXUS_TRACE_TEST_KEY",
            timeout_seconds=1,
        )
        ok, _ = await export_rows(records(), "run", config)
        assert not ok
        captured = capsys.readouterr()
        assert "SECRET" not in captured.out + captured.err
    finally:
        server.shutdown()
        thread.join(2)
        server.server_close()


async def test_export_failure_preserves_durable_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nexus.app.events import Events
    from nexus.app.session import SessionLog, read_records
    from nexus.core.types import RuntimeEvent, Session

    session = Session(tmp_path)
    session.run_id = "run"
    writer = SessionLog.create(session, "test", {}, tmp_path / "state")

    async def consume(event: RuntimeEvent) -> None:
        pass

    async def fail(*args: object) -> tuple[bool, list[str]]:
        raise RuntimeError("SECRET")

    monkeypatch.setattr("nexus.app.events.export_rows", fail)
    events = Events(session, writer, consume, tracing=TraceConfig(True))
    try:
        await events("run_started", {})
        await events("run_finished", {"outcome": "completed"})
        with writer.path.open("rb") as stream:
            saved, _ = read_records(stream)
        assert saved[-1]["data"]["outcome"] == "completed"
    finally:
        writer.close()
