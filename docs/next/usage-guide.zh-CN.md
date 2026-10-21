# Nexus 使用手册

[快速开始](../../README.zh-CN.md) · [English](usage-guide.md)

这里保留完整配置、会话语义、工具边界与开发验证说明；首次使用请先看快速开始。

## 配置

V0.3 Stage 1 使用 [DeepSeek 官方 + 硅基流动 Qwen 双模型示例](context-runtime-v0.3.toml)。
备份后把需要的段落合并到 `~/.nexus/config.toml`；CLI 固定读取此文件，没有 `--config`。
两个段落分别读取 `DEEPSEEK_API_KEY` 与 `SILICONFLOW_API_KEY`，不在 TOML 保存密钥。
旧单模型配置仍可运行容量内的任务，但缺少 `[compaction_model]` 会明确提示，无法语义压缩。
Qwen 已验证 `enable_thinking=false`，不复用主模型的 `reasoning_effort` 语义。
详见 [Context Runtime 设计与限制](context-runtime-v0.3.md)。以下保留 V0.2 通用配置示例。

保真专项增加了可追溯来源和可更新摘要，但固定 Qwen 实验仍未通过语义验收，并暴露格式拒绝
增加的问题。当前仅为阶段审查候选，见[专项负面结果](compaction-fidelity-hardening.md)。

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
response_language = "zh-CN"  # 默认中文；en 英文；auto 跟随本轮用户语言

[execution]
output_limit_bytes = 32768
```

`output_token_parameter` 可选 `max_tokens`（兼容默认值）或 `max_completion_tokens`。
`response_language` 控制面向用户的模型进度与回复，用户明确指定其他语言时优先遵从用户。
它不改写内部推理或工具参数，不增加翻译调用；配置在新 Conversation 生效。
部分服务的 `max_tokens` 不包含推理；服务支持时选择总 completion 参数，使限制覆盖推理与回答。
`max_output_tokens` 同时用于该参数和 Context 预留。Nexus 不根据模型名猜测，也不在参数被拒绝后
自动换参数重试。服务接受参数不等于上限生效，需用小额受限探针验证；见
[真实参数证据](cost-hardening.md)。

Base URL 必须替换成账户对应的真实地址。API key 只从指定环境变量读取：

```powershell
$env:DASHSCOPE_API_KEY = '<key>'
nexus
```

Linux 使用 `export DASHSCOPE_API_KEY='<key>'`。引导只询问变量名，不收集密钥。
旧配置中的 `observability` 等段落不再支持；先备份旧配置再移除淘汰字段，程序不会
自动改写旧配置。不会读取仓库 `.env` 或 `.nexus/config.toml`。

上面的百炼 Flash low 配置是 V0.2 的成本优先候选：完整八例为 6/8 PASS、8/8 completed，
已报告费用约 ¥1.54，不代表已验证长期稳定。早期 Requests/Sphinx 使用不同的配置，
曾出现编辑或结束不收敛，见
[执行预算实测记录](execution-budget-evidence.md)。
[后续模型对照](model-delivery-evidence.md) 中，Qwen 已实现 Requests PASS 并自主结束；
Sphinx 修复也 PASS，但当时服务权限错误中断了收尾。权限恢复后的低成本模型与完整八例对照见
[当前结果与限制](suite-hardening-evidence.md)；早期 Max 候选不代表当前默认选择或稳定性保证。

## 使用与恢复

```sh
nexus
nexus exec "修复空输入缺陷并运行已有测试"
nexus exec "分析测试失败" --json
nexus resume
```

交互命令：`/new`、`/resume`、`/compact`、`/skills`、`/skill <名称>`、`/skill off`、`/help`、`/exit`。
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
writer。普通输入每次开启独立执行 run，但同一 Session 的历史连续，保留旧 run 的中间
assistant 信息、Tool Calls 与配对 Results，直到统一语义压缩。`/new` 不继承旧 Session。
使用 `/resume` 或 `nexus resume` 选择最近
run 未完成的会话（中断、aborted、failed、limited），下一次输入会续接这个 run 的上下文。
已完成会话的下一次输入开启新 run；run_id 仍是内部元数据，选择器只恢复最近的未完成 run。
V0.3 Context Runtime 达到模型窗口 90% 时压缩（128000 对应 115200 tokens）；
输出预留与安全余量独立检查，必要时提前保护。保留约 20000 tokens 的近期完整消息组，
仅把旧前缀和上次摘要发送给独立压缩模型。没有 HOT/COLD，也没有旧 Run 摘录层。
checkpoint 追加写入后才更新上下文，原消息与 JSONL 不改写；恢复重建同一个 Session 投影。
压缩可在 Run 内、completed 后等待输入前、手动 `/compact` 时发生。
`/compact` 不创建任务、不调用工具、不耗 Agent steps。服务端超限最多额外压缩并重试一次。
失败保留旧历史；如果旧请求已不安全，则明确停止。缺少压缩模型不会调用主模型代替。
TUI 显示当前估算、阈值、压缩前后大小和独立压缩用量；累计调用用量不是当前上下文大小。
旧 provider 私有协议仍绑定原渠道，换渠道后可能需要新 Session；损坏或错配历史会拒绝。
预算最后 3 轮提醒优先验证与交接，耗尽后保持 limited，不额外调用模型。
非 completed 的运行保存有界工具事实和模型 Plan 待办，TUI 提示显式 Resume；
工具成功不代表任务已验证。摘要结构合法也不证明事实保真；实测仍存在状态误归类，见
[Context Runtime 说明](context-runtime-v0.3.md) 和 [验收证据](context-runtime-v0.3-evidence.md)。
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
不会累加上一轮数据。参见[离线示例与指标口径](developer-run-profiler-v0.1-evidence.md)。

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

可用于面试的代码入口、设计取舍和证据边界见[工程讲解](engineering-walkthrough.md)。

五个直接运行依赖：`openai`、`mcp`、`prompt-toolkit`、`rich`、`pyyaml`（安全解析 Skill 元数据）。
没有旧 Graph/Plan 授权/数据库/RAG/旧 Skills/评测平台的兼容入口。官方 SWE-bench 使用 `scripts/swebench_v0.py` 的 predict 与
collect，具体准备、官方命令、原始 base_commit diff 和报告关联方法见[英文使用手册](usage-guide.md#official-swe-bench-integration)。

## 有效设计依据

- [冻结架构](architecture-contract-v0.md)
- [已批准开发设计](01-development-design.md)
- [实施与验收标准](02-delivery-and-acceptance.md)

审核记录只描述迁移历史，不建立额外 ADR 审批体系。


## 固定八项开发评测

从本源码 checkout 启动 Docker Desktop（Linux containers），沿用现有模型配置和环境变量：

```powershell
.\.venv\Scripts\nexus.exe eval pvlib__pvlib-python-1707
.\.venv\Scripts\nexus.exe eval --all
```

评测自动收集 Profile，终端只显示逐项进度与汇总。结果写入 `~/.nexus/evaluation/results/`，包括 `FinalCtx`、`ToolResultBytes`、候选 patch、轨迹和独立验证证据。这是固定 Next Dev Set 的本地验证，不是官方 SWE-bench 评分。环境身份、运行条件和限制见 [Evaluation V0](../../evaluation/next-dev-v0/README.md)；真实结果见 [验收记录](evaluation-v0-evidence.md)。

[自主 hardening 阶段结果与成本证据](suite-hardening-evidence.md).

## 轻量 Skill

将可信的独立 `SKILL.md` 放到 `~/.nexus/skills/<名称>/SKILL.md`；
[pytest 回归示例](skills/pytest-regression/SKILL.md) 可手动复制到该目录。
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
范围、上限、持久化与回退 tag 见 [实现说明](skills-v0.1.md)。

## 许可证

[MIT](../../LICENSE)。
