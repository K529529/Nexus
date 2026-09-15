# Nexus

[English](README.md) | 简体中文

Nexus 是一个面向真实仓库任务的透明、async-first Coding Agent Runtime，提供持久化 PostgreSQL
session/checkpoint、受策略治理的原生与 MCP 工具、有界仓库上下文、经过 Validation 的代码编辑、
仓库级/用户级 Skills、结构化 Observability，以及确定性的 Evaluation harness。LangGraph 和具体
provider 均封装在 Nexus 自有边界之后。

## 环境要求

- Python 3.12+
- Git
- 安装了 `vector` 扩展的 PostgreSQL 16
- 用于真实 coding task 的 OpenAI-compatible model deployment
- 使用仓库内置开发数据库时，需要 Docker 和 Compose

## 安装

直接从 PyPI 安装已发布版本：

```bash
pip install nexus-coding-agent
```

安装后验证 CLI：

```bash
nexus --help
```

PyPI distribution 名称是 `nexus-coding-agent`，安装后的 CLI 命令仍然是 `nexus`。

## 配置

Nexus 使用 PostgreSQL 保存持久化运行时状态。通过源码仓库开发时，内置 Compose 配置提供的开发数据库地址为：

```text
postgresql+asyncpg://nexus:nexus@localhost:5432/nexus
```

必要时可通过 `NEXUS_DATABASE_URL` 覆盖。`compose.yaml` 中的凭据仅供开发使用。

请通过环境变量配置 secret，切勿通过 CLI 参数或仓库 TOML 配置：

```powershell
$env:NEXUS_MODEL_NAME = "your-model"
$env:NEXUS_MODEL_API_KEY = "your-api-key"
$env:NEXUS_MODEL_BASE_URL = "https://your-compatible-endpoint.example/v1" # optional
$env:NEXUS_DATABASE_URL = "postgresql+asyncpg://nexus:nexus@localhost:5432/nexus"
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

```bash
nexus chat "Find the smallest cause of the failing test and fix it."
```

Nexus 会探索有界上下文、提出 Plan、在需要时请求 approval、依次执行受策略治理的 Tool call、
对变更执行 Validation，并呈现最终结果或如实报告失败/达到限制。

常用命令：

```bash
nexus --help
nexus chat --help
nexus index --help
nexus session list --help
nexus session resume --help
nexus eval --help
```

## 文档

[Nexus V1 指南](docs/nexus-v1-release-guide.md)包含架构概览与架构图、CLI/配置参考、Validation、
Observability 与 Evaluation 流程、FastAPI walkthrough、已知限制，以及 V1 后续 roadmap。

此外还提供面向具体子系统的 [MCP](docs/mcp-guide.md)、[Skills](docs/skill-guide.md)、
[Observability](docs/day8-observability-guide.md) 和[架构决策](docs/adr/)指南。

## 从源码开发

面向贡献者或本地开发：

```bash
git clone https://github.com/K529529/Nexus.git
cd Nexus
uv sync --locked --dev
docker compose up -d postgres
uv run alembic upgrade head
uv run nexus --help
```

运行确定性的开发检查：

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

## 发布版本

当前公开版本：`v0.1.0`

PyPI package：`nexus-coding-agent`

```bash
pip install nexus-coding-agent
```
