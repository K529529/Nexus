# Nexus

[English](README.md) | 简体中文

Nexus 是一个透明、async-first 的 coding-agent runtime，提供 CLI adapter、持久化 PostgreSQL
session/checkpoint、受策略治理的原生与 MCP 工具、有界仓库上下文、经过 Validation 的代码编辑、
仓库级/用户级 Skills、安全的结构化 telemetry，以及确定性的 Evaluation harness。LangGraph
和具体 provider 均封装在 Nexus 自有边界之后。

此分支用于准备 `Nexus v0.1.0 Release Candidate`，不会创建最终 Git tag；tag 操作仍由
Product Owner 在审查并 merge 到 `main` 后执行。

## 环境要求

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- Git
- 安装了 `vector` 扩展的 PostgreSQL 16
- 用于真实 coding task 的 OpenAI-compatible model deployment
- 使用仓库内置开发数据库时，需要 Docker 和 Compose

## 安装

```powershell
uv sync --locked --dev
docker compose up -d postgres
uv run alembic upgrade head
uv run nexus --help
```

Compose 开发环境的 URL 为
`postgresql+asyncpg://nexus:nexus@localhost:5432/nexus`。必要时可通过
`NEXUS_DATABASE_URL` 覆盖。`compose.yaml` 中的凭据仅供开发使用。

请通过环境变量配置 secret，切勿通过 CLI 参数或仓库 TOML 配置：

```powershell
$env:NEXUS_MODEL_NAME = "your-model"
$env:NEXUS_MODEL_API_KEY = "your-api-key"
$env:NEXUS_MODEL_BASE_URL = "https://your-compatible-endpoint.example/v1" # optional
```

非敏感的仓库配置可保存在 `.nexus/config.toml`：

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

配置优先级按字段解析：

```text
CLI > repository TOML > user ~/.nexus/config.toml > NEXUS_* environment > defaults
```

MCP 进程配置和 LangSmith 配置仅信任用户级 TOML 或已批准的环境变量；API key 只能通过环境变量提供。

## 快速开始

请在允许 Nexus 检查和修改的仓库中运行：

```powershell
uv run nexus chat "Find the smallest cause of the failing test and fix it."
```

Nexus 会探索有界上下文、提出 Plan、在需要时请求 approval、依次执行受策略治理的 Tool call、
对变更执行 Validation，并呈现最终结果或如实报告失败/达到限制。常用命令如下：

```powershell
uv run nexus --help
uv run nexus chat --help
uv run nexus index --help
uv run nexus session list --help
uv run nexus session resume --help
uv run nexus eval --help
```

## 发布文档

[Nexus V1 发布指南](docs/nexus-v1-release-guide.md)是权威的 Day 10 指南，其中包含架构概览与
架构图、完整的 CLI/配置参考、Validation、Observability 与 Evaluation 流程、标准 FastAPI
walkthrough、已知限制、V1 后续 roadmap、release checklist，以及 `v0.1.0` release notes。

此外还提供面向具体子系统的 [MCP](docs/mcp-guide.md)、[Skills](docs/skill-guide.md)、
[Observability](docs/day8-observability-guide.md) 和[架构决策](docs/adr/)指南。

## 开发与发布门禁

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

真实 provider、LangSmith、Evaluation、fresh install、fixture 和真实仓库的结果，与确定性
regression evidence 分开记录在
[Day 10 发布证据](docs/day10-release-evidence.md)中。
