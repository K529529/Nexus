# Nexus

English | [简体中文](README.zh-CN.md)

Nexus is a transparent, async-first coding-agent runtime with a CLI adapter, durable PostgreSQL
sessions/checkpoints, policy-governed native and MCP tools, bounded repository context, validated
code editing, repository/user Skills, safe structured telemetry, and a deterministic evaluation
harness. LangGraph and concrete providers remain behind Nexus-owned boundaries.

This branch prepares the `Nexus v0.1.0 Release Candidate`. It does not create the final Git tag;
tagging remains a Product Owner action after review and merge to `main`.

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- Git
- PostgreSQL 16 with the `vector` extension
- an OpenAI-compatible model deployment for live coding tasks
- Docker with Compose when using the included development database

## Install

```powershell
uv sync --locked --dev
docker compose up -d postgres
uv run alembic upgrade head
uv run nexus --help
```

The Compose development URL is
`postgresql+asyncpg://nexus:nexus@localhost:5432/nexus`. Override it with
`NEXUS_DATABASE_URL` when necessary. Credentials in `compose.yaml` are development-only.

Configure secrets through environment variables, never CLI arguments or repository TOML:

```powershell
$env:NEXUS_MODEL_NAME = "your-model"
$env:NEXUS_MODEL_API_KEY = "your-api-key"
$env:NEXUS_MODEL_BASE_URL = "https://your-compatible-endpoint.example/v1" # optional
```

Non-secret repository configuration can be stored at `.nexus/config.toml`:

```toml
[model]
provider = "openai_compatible"
name = "your-model"
base_url = "https://your-compatible-endpoint.example/v1"

[database]
url = "postgresql+asyncpg://nexus:nexus@localhost:5432/nexus"

[runtime]
approval_mode = "approval"
max_steps = 20
max_repair_attempts = 3
max_replans = 2
```

Configuration precedence is resolved per field:

```text
CLI > repository TOML > user ~/.nexus/config.toml > NEXUS_* environment > defaults
```

MCP process configuration and LangSmith configuration are trusted only from user-level TOML or
approved environment variables; API keys are environment-only.

## Quick start

Run Nexus from the repository that it is allowed to inspect and change:

```powershell
uv run nexus chat "Find the smallest cause of the failing test and fix it."
```

Nexus explores bounded context, proposes a Plan, asks for approval when required, performs
policy-governed sequential Tool calls, validates changes, and renders a final result or a truthful
failure/limit stop. Useful commands are:

```powershell
uv run nexus --help
uv run nexus chat --help
uv run nexus index --help
uv run nexus session list --help
uv run nexus session resume --help
uv run nexus eval --help
```

## Release documentation

The [Nexus V1 release guide](docs/nexus-v1-release-guide.md) is the authoritative Day 10 guide. It
contains the architecture overview and diagram, complete CLI/configuration reference, validation,
observability and evaluation procedures, canonical FastAPI walkthrough, known limitations,
post-V1 roadmap, release checklist, and `v0.1.0` release notes.

Focused subsystem guides remain available for [MCP](docs/mcp-guide.md),
[Skills](docs/skill-guide.md), [observability](docs/day8-observability-guide.md), and the
[architecture decisions](docs/adr/).

## Development and release gates

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

Live provider, LangSmith, evaluation, fresh-install, fixture, and real-repository results are kept
separate from deterministic regression evidence in the
[Day 10 release evidence](docs/day10-release-evidence.md).
