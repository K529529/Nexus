# Nexus 0.3 使用手册

[快速开始](../../README.zh-CN.md) · [English](usage-guide.md) ·
[发布说明与限制](release-0.3.0.md)

本文适用于 Nexus 0.3.0。历史设计和阶段实验保留在原文档，
不把旧配置、旧成绩或阶段状态当作当前推荐。

## 配置

唯一用户配置为 `~/.nexus/config.toml`（Windows：`%USERPROFILE%\.nexus\config.toml`）。
不读取项目 `.env` 或项目 `.nexus/config.toml`，没有 `--config` 参数。
首次交互向导配置主模型；压缩模型需手动补充。未知字段会报错，先备份再合并配置，
不要覆盖个人 MCP、模型别名或权限设置。

以下模型名和 endpoint 是占位值，替换成账户实际支持的配置；密钥只写环境变量名：

```toml
[model]
name = "your-coding-model"
provider = "openai"
base_url = "https://your-service.example/v1"
api_key_env = "NEXUS_MODEL_API_KEY"
context_window = 128000
max_output_tokens = 8192
request_timeout_seconds = 120
include_usage = true
# reasoning_effort = "high"
# output_token_parameter = "max_completion_tokens"

[compaction_model]
name = "your-compaction-model"
provider = "openai"
base_url = "https://your-compaction-service.example/v1"
api_key_env = "NEXUS_COMPACTION_MODEL_API_KEY"
context_window = 128000
max_output_tokens = 4096
# enable_thinking = false

[runtime]
max_steps = 40
response_language = "zh-CN"

[execution]
output_limit_bytes = 32768
# shell = "C:/Program Files/PowerShell/7/pwsh.exe"

[security]
mode = "local"

[memory]
enabled = true

[multi_agent]
enabled = false
allowNestedSubagents = false
max_agents = 3

[web]
enabled = false

[tracing]
enabled = false
```

支持 `openai`、`deepseek`、`dashscope` 三个 provider，底层经 LiteLLM，
不配置自动模型 fallback。OpenAI-compatible 服务通常选择 `openai`。
`reasoning_effort`、`enable_thinking` 与输出参数是否有效由对应服务决定，
Nexus 不按模型名称猜测或在拒绝后改用其他参数。
`request_timeout_seconds` 是单次模型响应超时，不是整个任务或 ABK Agent 阶段预算。

`context_window` 是 Nexus 管理的主模型输入窗口；`max_output_tokens` 单独限制输出，
不会从主输入窗口扣除。请按服务真实总容量留出输入与输出空间。
压缩模型输入还需预留输出和安全余量。缺少压缩配置时不回退主模型做摘要，
容量内可继续，不能压缩或超限会明确提示/停止。

`NEXUS_MODEL_NAME/BASE_URL`、`NEXUS_COMPACTION_MODEL_NAME/BASE_URL` 可覆盖对应字段。
环境变量必须存在于启动进程中；更新 Windows 用户变量后需重新打开终端。
配置在 Conversation 生命周期内使用，编辑后重新启动。
`runtime.response_language` 支持 `zh-CN`、`en`、`auto`。

模型别名使用 `[models.alias]`，字段与 `[model]` 相同；`/model` 打开键盘菜单，
`/model alias` 直接切换，`default` 指向启动默认模型。
别名不改变独立压缩模型；已有子代理保留创建时模型快照。
详细参数与历史验证见[模型接入](stage7-observability-models.md)。

### 安装与升级

在独立的 Python 3.12+ 环境中从 PyPI 安装：

```sh
python -m pip install --upgrade nexus-coding-agent==0.3.0
```

也可运行 `uv tool install nexus-coding-agent==0.3.0`，或使用 [README 源码步骤](../../README.zh-CN.md#快速开始)。
升级前备份 `~/.nexus/config.toml`、sessions 与 memory；升级不会自动修改这些内容。
独立环境安装后检查 `python -m nexus --version`，并确认 `Get-Command nexus`（Windows）
或 `command -v nexus`（Linux）指向预期解释器。旧的全局 CLI 可能仍运行旧代码。

从 0.2 升级保留兼容的模型字段，补充压缩配置，按需开启新能力。
旧 `observability`、SRT 专用权限和旧域名规则不是当前 `tracing`/平台配置，
不能机械搬入。旧日志继续采用有证据才恢复的保守策略；升级不赋予未知副作用可重放性。
从 0.1 升级另读[历史迁移说明](release-0.2.0.md#install-and-migrate)。

## 命令与交互

| 场景 | 操作 |
| --- | --- |
| 交互 / 单次运行 | `nexus` / `nexus exec "任务"` |
| 原始公开事件 / 统计 | `nexus exec "任务" --json` / `nexus --profile` |
| 恢复当前工作区会话 | `nexus resume` 或 `/resume`，选择最近记录 |
| 清空当前对话 / 压缩历史 | `/new` / `/compact` |
| 切换模型 / 权限 | `/model [alias]` / `/permissions` |
| 查看记忆状态 / 开关 | `/memory [status\|on\|off]` |
| 查看 / 显式选择 Skill | `/skills`、`/skill 名称`、`/skill off` |
| 查看帮助 / 退出 | `/help` / `/exit` |

`--json` 和 `--profile` 互斥。恢复命令没有直接传 Session ID 的位置参数。
单次临时权限覆盖：`nexus exec "任务" --permission-mode read-only`，
可选 `local`、`read-only`、`workspace-write`。

运行中输入框持续存在。**Enter 是指导，Tab 是排队**，不要求用户使用指导命令。
指导在完整工具批次后的安全边界进入模型请求；已收到不等于已投递。
`/pending` 查看选中代理的待处理消息；`/edit ID 新内容`、
`/withdraw ID` 只作用于尚未开始投递的消息，缺少参数、未知 ID 会提示，
不退出监听。排队文本按后续任务执行，即使以斜杠开头也不当成空闲命令。

F4、Alt+Enter、Ctrl+J 插入换行。F2 开关长粘贴捕获，F3 展开粘贴块；
支持 bracketed paste。补全菜单优先于 Tab 排队。Ctrl+C 取消活动任务并等待工具清理。
PgUp/PgDn 阅读会话，Ctrl+End 回到最新；新输出不强制跳回底部。
具体宿主的中文输入法、缩放和粘贴体验仍需人工检查。

## 多 Agent

默认关闭，设置 `[multi_agent] enabled = true` 后才为新根会话启用协作。
默认最多 **3 个活跃子/孙代理**，不包含根代理；等待决策和取消清理也占额度。
默认不允许嵌套；开启 `allowNestedSubagents` 后最多主 → 子 → 孙两层。
所有实例复用同一个 Agent Loop，不是第二套工作流。

| 角色 | 默认推理强度 | 默认模型 | 写入边界 |
| --- | --- | --- | --- |
| scout | low | 继承创建者当前模型 | 不提供 apply_patch；Shell 不构成只读隔离 |
| worker | high | 继承创建者当前模型 | 依照现有 Gateway 权限 |
| reviewer | high | 继承创建者当前模型 | 不提供 apply_patch；Shell 不构成只读隔离 |

角色覆盖使用 `[multi_agent.roles.scout]` 等表，可设 `model = "alias"`、
`reasoning_effort`、`mcp_servers = ["已配置服务名"]`。默认子代理不拿 MCP，
配置后只取父权限与角色名单交集。完整示例见 [Stage8 使用说明](stage8-usage.md)。

子代理 Fresh 启动，只接收任务、背景与范围，不复制父对话；拥有自己的历史。
共享同一工作区，任务范围不是文件锁，不同代理应避免同时编辑同一文件。
F6 或 `/agents` 打开代理选择器，展示任务名、角色、短 ID 与状态；
`/agents ID` 或 `/agent ID` 进入该会话，Esc 或 `/agent root` 回到根。
Enter 指导选中子代理，Tab 仍给根排队。已完成子代理只读，不会因输入复活。
当前树保留活跃/待处理实例，旧轮已完成项进入 `/agents history`；
`/agent detail ID` 查看轨迹，`/agent cancel ID` 取消该子树。
累计 token 是根及所有历史子代理的**会话总量**，不是当前任务用量。

## 上下文与长期记忆

会话历史跨 Run 连续；`/new` 开始新 Session。自动压缩阈值是管理输入窗口的 90%，
近期目标约 20K token，始终保持完整工具调用/结果组。压缩可以在请求前、正常收尾、
手动 `/compact` 或 provider overflow 后触发；overflow 最多一次压缩重试。
原始 JSONL 不改写。摘要保存语义状态，Runtime Facts 独立保留工具回执；
Schema 合法并不保证摘要准确，也不把命令退出码当作任务验证通过。
详见[连续上下文](context-runtime-v0.3.md)及[保真失败与取舍](compaction-simplification.md)。

长期记忆默认开启，以 `~/.nexus/memory/` 中 MEMORY.md、SCRATCHPAD.md、daily/
为事实来源；QMD 是可重建索引，恢复删除记录在 recovery/。
Local 首次启动可自动准备 Node/QMD 和检索模型，可能需要网络、磁盘与等待。
`nexus memory status` 只读诊断；`nexus memory off` 关闭后续新会话记忆，
保留文件、索引和模型。交互 `/memory off` 会换新 Session，避免旧记忆继续进入请求。
包含记忆上下文的旧会话不能在关闭记忆时直接恢复。
[设置、权限与平台验收](memory-setup-and-controls.md)保留实际安装证据；
离线测试不等于全部平台的真实 QMD 安装验收。

## 工具、Skills 与 MCP

原生 `exec_command`、`apply_patch`、`update_plan` 等由统一 Registry/Gateway 管理。
Skills 位于 `~/.nexus/skills/<name>/SKILL.md`；`nexus skills list` 列出，
`nexus --skill name` 或 `nexus exec "任务" --skill name` 显式选择。
`/skill off` 取消显式选择，自动加载仍可用；Skill 不授予权限或自动执行附带脚本。

MCP 仅连接显式配置的 stdio 服务，调用现有工具/读取受支持文本资源：

```toml
[mcp.servers.example]
command = "/absolute/path/to/already-installed-server"
args = []
env_from = { SERVICE_TOKEN = "MY_SERVICE_TOKEN" }
```

`env_from` 的值是当前进程变量名，缺失会提示，不应写明文 key。
远端文本、结构化结果和嵌入文本资源有输出上限；不实现 MCP sampling/elicitation。
MCP 工具仅在 Local 暴露，不将宿主服务伪装成沙箱内调用。
Web 默认关闭，在 Local 中配置 `[web] enabled = true` 和独立 DeepSeek key 后
提供 `web_search/web_fetch`；不是任意 provider 的通用联网开关。
[Web 配置](stage7-web-tools.md)记录其 endpoint 和失败边界。

## 沙箱与审批

- **Local（默认）**：当前用户权限执行。工作区不是 OS 安全边界。
- **Windows 受限模式**：Native token/ACL/Job 路线，部分强制隔离；网络不受限，
  不阻止通过已有读取权限获取凭据文件。Koffi/Node/Python 安装需在写根之外。
  授权工作区可能留下持久 ACL，必须事先理解并明确列入 `windows_acl_roots`；
  不是临时命令结束即撤销的权限。
- **Linux/WSL 受限模式**：非 root 用户 + Docker，RO/RW workspace bind、独立容器，
  默认 2 CPU/1024 MiB/128 PID，network 为 bridge 或 none。
  `nexus sandbox doctor` 诊断，`nexus sandbox setup` 显式准备镜像。
- 缺少后端/依赖会明确失败，不自动降级 Local。macOS 没有本轮受限后端验收。
- 只读下精确 Patch 的 Allow once 绑定调用、参数摘要与范围，不授权任意 Shell、
  额外目录或后续调用。全树审批串行，界面标明所属代理，保留输入草稿。

完整配置、Windows 0700/ACL 局限及 Linux 清理规则见
[平台说明](stage4-platform-backends.md)。受限模式下宿主记忆是显式授权能力，
不默认注入全局快照/QMD；可信 Skills 也受配置控制。

## 恢复、JSONL 与查看方式

根会话在 `~/.nexus/sessions/`，子代理日志位于根日志目录下
`<session_id>/subagents/<agent_id>.jsonl`。含原始对话、代码和工具输出，
分享前审查；外部遥测的白名单不代表本地日志没有敏感内容。
同一 JSONL 使用独占写锁；损坏尾部恢复为新副本，原文件保留。

工具 checkpoint：
PREPARED 可经当前权限重新进入执行；DISPATCHING 而无完整结果为 UNKNOWN，
不能重放；COMPLETED 恢复保存的结果。
Patch 缺结束回执时只读核对 before/after 哈希，不能据此伪造成功。
取消或清理不确定必须核查真实副作用。根恢复重建代理树，不自动重启整棵树；
父代理显式 `resume_agent` 使用该实例自己的历史，旧审批不复用。
详见[Durable Execution](stage6-durable-execution.md)。

本地固定八项评测生成：
`~/.nexus/evaluation/history.html`（跨轮历史）和
`~/.nexus/evaluation/results/<run-id>/report.html`（单轮结果/轨迹）。
离线重建历史：`python -m nexus.evaluation.history`，
可用 `--home /path/to/evaluation` 指定评测根目录。
它们不是所有普通交互会话自动生成的 HTML，也不是 ABK 的报告。
[八项评测入口](../../evaluation/next-dev-v0/README.md)说明数据与验证规则。

`nexus --profile` 展示单轮统计；`exec --json` 输出公开运行事件。
多 Agent 必须区分根/子事件，根结论不能取最后一个子代理结束事件。
全树用量按实际请求计数，缺失为 unknown，缓存输入不重复加入总量；
时间不叠加并行子代理时长。

## 可观测性

默认不向外部发送 trace。配置 `[tracing]` 的 enabled、完整
`.../otel/v1/traces` endpoint、api_key_env 和 project 后，导出已结束操作的元数据：
任务/父子/执行 ID、模型/工具关联、状态、时间与实际用量。
不导出提示词、reasoning、源码、工具完整输出、工作区路径和通信正文。

`nexus trace export /path/session.jsonl --run-id RUN_ID` 显式补发指定任务；
远端是否去重取决于平台，不承诺 exactly-once。
失败不改本地业务结果；缺失结束事件不伪造完成时间。
详细配置见[OTel 与模型接入](stage7-observability-models.md)。

## 开发与证据

[开发、测试、构建与发布](development-and-release.md) ·
[当前架构](engineering-walkthrough.md) · [0.3.0 验证记录](release-0.3.0-validation.md)。
冻结设计、历史实验及失败原文均保留；ABK 的评测提交和发布候选分开记录。
