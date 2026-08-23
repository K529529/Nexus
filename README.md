# Nexus

Nexus is a transparent and extensible coding-agent runtime. This branch contains the
approved **Day 1 runtime skeleton** only: a Typer CLI, async `NexusRuntime`, typed events,
a one-node LangGraph adapter, an OpenAI-compatible model gateway, and PostgreSQL
connectivity bootstrap.

Day 1 does **not** understand repositories, search or edit code, create plans, call tools,
request approval, persist sessions, retrieve context, use MCP, validate changes, or act as
a complete coding agent.

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- Docker with Compose, only if using the optional development PostgreSQL service
- An OpenAI-compatible model name and API key for a live chat run

## Setup

Install the locked development environment:

```bash
uv sync --dev
```

Optionally start PostgreSQL 16 development infrastructure:

```bash
docker compose up -d postgres
```

The default URL is
`postgresql+asyncpg://nexus:nexus@localhost:5432/nexus`. Override it with
`NEXUS_DATABASE_URL`. The Compose credentials are development-only.

If port 5432 is already occupied, choose a development host port and keep the runtime URL
aligned. For example in PowerShell:

```powershell
$env:NEXUS_POSTGRES_PORT = "55432"
$env:NEXUS_DATABASE_URL = "postgresql+asyncpg://nexus:nexus@localhost:55432/nexus"
docker compose up -d postgres
```

Configure the model through environment variables. API keys are intentionally unsupported
in CLI arguments and TOML files:

```powershell
$env:NEXUS_MODEL_NAME = "your-model"
$env:NEXUS_MODEL_API_KEY = "your-api-key"
$env:NEXUS_MODEL_BASE_URL = "https://your-compatible-endpoint.example/v1" # optional
```

Non-secret repository configuration may instead be stored at `.nexus/config.toml`:

```toml
[model]
provider = "openai_compatible"
name = "your-model"
base_url = "https://your-compatible-endpoint.example/v1"

[database]
url = "postgresql+asyncpg://nexus:nexus@localhost:5432/nexus"
```

Configuration is resolved per field in this exact order:

```text
CLI > repository TOML > user ~/.nexus/config.toml > NEXUS_* environment > defaults
```

## Quick start

The root command displays help and exits successfully:

```bash
uv run nexus
```

Run the Day 1 acceptance path:

```bash
uv run nexus chat "Reply with a short greeting."
```

Expected shape:

```text
Task started
<model-generated greeting>
```

Model or configuration failures are rendered as safe structured errors and return a
non-zero exit code.

## Development checks

```bash
uv run ruff check .
uv run mypy src tests
uv run pytest
```

The PostgreSQL connectivity test skips only when an external service is genuinely
unavailable. Start the Compose service to exercise the real connection path locally.

See the [Day 1 architecture](docs/day1-architecture.md) and
[architecture decisions](docs/adr/) for boundary rationale.
