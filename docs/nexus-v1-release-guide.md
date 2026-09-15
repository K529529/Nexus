# Nexus V1 release guide

Status: Day 10 release-candidate guide for `Nexus v0.1.0`. The authoritative contracts are the
Frozen Baseline v1.1.1 and the approved Day 1-Day 10 addenda. This guide describes implemented V1
behavior; it does not authorize architecture or product changes.

## 1. README

The repository [README](../README.md) is the short installation and command entry point. This
guide is the complete release reference. Actual gate outcomes, including failures and external
dependencies, are recorded in [Day 10 release evidence](day10-release-evidence.md).

## 2. Architecture overview

Nexus is CLI-first and async-first. Typer is a thin adapter. `NexusRuntime` owns application
orchestration while Nexus domain ports isolate LangGraph, PostgreSQL/SQLAlchemy, model, embedding,
MCP, and tracer implementations. Concrete dependency assembly is confined to
`src/nexus/infrastructure/bootstrap/`.

Repository context is selected and bounded. The V1 graph explores, plans, waits for approval when
required, executes tools sequentially through policy/sandbox boundaries, validates modifications,
repairs or replans within configured limits, and terminalizes truthfully. Durable business state
and LangGraph checkpoints have separate Nexus-owned persistence boundaries. Safe runtime events
and allowlisted telemetry expose progress without prompts, file contents, command output,
credentials, or private reasoning.

The accepted architecture decisions are documented in [docs/adr](adr/).

## 3. Architecture diagram

```mermaid
flowchart TD
    CLI[Typer CLI] --> RT[NexusRuntime application boundary]
    RT --> GR[GraphRuntime port]
    GR --> LG[LangGraph adapter]
    LG --> CTX[Bounded context and Skills]
    LG --> PLAN[Plan and approval]
    LG --> TR[Tool Runtime]
    TR --> POL[Policy and workspace sandbox]
    TR --> NAT[Native tools]
    TR --> MCP[MCP client tools]
    LG --> VAL[Validation service]
    RT --> SES[Session and Run services]
    SES --> PG[(PostgreSQL business state)]
    LG --> CP[(PostgreSQL checkpoints)]
    RT --> OBS[Safe event and telemetry boundary]
    OBS --> CON[Console JSONL]
    OBS --> LS[Optional LangSmith]
    BOOT[Infrastructure composition root] -. assembles .-> RT
```

Domain code does not depend on Typer, LangGraph, SQLAlchemy, MCP SDKs, or concrete model vendors.
Every side effect remains behind Tool Runtime, policy, approval, and workspace containment.

## 4. Installation

Prerequisites are Python 3.12+, `uv`, Git, PostgreSQL 16 with `vector`, and an OpenAI-compatible
model deployment for live tasks. The included Compose service supplies the development database.

Repository development install:

```powershell
uv sync --locked --dev
docker compose up -d postgres
uv run alembic upgrade head
uv run nexus --help
```

Release fresh-install verification must not reuse the repository `.venv` or a stale wheel:

```powershell
uv build
$fresh = Join-Path $env:TEMP "nexus-v010-fresh"
New-Item -ItemType Directory -Path $fresh
uv venv (Join-Path $fresh ".venv")
uv pip install --python (Join-Path $fresh ".venv/Scripts/python.exe") `
  (Get-ChildItem dist/nexus_agent-0.1.0-py3-none-any.whl)
& (Join-Path $fresh ".venv/Scripts/nexus.exe") --help
```

Supply non-secret configuration as described below and secrets only through environment
variables. Run migrations with the installed package's `alembic` command from a source copy that
contains `alembic.ini` and `migrations/`; the wheel intentionally packages runtime code, not the
repository's operator-owned migration workspace. The exact executed fresh-install evidence is in
the evidence document.

## 5. Quick start

From the repository Nexus may operate on:

```powershell
$env:NEXUS_MODEL_NAME = "your-model"
$env:NEXUS_MODEL_API_KEY = "your-api-key"
$env:NEXUS_DATABASE_URL = "postgresql+asyncpg://nexus:nexus@localhost:5432/nexus"
uv run nexus chat "Explain this repository's validation flow. Do not change files."
```

For a coding task, review the rendered Plan and enter the requested approval decision. A normal
successful change ends with validation evidence and a final result. A denied operation, model
failure, validation failure, or configured limit produces a non-success terminal event and a
non-zero CLI exit where applicable.

## 6. CLI reference

| Command | Implemented behavior |
| --- | --- |
| `nexus` / `nexus --help` | Show root help and exit successfully. |
| `nexus chat TASK [--model NAME] [--base-url URL]` | Run one task through the normal V1 runtime and interactive Plan approval flow. |
| `nexus index [--rebuild]` | Incrementally index the current canonical repository, or rebuild its compatible index. |
| `nexus session list` | List durable sessions belonging to the current repository identity. |
| `nexus session resume SESSION_ID` | Resume the latest interrupted run for that repository-owned session. |
| `nexus eval [--case EVAL-NNN]` | Run all six Day 9 cases sequentially or one unchanged mandatory case. |

Run `<command> --help` for exact arguments. Model API keys are not CLI options. Nexus Agent does
not expose Git commit, push, or history-rewrite commands.

## 7. Configuration

Configuration resolves each field in this order:

```text
CLI > repository .nexus/config.toml > user ~/.nexus/config.toml > NEXUS_* environment > defaults
```

Repository/user TOML supports `[model]`, `[database]`, `[runtime]`, `[context]`, `[retrieval]`,
`[embedding]`, `[skills]`, and `[observability.console]`. User TOML additionally owns `[mcp]` and
`[observability.langsmith]`; repository definitions of those trusted process/export surfaces fail
closed. See the focused guides for their exact schemas.

Secrets are accepted only from their environment variables:

- `NEXUS_MODEL_API_KEY`
- `NEXUS_EMBEDDING_API_KEY`
- `NEXUS_LANGSMITH_API_KEY`

Common non-secret environment fields include `NEXUS_MODEL_NAME`, `NEXUS_MODEL_BASE_URL`,
`NEXUS_DATABASE_URL`, runtime limits, semantic/embedding identity, selected-Skill limit, and
console/LangSmith tracing switches. Secret values must not be committed, echoed into evidence, or
placed in CLI history.

## 8. MCP guide

The authoritative [MCP guide](mcp-guide.md) covers user-controlled stdio servers, exact tool
naming, risk classification, environment inheritance, retry/timeout behavior, normalization, and
the pinned real-server acceptance. V1 is an MCP client/host only. MCP WRITE is not authorized and
HTTP/SSE/OAuth are not implemented.

## 9. Skill guide

The authoritative [Skill guide](skill-guide.md) defines strict TOML front matter, source
precedence, progressive disclosure, selection limits, atomic body budgeting, and security
authority. Skills are untrusted Markdown guidance; they cannot execute hooks, register tools,
grant permission, or weaken validation/sandboxing.

## 10. Validation guide

Every code modification must be validated through the existing validation boundary. The Plan's
approved validation commands, executable allowlist, command policy, timeout, and workspace
containment remain authoritative. The graph may repair or replan only within configured bounds;
exhaustion terminalizes as a stop/failure rather than success.

Release-quality local verification:

```powershell
uv run ruff check .
uv run mypy src tests
uv run pytest --cov=nexus --cov-report=term-missing --cov-report=json:coverage.json
uv run coverage report --precision=2 --fail-under=70
uv run coverage report --include="src/nexus/application/runtime.py" --precision=2 --fail-under=80
uv run coverage report --include="src/nexus/security/*" --precision=2 --fail-under=80
uv run coverage report --include="src/nexus/application/validation.py" --precision=2 --fail-under=80
uv lock --check
uv build
```

Coverage is statement coverage. `.coverage` from the single canonical pytest run is authoritative;
all reports use two-decimal display and Coverage.py's inclusive `--fail-under` decision.

## 11. Observability guide

The [observability guide](day8-observability-guide.md) defines safe event fields, execution/run/
session/trace correlation, Console JSONL, optional LangSmith export, redaction, and non-fatal
tracer failure. Day 10 release evidence records the real LangSmith acceptance separately from
deterministic tracer tests. Private chain-of-thought is never a telemetry or evidence field.

## 12. Evaluation guide

The mandatory suite is `EVAL-001` through `EVAL-006` under `evals/cases`. Objectives and
deterministic success conditions are frozen. Run it without changing those cases:

```powershell
uv run nexus eval
uv run nexus eval --case EVAL-001
```

Reports are written under `evals/reports`; the stable baseline remains
`evals/reports/baseline-v1.json`. Evidence distinguishes `PASS`, `TASK_FAILED`,
`INFRASTRUCTURE_ERROR`, and `EVALUATOR_ERROR`, and preserves the frozen metrics:
`agent_steps`, `llm_calls`, `tool_calls`, `replans`, `repairs`, latency, and token usage. A failed
case is never relabeled or weakened to produce a release pass.

## 13. Canonical FastAPI task walkthrough

The dedicated [FastAPI fixture](../examples/day10-fastapi-fixture/README.md) supports bug fix,
feature modification, behavior-preserving refactor, missing targeted test, and repository
explanation tasks. Each run starts from a disposable copy and initial Git commit.

The release walkthrough uses the premium-discount bug:

```text
task
  Fix the premium large-order discount with the smallest change; run the focused/full tests.
selected repository/context
  disposable canonical fixture; app/main.py and tests/test_app.py
plan
  reproduce failure -> correct discount -> run tests -> inspect diff
approval
  operator reviews and approves the rendered Plan/change authorization
tool-driven change
  Nexus read/search/apply_patch/run_command paths only
validation
  uv run pytest; git diff --check
git diff
  expected one-line production correction (5 -> 15), no unsafe Git operation
final result
  truthful summary with validation status
trace/run correlation
  matching run_id/session_id/trace_id and a fresh execution_id
```

The evidence document records the actual run outcome; this expected walkthrough is not itself a
PASS claim.

## 14. Known limitations

Nexus V1 does not provide:

- a VSCode extension;
- a Web UI;
- Multi-Agent orchestration;
- a Docker or remote sandbox;
- language-specific AST/LSP refactoring intelligence;
- Agent Git commit, push, or history rewrite;
- complete long-term memory;
- automatic real-time repository indexing.

V1 is CLI-first, uses local-process workspace containment rather than an OS/container security
boundary, supports MCP stdio only, executes tools sequentially by default, and requires an
operator-provided PostgreSQL/model environment for live tasks.

## 15. Post-V1 roadmap

Possible post-V1 directions include VSCode/API/Web adapters, Multi-Agent orchestration,
Docker/remote sandboxing, richer code intelligence, alternate semantic stores, OpenTelemetry, and
expanded model/tool integrations. They are not Day 10 requirements and are not promised by
`v0.1.0`.

## 16. Release checklist

- [ ] Frozen architecture remains conformant.
- [ ] CI is green.
- [ ] Required deterministic and live suites pass.
- [ ] All four coverage thresholds pass from one canonical data file.
- [ ] Fresh install, fresh migration, and index compatibility pass.
- [ ] Canonical fixture walkthrough and real-repository smoke pass.
- [ ] Workspace, dangerous Git/command, secret, MCP-unavailable, and limit-stop checks pass.
- [ ] MCP and LangSmith V1 acceptance remain valid against the release candidate.
- [ ] Day 9 baseline exists and all six current outcomes are reported truthfully.
- [ ] Documentation matches implementation; no V1-excluded feature was introduced.
- [ ] Product Owner Knowledge Review passes.
- [ ] After merge to `main`, the Product Owner authorizes creation/push of tag `v0.1.0`.

A mandatory `FAIL` or `NOT RUN` means the release candidate is not accepted.

## 17. Release notes — v0.1.0

`v0.1.0` is the planned first Nexus V1 release candidate: a transparent coding-agent runtime integrating
bounded repository exploration, Plan approval, policy-governed native/MCP tools, editing and
validation/repair, durable sessions and checkpoints, hybrid retrieval/indexing, Skills, safe
observability, and the six-case evaluation harness.

Day 10 adds release hardening rather than new product scope: coverage enforcement, regression
coverage for the stable runtime seam, correct LangSmith batch trace hierarchy metadata, a
canonical FastAPI demo fixture, corrected CLI/help text, reproducible release documentation, and
a reviewable evidence bundle. The final `v0.1.0` tag is intentionally deferred until Product Owner
review and merge to `main`.
