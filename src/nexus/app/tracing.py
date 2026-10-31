"""Metadata-only JSONL projection and bounded, isolated OTLP/HTTP delivery."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from nexus.core.types import Json


@dataclass
class TraceConfig:
    enabled: bool = False
    endpoint: str = "https://api.smith.langchain.com/otel/v1/traces"
    api_key_env: str = "LANGSMITH_API_KEY"
    project: str = ""
    agent_id: str = ""
    environment: str = ""
    workspace_id: str = ""
    timeout_seconds: int = 10


def parse_trace(data: Json) -> TraceConfig:
    from nexus.app.config import ConfigError

    if not isinstance(data, dict) or data.keys() - TraceConfig.__dataclass_fields__.keys():
        raise ConfigError("tracing: unknown fields or invalid table")
    config = TraceConfig(**data)
    if type(config.enabled) is not bool:
        raise ConfigError("tracing.enabled: expected boolean")
    if type(config.timeout_seconds) is not int or not 1 <= config.timeout_seconds <= 60:
        raise ConfigError("tracing.timeout_seconds: expected integer 1..60")
    for key in ("endpoint", "api_key_env", "project", "agent_id", "environment", "workspace_id"):
        if not isinstance(getattr(config, key), str):
            raise ConfigError(f"tracing.{key}: expected string")
    url = urlsplit(config.endpoint)
    if (
        url.scheme not in {"http", "https"}
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
        or not url.path.endswith("/v1/traces")
    ):
        raise ConfigError("tracing.endpoint: expected full HTTP(S) traces endpoint, no credentials")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", config.api_key_env):
        raise ConfigError("tracing.api_key_env: invalid variable name")
    if bool(config.agent_id) != bool(config.environment) or (config.agent_id and config.project):
        raise ConfigError("tracing: use project OR agent_id with environment")
    if config.environment and config.environment not in {
        "local",
        "development",
        "staging",
        "production",
    }:
        raise ConfigError("tracing.environment: invalid environment")
    return config


def stable_id(value: str, size: int) -> int:
    return int.from_bytes(hashlib.sha256(value.encode()).digest()[:size], "big") or 1


def timestamp(row: Json) -> int:
    return int(datetime.fromisoformat(row["timestamp"]).timestamp() * 1_000_000_000)


def identifier(value: Any) -> str | None:
    # Never export free text, URLs, exceptions or user prompts in names/attributes.
    return value if isinstance(value, str) and re.fullmatch(r"[\w./:-]{1,160}", value) else None


def project_run(rows: list[Json], run_id: str) -> tuple[list[Json], list[str]]:
    rows = [r for r in rows if r.get("run_id") == run_id]
    diagnostics: list[str] = []
    spans: list[Json] = []
    starts: dict[str, Json] = {}
    requests: dict[str, list[Json]] = {}
    compact: Json | None = None
    root = next((r for r in rows if r["kind"] == "run_started"), None)
    end = next((r for r in reversed(rows) if r["kind"] == "run_finished"), None)
    if root is None or end is None:
        return [], ["incomplete_run: no fabricated end time"]
    # Idle maintenance after run_finished belongs to the session, not this run.
    rows = [r for r in rows if r["seq"] <= end["seq"]]
    session = str(root["session_id"])
    seed = session + ":" + run_id
    trace = stable_id(seed, 16)
    root_key = "run"

    def span(
        key: str,
        parent: str | None,
        name: str,
        first: Json,
        last: Json,
        attrs: Json,
        error: str | None = None,
    ) -> Json:
        if error:
            attrs["error.type"] = error
        value = {
            "name": name,
            "trace_id": trace,
            "span_id": stable_id(seed + key, 8),
            "parent_id": stable_id(seed + parent, 8) if parent else None,
            "start": timestamp(first),
            "end": timestamp(last),
            "attributes": {"nexus.session_id": session, "nexus.run_id": run_id, **attrs},
            "error": error,
        }
        spans.append(value)
        return value

    outcome = end["data"].get("outcome", "unknown")
    span(
        root_key,
        None,
        "nexus.run",
        root,
        end,
        {"langsmith.span.kind": "chain", "nexus.outcome": outcome},
        None if outcome == "completed" else "run_" + str(outcome),
    )
    last_request: str | None = None
    for row in rows:
        kind, d = row["kind"], row["data"]
        if kind == "run_started" and row is not root:
            diagnostics.append("resumed_run: prior unfinished operations not replayed")
            starts.clear()
            compact = None
            last_request = None
            spans[0]["attributes"]["nexus.resumed"] = True
        elif kind == "compaction_started":
            compact = row
        elif kind in {"context_compacted", "compaction_failed"} and compact:
            span(
                "compact:" + str(compact["seq"]),
                root_key,
                "nexus.compaction",
                compact,
                row,
                {"langsmith.span.kind": "chain", "nexus.purpose": "compaction"},
                "compaction_failed" if kind == "compaction_failed" else None,
            )
            compact = None
        elif kind == "model_started":
            request = str(d.get("request_id") or "legacy:" + str(row["seq"]))
            if not d.get("request_id"):
                diagnostics.append("legacy_model: logical request grouping unknown")
            starts["model"] = row
            requests.setdefault(request, [])
        elif kind == "model_finished" and "model" in starts:
            first = starts.pop("model")
            fd = first["data"]
            request = str(fd.get("request_id") or "legacy:" + str(first["seq"]))
            attrs: Json = {"langsmith.span.kind": "llm", "gen_ai.operation.name": "chat"}
            for source, target in {
                "model": "gen_ai.request.model",
                "response_model": "gen_ai.response.model",
                "provider": "gen_ai.provider.name",
                "purpose": "nexus.purpose",
                "request_id": "nexus.request_id",
            }.items():
                if value := identifier(d.get(source, fd.get(source))):
                    attrs[target] = value
            for field in ("attempt", "step"):
                if type(fd.get(field)) is int:
                    attrs["nexus." + field] = fd[field]
            usage = d.get("usage", {})
            for source, target in {
                "input_tokens": "input_tokens",
                "output_tokens": "output_tokens",
                "cached_input_tokens": "cache_read.input_tokens",
            }.items():
                if type(usage.get(source)) is int:
                    attrs["gen_ai.usage." + target] = usage[source]
            if "gen_ai.provider.name" in attrs:
                attrs["gen_ai.system"] = attrs["gen_ai.provider.name"]  # LangSmith compatibility
            error = identifier(d.get("error"))
            item = span(
                "attempt:" + str(first["seq"]),
                "request:" + request,
                "nexus.model.attempt",
                first,
                row,
                attrs,
                error,
            )
            item["request_parent"] = "compact:" + str(compact["seq"]) if compact else root_key
            requests[request].append(item)
            last_request = request
        elif kind == "tool_started":
            starts["tool:" + str(d["call_id"])] = {**row, "request": last_request}
        elif kind == "tool_finished":
            tool_first = starts.pop("tool:" + str(d["call_id"]), None)
            first = tool_first or {}
            if tool_first is None:
                diagnostics.append("tool_without_start: recovered or unknown; omitted")
                continue
            attrs = {"langsmith.span.kind": "tool", "gen_ai.operation.name": "execute_tool"}
            for value, key in (
                (first["data"].get("name"), "gen_ai.tool.name"),
                (d.get("call_id"), "gen_ai.tool.call.id"),
                (first.get("request"), "nexus.request_id"),
            ):
                if safe := identifier(value):
                    attrs[key] = safe
            if type(first["data"].get("step")) is int:
                attrs["nexus.step"] = first["data"]["step"]
            span(
                "tool:" + str(first["seq"]),
                root_key,
                "nexus.tool",
                first,
                row,
                attrs,
                None if d.get("ok") is True else identifier(d.get("error_code")) or "tool_failed",
            )
    for request, attempts in requests.items():
        if not attempts:
            continue
        a, b = attempts[0], attempts[-1]
        request_span = {
            **a,
            "name": "nexus.model.request",
            "span_id": stable_id(seed + "request:" + request, 8),
            "parent_id": stable_id(seed + a["request_parent"], 8),
            "end": b["end"],
            "error": b["error"],
            "attributes": {
                "nexus.session_id": session,
                "nexus.run_id": run_id,
                "nexus.request_id": request,
                "langsmith.span.kind": "chain",
            },
        }
        spans.append(request_span)
    if starts or compact:
        diagnostics.append("unfinished_operations: omitted")
    # Never export children referencing an unfinished/omitted compaction parent.
    while True:
        ids = {s["span_id"] for s in spans}
        complete = [s for s in spans if s["parent_id"] is None or s["parent_id"] in ids]
        if len(complete) == len(spans):
            break
        diagnostics.append("orphaned_operations: omitted")
        spans = complete
    return spans, diagnostics


def readable_spans(spans: list[Json]) -> list[Any]:
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import ReadableSpan
    from opentelemetry.trace import SpanContext, Status, StatusCode, TraceFlags

    def context(trace: int, span: int) -> SpanContext:
        return SpanContext(trace, span, False, TraceFlags(1))

    return [
        ReadableSpan(
            s["name"],
            context=context(s["trace_id"], s["span_id"]),
            parent=context(s["trace_id"], s["parent_id"]) if s["parent_id"] else None,
            resource=Resource({"service.name": "nexus"}),
            attributes=s["attributes"],
            start_time=s["start"],
            end_time=s["end"],
            status=Status(StatusCode.ERROR if s["error"] else StatusCode.OK),
        )
        for s in spans
    ]


async def export_rows(rows: list[Json], run_id: str, config: TraceConfig) -> tuple[bool, list[str]]:
    if not config.enabled:
        return True, []
    spans, diagnostics = project_run(rows, run_id)
    if not spans:
        return False, diagnostics
    if not os.environ.get(config.api_key_env):
        return False, [*diagnostics, "missing_api_key"]
    process = None
    try:
        # A killable worker bounds DNS, SDK retries and shutdown as well as HTTP.
        # Only allowlisted metadata crosses stdin; credentials stay in inherited env.
        async with asyncio.timeout(config.timeout_seconds + 2):
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "nexus.app.tracing",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await process.communicate(
                json.dumps({"config": asdict(config), "spans": spans}).encode()
            )
            return process.returncode == 0, diagnostics
    except (OSError, TimeoutError):
        return False, [*diagnostics, "export_failed_or_timeout"]
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()


async def export_file(path: Path, run_id: str, config: TraceConfig) -> tuple[bool, list[str]]:
    from nexus.app.session import read_records

    with path.open("rb") as stream:
        rows, damaged_tail = read_records(stream)
    ok, diagnostics = await export_rows(rows, run_id, config)
    if damaged_tail:
        diagnostics.append("damaged_tail: incomplete final record ignored")
    return ok, diagnostics


def worker() -> int:
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.trace.export import SpanExportResult

    payload = json.load(sys.stdin)
    config = parse_trace(payload["config"])
    headers = {"x-api-key": os.environ[config.api_key_env]}
    for key, value in (
        ("Langsmith-Project", config.project),
        ("Langsmith-Agent-Id", config.agent_id),
        ("Langsmith-Agent-Environment", config.environment),
        ("X-Tenant-Id", config.workspace_id),
    ):
        if value:
            headers[key] = value
    exporter = OTLPSpanExporter(
        endpoint=config.endpoint, headers=headers, timeout=config.timeout_seconds
    )
    try:
        return (
            0
            if exporter.export(readable_spans(payload["spans"])) == SpanExportResult.SUCCESS
            else 1
        )
    finally:
        exporter.shutdown()  # type: ignore[no-untyped-call]


if __name__ == "__main__":
    raise SystemExit(worker())
