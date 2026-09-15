# Nexus

English | [简体中文](README.zh-CN.md)

Nexus is a transparent, async-first coding-agent runtime for real repository tasks, with durable
PostgreSQL sessions and checkpoints, policy-governed native and MCP tools, bounded repository
context, validated code editing, repository/user Skills, structured observability, and a
deterministic evaluation harness. LangGraph and concrete providers remain behind Nexus-owned
boundaries.

## Requirements

- Python 3.12+
- Git
- PostgreSQL 16 with the `vector` extension
- an OpenAI-compatible model deployment for live coding tasks
- Docker with Compose when using the included development database

## Install

Install the published package from PyPI:

```bash
pip install nexus-coding-agent
```

Then verify the CLI:

```bash
nexus --help
```

The PyPI distribution name is `nexus-coding-agent`; the installed CLI command is `nexus`.

## Configuration

Nexus requires PostgreSQL for persistent runtime state. When working from the source repository,
the included Compose setup provides a development database at:

```text
postgresql+asyncpg://nexus:nexus@localhost:5432/nexus
```

Override it with `NEXUS_DATABASE_URL` when necessary. Credentials in `compose.yaml` are
development-only.

Configure secrets through environment variables, never CLI arguments or repository TOML:

```powershell
$env:NEXUS_MODEL_NAME = "your-model"
$env:NEXUS_MODEL_API_KEY = "your-api-key"
$env:NEXUS_MODEL_BASE_URL = "https://your-compatible-endpoint.example/v1" # optional
$env:NEXUS_DATABASE_URL = "postgresql+asyncpg://nexus:nexus@localhost:5432/nexus"
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

```bash
nexus chat "Find the smallest cause of the failing test and fix it."
```

Nexus explores bounded context, proposes a Plan, asks for approval when required, performs
policy-governed sequential Tool calls, validates changes, and renders a final result or a truthful
failure/limit stop.

Useful commands:

```bash
nexus --help
nexus chat --help
nexus index --help
nexus session list --help
nexus session resume --help
nexus eval --help
```

## Documentation

The [Nexus V1 guide](docs/nexus-v1-release-guide.md) contains the architecture overview and diagram,
CLI/configuration reference, validation, observability and evaluation procedures, a FastAPI
walkthrough, known limitations, and the post-V1 roadmap.

Focused subsystem guides are available for [MCP](docs/mcp-guide.md),
[Skills](docs/skill-guide.md), [observability](docs/day8-observability-guide.md), and
[architecture decisions](docs/adr/).

## Development from source

For contributors or local development:

```bash
git clone https://github.com/K529529/Nexus.git
cd Nexus
uv sync --locked --dev
docker compose up -d postgres
uv run alembic upgrade head
uv run nexus --help
```

Run the deterministic development checks with:

```bash
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

## Release

Current public release: `v0.1.0`

PyPI package: `nexus-coding-agent`

```bash
pip install nexus-coding-agent
```
