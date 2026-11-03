# Stage 8 实施记录

授权范围见 [用户实施方案](stage8-multi-agent-plan.md)。本次明确扩展 V0 的
Sub-Agent 排除项；仍使用一套核心循环，不恢复旧架构。授权分阶段 commit/push，
不合入 V0.3，不发布版本。

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
