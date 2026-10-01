# Nexus Next V0

[English / 完整配置与评测命令](README.md)

一个小型本地 Coding Agent：单个手写消息循环，仅有 `exec_command` 与
`apply_patch` 两个原生工具。模型负责探索、规划、修改、运行检查、修复和决定结束。
无需数据库、仓库索引或工作流引擎；用户显式配置的 stdio MCP server 可提供额外工具。

当前是 `0.2.0` 本地候选实现，未发布 PyPI。实际交付状态见
[验收证据](docs/next/acceptance-evidence.md)，模型返回 completed 不等于测试通过或
SWE-bench resolved。

## 安装与配置

要求 Python 3.12+；支持 Windows PowerShell 与 Linux `/bin/sh`。

```sh
uv build
python -m pip install dist/nexus_coding_agent-0.2.0-py3-none-any.whl
# 或
uv tool install dist/nexus_coding_agent-0.2.0-py3-none-any.whl
```

开发环境使用 `uv sync --frozen --dev`，通过 `uv run --frozen nexus` 启动。
先进入目标项目根目录：启动目录就是 workspace，不自动向上寻找 Git root。
启动只读取当前目录的 `AGENTS.md`，不会扫描源码、加载嵌套指令或建立索引。

首次在真实终端运行 `nexus` 可进入配置引导，也可手动创建 `~/.nexus/config.toml`：

```toml
[model]
name = "deepseek-v4.1-flash"
base_url = "https://<你的百炼账户endpoint>/compatible-mode/v1"
api_key_env = "DASHSCOPE_API_KEY"
context_window = 1000000
max_output_tokens = 32768
reasoning_effort = "high"
include_usage = true

[runtime]
max_steps = 40

[execution]
output_limit_bytes = 32768
```

Base URL 必须替换成账户对应的真实地址。API key 只从指定环境变量读取：

```powershell
$env:DASHSCOPE_API_KEY = '<key>'
nexus
```

Linux 使用 `export DASHSCOPE_API_KEY='<key>'`。引导只询问变量名，不收集密钥。
旧配置中的 `observability` 等段落不再支持；先备份旧配置再移除淘汰字段，程序不会
自动改写旧配置。不会读取仓库 `.env` 或 `.nexus/config.toml`。

## 使用与恢复

```sh
nexus
nexus exec "修复空输入缺陷并运行已有测试"
nexus exec "分析测试失败" --json
nexus resume
```

交互命令：`/new`、`/resume`、`/help`、`/exit`。恢复列表展示任务标题、项目、更新时间
和状态；上下键选择，Enter 确认，Esc 取消。运行中 Ctrl+C 取消当前轮次并回收进程，
空闲时清空输入。无终端时应使用 `exec`，不会等待不可见输入。

默认终端不再逐轮打印 generating，普通成功工具只留一行操作摘要、状态和耗时；支持的
终端在工具运行期间显示临时活动行。Read/Search/Test/Git 分类只影响显示，复杂或过长命令
显示为 Run shell command。不展开 shell/MCP 返回的文件正文；失败显示最多四行
错误摘录，patch 显示最多三个文件的路径、行数和简短 diff。模型进度说明与最终回答正常
显示。此收缩仅影响终端：模型和会话日志仍保留工具输出预算内的结果；需要查看详细事件
时可使用 `nexus exec "任务" --json`。

同一 Conversation 跨回合复用模型 client、工具 registry 和 MCP 连接；`/new`、选中恢复会话
或退出时释放，新的 Conversation 在首次任务时连接。失败或被中断的 MCP server 在当前
Conversation 内不自动重连。配置在新 Conversation 中重载，不做热更新。内部调用者须在
执行 turn 的同一异步任务中 `await Conversation.close()`。

退出码：0 completed、1 failed、2 配置/参数错误、3 limited、130 aborted。

会话写入 `~/.nexus/sessions/<workspace-key>/` 下的 append-only JSONL，文件锁确保单
writer。普通输入每次开启独立 run，模型只接收当前 system/仓库指令、当前请求和该 run
的消息；之前 run 的完整历史仍保留在 JSONL。使用 `/resume` 或 `nexus resume` 选择最近
run 未完成的会话（中断、aborted、failed、limited），下一次输入会续接这个 run 的上下文。
已完成会话的下一次输入开启新 run；run_id 仍是内部元数据，选择器只恢复最近的未完成 run。
V0.1 不做压缩，活动上下文超预算返回 `limited/context_limit`，同一长任务的历史仍会增长。
恢复后等待用户输入，不重新执行旧命令。崩溃后的缺失结果标为
`interrupted_unknown`，需要先检查实际文件/进程状态。损坏尾行恢复到新文件并保留原文件；
中间损坏拒绝恢复。逐记录 flush 不等于耐断电事务，也不保证副作用恰好执行一次。

本地执行使用当前用户权限。shell/MCP 可访问该账户可访问的路径与网络，patch 的
workspace 边界不是整个运行环境的沙箱。stdout/stderr 共用 head/tail 输出预算；工具失败、
非零退出码、超时、patch 冲突都会作为 observation 返回模型。无法确认进程清理时停止当前
轮次并明确报告。退出码记录的是实际 shell 的退出码。

`apply_patch` 只接受 UTF-8 unified diff，使用 `--- a/path`、`+++ b/path` 和 `@@` hunks；
创建/删除用 `/dev/null`。不支持 Codex Begin Patch 格式、rename、二进制或模式变更。
整批预检失败不写文件；仅单文件替换原子性，多文件 I/O 故障会如实返回 partial。

公开事件不包含私有 protocol_data，并脱敏已配置的已知凭据。会话可能包含源码和用户
输入；这不是通用秘密识别系统。当前百炼实测无需保留 reasoning 续接字段。

## 验证与阅读代码

```sh
uv run --frozen ruff check .
uv run --frozen mypy src tests
uv run --frozen pytest
uv lock --check
uv build
git diff --check
```

CI 使用 Windows/Linux、Python 3.12，不需要云凭据或数据库。评测 collector 测试需要 Git，
普通 Nexus 启动不要求 Git。真实模型验收独立执行，不放入付费 CI。

建议先读 `src/nexus/core/agent.py`：关注 assistant tool_calls 如何产生匹配的 tool messages，
以及下一轮模型为什么能自行修复失败。再读 `core/model.py` 的流式协议边界和
`app/session.py` 的恢复语义。这里的 completed 是模型本轮正常结束，测试事实来自工具输出，
benchmark verdict 则来自官方 grader，三者分别记录。

仅四个直接运行依赖：`openai`、`mcp`、`prompt-toolkit`、`rich`。没有旧 Graph/Plan/数据库/
RAG/Skills/评测平台的兼容入口。官方 SWE-bench 使用 `scripts/swebench_v0.py` 的 predict 与
collect，具体准备、官方命令、原始 base_commit diff 和报告关联方法见英文说明。

## 有效设计依据

- [冻结架构](docs/next/architecture-contract-v0.md)
- [已批准开发设计](docs/next/01-development-design.md)
- [实施与验收标准](docs/next/02-delivery-and-acceptance.md)

审核记录只描述迁移历史，不建立额外 ADR 审批体系。
