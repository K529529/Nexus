# Nexus Next V0

[English / 完整配置与评测命令](README.md)

一个小型本地 Coding Agent：单个手写消息循环，`exec_command` 与
`apply_patch` 两个原生编码工具，加上记录进度的 `update_plan`。
模型负责探索、规划、修改、运行检查、修复和决定结束。
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
name = "qwen3.8-flash"
base_url = "https://<你的百炼账户endpoint>/compatible-mode/v1"
api_key_env = "DASHSCOPE_API_KEY"
context_window = 1000000
max_output_tokens = 16384
reasoning_effort = "low"
include_usage = true
request_timeout_seconds = 300  # 一次完整模型响应的期限，1..600 秒
output_token_parameter = "max_completion_tokens"  # 本服务已验证限制推理与回答总量

[runtime]
max_steps = 40

[execution]
output_limit_bytes = 32768
```

`output_token_parameter` 可选 `max_tokens`（兼容默认值）或 `max_completion_tokens`。
部分服务的 `max_tokens` 不包含推理；服务支持时选择总 completion 参数，使限制覆盖推理与回答。
`max_output_tokens` 同时用于该参数和 Context 预留。Nexus 不根据模型名猜测，也不在参数被拒绝后
自动换参数重试。服务接受参数不等于上限生效，需用小额受限探针验证；见
[真实参数证据](docs/next/cost-hardening.md)。

Base URL 必须替换成账户对应的真实地址。API key 只从指定环境变量读取：

```powershell
$env:DASHSCOPE_API_KEY = '<key>'
nexus
```

Linux 使用 `export DASHSCOPE_API_KEY='<key>'`。引导只询问变量名，不收集密钥。
旧配置中的 `observability` 等段落不再支持；先备份旧配置再移除淘汰字段，程序不会
自动改写旧配置。不会读取仓库 `.env` 或 `.nexus/config.toml`。

上面的百炼 Flash low 配置是本轮成本优先的日常候选：完整八例为 6/8 PASS、8/8 completed，
已报告费用约 ¥1.54，不代表已验证长期稳定。早期 Requests/Sphinx 使用不同的配置，
曾出现编辑或结束不收敛，见
[执行预算实测记录](docs/next/execution-budget-evidence.md)。
[后续模型对照](docs/next/model-delivery-evidence.md) 中，Qwen 已实现 Requests PASS 并自主结束；
Sphinx 修复也 PASS，但当时服务权限错误中断了收尾。权限恢复后的低成本模型与完整八例对照见
[当前结果与限制](docs/next/suite-hardening-evidence.md)；早期 Max 候选不代表当前默认选择或稳定性保证。

## 使用与恢复

```sh
nexus
nexus exec "修复空输入缺陷并运行已有测试"
nexus exec "分析测试失败" --json
nexus resume
```

交互命令：`/new`、`/resume`、`/skills`、`/skill <名称>`、`/skill off`、`/help`、`/exit`。
恢复列表按最近更新时间排序，展示最后一次用户输入摘要、UTC 时间和状态，每屏九条；
上下键浏览，Enter 确认，数字 1–9 直接打开当前屏幕的条目，Esc 取消。运行中 Ctrl+C 取消当前轮次并回收进程，
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
Context Runtime V0.2.1 保留 run 隔离和安全压缩：投影后的活动输入达到预算的 85% 时，对当前
run 的旧完整消息组生成摘要，目标约为预算的 60%。只替换活动上下文，Session 历史和 JSONL
仍保留原消息；resume 会重建已保存的投影。若服务端报超限且本请求边界尚未尝试压缩，
则压缩并最多重试一次；仍无法容纳时返回 `limited/context_limit`。工具结果在后续有效 assistant
消息写入前保持 FULL；已消费且掉出 token 工作集的结果，在请求时派生 `compact-v1` preview。
Session/JSONL 保留原文，snapshot 保留逻辑引用；HOT 所属工具组不能被摘要替换。
投影统计进入 JSONL/profile metrics，不扩常驻 TUI。软压缩和进一步历史缩减仍是后续工作，
详见 [V0.2.1 实测记录](docs/next/context-runtime-v0.2.1-evidence.md)。
恢复后等待用户输入，不重新执行旧命令。崩溃后的缺失结果标为
`interrupted_unknown`，需要先检查实际文件/进程状态。损坏尾行恢复到新文件并保留原文件；
中间损坏拒绝恢复。逐记录 flush 不等于耐断电事务，也不保证副作用恰好执行一次。

本地执行使用当前用户权限。shell/MCP 可访问该账户可访问的路径与网络，patch 的
workspace 边界不是整个运行环境的沙箱。stdout/stderr 共用 head/tail 输出预算；工具失败、
非零退出码、超时、patch 冲突都会作为 observation 返回模型。无法确认进程清理时停止当前
轮次并明确报告。退出码记录的是实际 shell 的退出码。

`apply_patch` 主格式为 Nexus `*** Begin Patch`，使用 Add/Update/Delete File 和基于上下文
匹配的 `@@` chunks，不需要行号或 hunk 行数。兼容旧 UTF-8 unified diff；不支持 rename、
二进制或模式变更。有歧义的匹配会被拒绝。整批预检失败不写文件；仅单文件替换原子性，
多文件 I/O 故障会如实返回 partial。

`update_plan` 完整替换当前 run 的 pending/in_progress/completed 清单，最多一个
in_progress，空数组清空。同 run 恢复已提交清单，每次请求投影最新状态，不逐轮追加历史。
清单 completed 不代表修复正确，也不会结束 Agent。Stagnation Detector 每个执行窗口最多
提供一次进度提醒，不强制编辑；请求还显示剩余模型轮次预算。这些机制提供提示，不能保证
模型持续推进或及时结束。

公开事件不包含私有 protocol_data，并脱敏已配置的已知凭据。会话可能包含源码和用户
输入；这不是通用秘密识别系统。服务返回 `reasoning_content` 时，adapter 将其作为绑定
service/model 的私有续接数据保存在本地会话，并随后续请求发送；公开事件不含该字段。
更换 service/model 应使用新会话，不能复用原有私有续接字段。

## 验证与阅读代码

开发调试时可启用每轮结束后的统计报告：

```sh
nexus --profile
nexus exec "解释当前项目结构" --profile
```

当前 Windows 开发环境可直接使用：

```powershell
.\.venv\Scripts\nexus.exe --profile
.\.venv\Scripts\nexus.exe exec "解释当前项目结构" --profile
```

不带参数时默认 TUI 不变；`--profile` 与 `--json` 互斥，不新增 `/profile` 聊天命令。
报告是派生的人类可读摘要，完整轨迹仍以 JSONL 为准，报告末尾显示实际日志路径。
累计 tokens 包含所有模型请求；活动上下文 first/peak/final 只取成功的普通请求输入，
排除压缩请求。usage 不完整时显示已知小计的 `≥` 下界及覆盖率；完全无已知值的字段
仍为 `unknown`。另计模型成功、失败尝试与显式重试次数。每轮（包括恢复执行段）重新统计，
不会累加上一轮数据。参见[离线示例与指标口径](docs/next/developer-run-profiler-v0.1-evidence.md)。

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

可用于面试的代码入口、设计取舍和证据边界见[工程讲解](docs/next/engineering-walkthrough.md)。

仅四个直接运行依赖：`openai`、`mcp`、`prompt-toolkit`、`rich`。没有旧 Graph/Plan 授权/数据库/
RAG/Skills/评测平台的兼容入口。官方 SWE-bench 使用 `scripts/swebench_v0.py` 的 predict 与
collect，具体准备、官方命令、原始 base_commit diff 和报告关联方法见英文说明。

## 有效设计依据

- [冻结架构](docs/next/architecture-contract-v0.md)
- [已批准开发设计](docs/next/01-development-design.md)
- [实施与验收标准](docs/next/02-delivery-and-acceptance.md)

审核记录只描述迁移历史，不建立额外 ADR 审批体系。


## 固定八项开发评测

从本源码 checkout 启动 Docker Desktop（Linux containers），沿用现有模型配置和环境变量：

```powershell
.\.venv\Scripts\nexus.exe eval pvlib__pvlib-python-1707
.\.venv\Scripts\nexus.exe eval --all
```

评测自动收集 Profile，终端只显示逐项进度与汇总。结果写入 `~/.nexus/evaluation/results/`，包括 `FinalCtx`、`ToolResultBytes`、候选 patch、轨迹和独立验证证据。这是固定 Next Dev Set 的本地验证，不是官方 SWE-bench 评分。环境身份、运行条件和限制见 [Evaluation V0](evaluation/next-dev-v0/README.md)；真实结果见 [验收记录](docs/next/evaluation-v0-evidence.md)。

[自主 hardening 阶段结果与成本证据](docs/next/suite-hardening-evidence.md).

## 轻量 Skill

将可信的独立 `SKILL.md` 放到 `~/.nexus/skills/<名称>/SKILL.md`；
[pytest 回归示例](docs/next/skills/pytest-regression/SKILL.md) 可手动复制到该目录。
不会自动安装示例或读取项目中的 Skill。

```text
# 系统终端
nexus skills list
nexus --skill pytest-regression
nexus exec "修复缺陷并补充回归测试" --skill pytest-regression

# Nexus 的 You 提示符内
/skills
/skill pytest-regression
/skill off
```

`/skills` 刷新并展示可用及启用状态；显式选择在当前 Session 的连续任务中保留，
再次选择会替换，`/skill off` 取消显式指定并恢复自动选择模式，`/new` 清除旧选择。
模型也可通过普通 `load_skill` 工具按需加载，自动加载只影响当前 run。
恢复使用保存的正文快照，文件变化不会悄悄改变旧任务；再次显式选择才读取新版。
目录、正文都计入 Context 预算；固定 eval 默认不读取个人 Skill 目录。
范围、上限、持久化与回退 tag 见 [实现说明](docs/next/skills-v0.1.md)。
