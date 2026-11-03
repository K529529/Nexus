# Stage 8 实施记录

授权范围见 [用户实施方案](stage8-multi-agent-plan.md)。本次明确扩展 V0 的
Sub-Agent 排除项；仍使用一套核心循环，不恢复旧架构。授权分阶段 commit/push，
不合入 V0.3，不发布版本。

2026-11-03 用户补充：开发完成后不运行 ABK，由 Archer 自行手动运行。
本次只准备对照配置/统计说明，保留开发侧验证与未验证项记录。
用户进一步明确 history/HTML trace 指本地 8 case 的 `history.html` 和 `report.html`；
Stage8-6 扩展这两个现有入口，不新增通用 history 命令。

基线：远端 `codex/nexus-v0.3`，`c6f1b2ca1c1351abc6a3542a232cf18dc8e9a85c`。
开发分支：`codex/nexus-v0.3-stage8-multi-agent`。
开始时已有 `.idea/`、`docs/next/evidence/stage4-srt-poc/`、两个 Stage4 SRT
脚本未跟踪，保留，不纳入本次提交。

## Stage8-1 — 运行中输入

- 执行留在原 CLI Task，输入监听独立，保留 MCP 上下文的同 Task 生命周期。
- Enter 指导当前任务；Tab 排队下一任务。工具批次完整回传后注入指导；最终
  回复期间新到的指导继续当前 run。排队文本在当前 turn 结束后顺序执行。
- 显示接收/投递状态与消息 ID；执行期间 `/pending` 查看，`/edit ID text`
  修改，`/withdraw ID` 撤回尚未投递消息。
- 审批独占输入并保留草稿；运行中 Ctrl+C 取消任务，重复取消不打断清理。
- 输入生命周期事件写入 JSONL。跨进程恢复待处理输入尚未实现，留在持久化阶段。

2026-11-03 Windows / Python 3.12 验证：

- `pytest tests/test_stage8_input.py tests/test_input_v02.py tests/test_tui_stage4d.py
  tests/test_agent.py tests/test_cli_ux.py tests/test_conversation.py -q -p no:cacheprovider`：
  95 passed / 1 failed（旧 Conversation 测试替身缺少新增 inbox）；补齐替身后
  单独复验该例 1 passed。新增 4 例全部通过，含真实 Shell 执行期间指导。
- `ruff check --no-cache .`、`mypy src tests`、`git diff --check` 通过。
- 首次默认 pytest 临时目录和 Ruff 缓存访问被拒；未修改权限，后续使用每次新建的
  系统临时父目录 `PYTEST_DEBUG_TEMPROOT` 与无缓存检查。沙箱内真实进程清理失败，
  正常主机权限复验通过；不能以受限沙箱失败作为主机清理验收结果。
- Windows 人工终端/中文 IME 实际手感、真实模型指导响应尚未验收。

下一阶段：默认关闭的原生子代理、Fresh 上下文、独立会话/日志、异步创建及结果获取。

## Stage8-2 — 原生子代理闭环

- Stage8-1 已推送：`7733665`。
- `[multi_agent] enabled=false` 默认关闭；开启提供 `spawn_agent`、`wait_agents`、
  `list_agents`。三个内置 Markdown 角色与角色模型别名/推理强度配置；模型在创建时固定。
- 独立 Conversation、Session、Context、模型客户端、工具绑定；一套 `run_turn`。
  创建立即返回，等待超时不取消。父结束前取消并等待子清理；清理失败保留额度。
- scout/reviewer 无 apply_patch；角色职责约束不构成 Shell 只读隔离。
- 子日志存放 `<session_id>/subagents/<agent_id>.jsonl`，不进入 history 顶层。
- 嵌套/MCP 子代理共享连接/父子消息在 Stage8-3 接入；当前子代理不连接 MCP。
- 24 项相关测试通过（新增子代理/输入、Conversation、边界）；Ruff、完整 mypy
  与 diff 检查通过。验证父模型可在子模型等待时继续、Fresh 无父对话复制、客户端
  在原 Task 关闭、满额及时返回、等待不取消以及子任务首次调度前取消。
- 使用短系统临时父目录 `n8-<随机值>`：之前过长的测试临时路径加子日志层级超出
  当前 Windows 文件路径长度限制；未改系统长路径设置。实际超长存储路径仍可能失败。
- 真实模型并发与 Windows 终端体验未验收；未声称性能优于单 Agent。

## Stage8-3 — 协作与资源管理

- Stage8-2 已推送：`b14087a`。
- 父子指导/进度/阻塞决策请求；等待结果被求助、用户指导、状态变化唤醒。
  无兄弟直连；等待超时不取消。第二层无创建工具，运行时仍检查开关、角色与深度。
- 子孙共用额度，等待/取消清理期间仍占额度；默认 3，满额立即返回。
- Skills 独立目录扫描/会话加载；MCP 权限逐代取交集，根连接共享、工具绑定独立，
  同服务串行/不同服务并发，故障在安全请求边界移除相关代理工具并通知。
- 同一棵树的审批串行；每个代理独立 Gateway 与一次性批准，保留现有沙箱策略。
- 取消向下传播并等待清理；新代理不能绕过共享工作区尚未核实的副作用。
- 90 项相关测试通过；共享审批锁/运行时校验收尾后 66 项复验通过；包括真实
  Windows Shell 及其 Python 子进程树取消，并查询 OS 进程退出状态。清理失败
  保留额度也有故障注入覆盖。Ruff、完整 mypy、diff 检查通过。
- 未完成真实模型嵌套协作及人工终端验收；下一阶段为持久化/显式恢复。

## Stage8-4 — 持久化与恢复

- Stage8-3 已推送：`5370543`。
- 根会话恢复读取代理关系与独立子日志；未完成代理为 interrupted，无后台 Task
  自动启动。`resume_agent` 显式使用子会话历史、原模型快照、当前权限继续。
- 创建收据记录源 run/call，Stage6 恢复可确认已创建任务，不重复创建；工具副作用
  继续使用 Stage6 UNKNOWN/PREPARED 判定。跨代理共享未核实副作用，不能另建代理绕过。
- 每次显式恢复有新 execution identity；完成结果以父会话中持久化的消息判定
  交付，收据仅作诊断，完成未交付可补交。主代理结束前收回结果或取消清理。
- 运行中输入 ID 进入本地消息记录（不进入 Provider wire 字段）；未投递输入恢复，
  已持久化输入不重复投递。收尾压缩期间到达的指导继续原任务。
- 新增真实独立 Python 进程 `os._exit(77)` 崩溃实验，覆盖创建、命令副作用、完成、
  已交付窗口；非幂等 counter 始终只有一个字节。显式恢复复用子历史、不重放命令。
- 联合回归 100 passed / 1 failed（旧 CLI 替身尚未提供 inbox）；补齐后该例单独
  1 passed。Ruff、完整 mypy、diff 检查通过。历史清理失败保留状态/额度。
- 恢复不意味着旧 OS 子进程已经退出；未知副作用仍需核实，不恢复旧一次性审批。

## Stage8-5 — 终端体验

- Stage8-4 已推送：`707e3d4`。
- 输入框旁实时树显示角色、任务摘要、层级、状态、当前操作、耗时、用量与额度。
  `/agents` 查看全树；`/agent ID|root` 选择指导目标（支持唯一 ID 前缀）；
  `/agent detail ID` 从独立子日志查看详情，`/agent cancel ID` 取消整棵子树。
- Tab 始终排队根任务；选定子代理的 Enter 指导/决策回复会通知父代理。
  `/pending`、`/edit`、`/withdraw` 操作所选代理的待投递输入。
- 子代理流不混入主输出；求助/失败明显提示；审批显示所属 Agent、串行接管输入，
  原草稿保留。详情展示不再复制进主 JSONL。
- 统一事件用量投影包含失败/重试/压缩请求，缓存不重复相加，缺失保持 unknown；
  接入离线入口留在下一阶段。
- 原有输入/审批/缩放/CLI 回归 87 passed；新增真实 prompt_toolkit 渲染、草稿保持、
  目标指导/父通知/详情隔离和用量测试 3 passed。Ruff、完整 mypy、diff 检查通过。
- Windows 实际终端、中文 IME 与真实模型交互仍待 Archer 人工验收。

## Stage8-6 — 历史与可观测性

- Stage8-5 已推送：`27cd00a`。
- 按 Archer 确认扩展本地 8-case 的 `history.html` / `report.html`：代理概况、
  按代理筛选、同一时间轴的并行模型/工具（含等待）操作、通信与独立轨迹。
  旧记录及未实际委派的会话不显示空面板。
- TUI 与本地 JSONL 投影共用用量累加器；汇总实际请求（包括失败/重试/压缩），
  缺失保持 unknown，缓存不再次计入 total，无可靠价格时费用为 unknown。
  评估计数改为全树，context 指标仍为根代理并明确标注。
- 沿用 Stage7 导出，子代理不单独发送；根导出关联全树生命周期、请求、工具和
  通信元数据。每次恢复有新 execution / trace identity，不跨停机时间计时。
  Archer 明确授权沿用现有 LangSmith 配置导出该元数据；不增加正文/提示词/源码。
- 修复子代理继承评估环境 registry 工厂，避免容器评估意外回退到宿主 Shell。
- 74 项历史/报告/遥测回归通过，Ruff、完整 mypy 通过。验证稳定父子关联、
  用量不重复、恢复轨迹分离、正文与私有续写字段不导出、缺尾旧日志兼容。
- 仅验证本地遥测投影及既有本机失败端点测试，未调用真实 LangSmith 服务。
  未运行本地 8-case / ABK；按用户要求由 Archer 手动执行。
