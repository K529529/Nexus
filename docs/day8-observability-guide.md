# Nexus Day 8 Observability Guide

Status: Day 8 implementation guide. The approved contract is
`docs/spec-addenda/Nexus_Day8_Contract_Addendum_v0.3.md`.

## What Day 8 observes

Nexus now publishes safe structured runtime progress while the graph is running and can export
the same deny-by-default `TelemetryEvent` v1.0 records to optional tracers. Runtime, model, Tool,
approval, validation, repair, changed-file, interruption, terminal, and observability-warning
events carry UUID correlation and non-sensitive counters or enums. Prompts, completions, task
text, Tool arguments/results, file contents, diffs, command output, exception text, credentials,
and private reasoning are never exported.

`RuntimeEvent` is the internal business/progress event used by Runtime, graph, CLI, Tool Runtime,
and validation. `TelemetryEvent` is a separate versioned external/log schema built from an
event-specific allowlist. It is never an alias or unrestricted serialization of RuntimeEvent.

Each `run` or `resume` invocation creates a fresh execution segment. Its sink lifecycle is one
ordered FIFO:

```text
TraceStart -> TelemetryEvent* -> TraceFinish
```

Resume retains the durable `run_id`/`trace_id` and receives a new `execution_id` and sequence
starting at one.

## Local JSON Lines demo

The normal CLI renderer remains enabled and ConsoleTracer remains disabled by default. Enable the
additional safe JSON Lines stream through repository, user, or environment configuration:

```toml
[observability.console]
enabled = true
```

or in PowerShell:

```powershell
$env:NEXUS_CONSOLE_TRACING_ENABLED = "true"
uv run nexus chat "your task"
```

Console telemetry is written to stderr, one complete UTF-8 JSON object per line. It contains the
exact safe TelemetryEvent schema and no ANSI styling.

## LangSmith configuration

LangSmith is disabled by default. Remote configuration is trusted only from user-level
`~/.nexus/config.toml` and `NEXUS_LANGSMITH_*` environment variables. Repository configuration
containing `[observability.langsmith]` is rejected even when `enabled=false`. The API key is
accepted only from `NEXUS_LANGSMITH_API_KEY`; it is never accepted from TOML or CLI.

User-level TOML:

```toml
[observability.langsmith]
enabled = true
project = "nexus"
# endpoint = "https://api.smith.langchain.com" # optional
# workspace_id = "workspace-id"                # optional
```

PowerShell secret configuration:

```powershell
$env:NEXUS_LANGSMITH_API_KEY = "your-key"
```

Environment variables can also provide `NEXUS_LANGSMITH_TRACING_ENABLED`,
`NEXUS_LANGSMITH_PROJECT`, `NEXUS_LANGSMITH_ENDPOINT`, and
`NEXUS_LANGSMITH_WORKSPACE_ID`. Nexus uses a private explicitly configured client and does not set
global LangChain/LangSmith auto-tracing variables.

## Safe failure behavior

A tracer start, record, finish, flush, or queue overflow failure disables only that sink for the
current execution. The business Run, Tool order, validation result, and CLI success status are
unchanged. Warnings contain only a stable code, sink, operation, and fallback category. There are
no hidden Nexus retries.

## Verification

Default non-live gates:

```powershell
uv run ruff check .
uv run mypy src tests
uv run pytest --ignore=tests/local -m "not postgres and not mcp_e2e and not langsmith_e2e"
uv lock --check
uv build
```

The local `tests/local` calculator is a deliberately user-owned Day 4 fixture and is not part of
the repository regression gate.

Real LangSmith acceptance is never triggered merely because a key is present. It additionally
requires the explicit `NEXUS_RUN_LANGSMITH_E2E=1` operator opt-in, a disposable fixture with no
personal content, and the `langsmith_e2e` marker. Record the SDK version, sanitized project,
run/trace/execution IDs, UTC timestamp, and PASS/FAIL/NOT RUN here after an authorized live run.

Current real-provider result: **NOT RUN**. No external export was authorized during deterministic
implementation validation.

## Known V1 limitations

- Telemetry is not durably replayed after a process crash.
- Resume is represented by a new execution segment/root linked by the durable Nexus run/trace ID.
- No OpenTelemetry, OTLP, metrics backend, dashboard, alerting, or sampling platform is included.
- ConsoleTracer is stderr JSONL only; Day 8 adds no log-file lifecycle or trace-search CLI.
- Provider-reported token usage may be partial or unavailable and is never estimated.
