# Nexus

Nexus is a transparent and extensible coding-agent runtime. Through **Day 3**, it includes
the Day 1 model-backed runtime plus durable Repository/Session/Run/SessionTurn business
state, official PostgreSQL-backed LangGraph checkpoints, session list/resume commands,
and a policy-governed native Tool Runtime for bounded workspace inspection.

Day 3 native Tools can list/search/read files and perform fixed read-only Git inspection.
They are not yet wired into the Agent graph. Day 3 does **not** edit code, execute WRITE
operations, create approved Plans, use MCP, or act as a complete coding agent.

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
uv run alembic upgrade head
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
[Day 2 session guide](docs/day2-session-cli.md), the
[Day 3 Tool Runtime and security guide](docs/day3-native-tool-runtime-security.md), plus the
[Day 8 observability guide](docs/day8-observability-guide.md), plus the
[architecture decisions](docs/adr/), for boundary rationale and durable resume evidence.
