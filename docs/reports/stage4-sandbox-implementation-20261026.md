# Nexus V0.3 Stage 4 — Sandbox + HITL implementation

日期：2026-10-26（Asia/Shanghai）。本轮以用户提供的 Stage 4 任务书作为新增能力授权，
保持已有单一手写 Agent Loop，不恢复旧工作流、风险分级或多 Agent 架构。

## 1. 分支、基线与交付状态

- 工作分支：`codex/nexus-v0.3-stage4-sandbox`。
- 基线/当前 HEAD：`b13ee4e13b32bc60bf56cdad9eaf5461934c437e`。
- 该基线为 V0.3 的合并提交；父提交分别为 `602c60ba712708badf6938556c9d04f7d0084690`
  和 Stage 3 的 `603502a8e222a921830609e1cfffb04828f9f87e`。
- 最终实现 Commit SHA：**NOT CREATED**。提交前已完成明确文件清单暂存与检查；
  自动审批拒绝 `git commit`，理由为 AGENTS.md 禁止自行提交、当前用户消息未明确授权 commit。
  因此本报告对应工作区/暂存区实现，不能将基线 SHA 误认为包含 Stage 4 的提交。
- 未 push、merge、publish。Stage 3 和 V0.3 分支没有被本轮移动。
- 原有 `scripts/stage4_srt_probe.mjs`、`scripts/stage4_srt_filesystem_poc.mjs`、
  `docs/next/evidence/stage4-srt-poc/` 均保留、未运行、未暂存。
  `.idea/` 为其他未跟踪内容，保留且排除在交付范围外。

## 2. 实现状态

| 模块 | 状态 | 证据与实际边界 |
| --- | --- | --- |
| Security Gateway | Implemented / Functionally Verified | 所有 Loop tool calls 经统一入口；Local 原工具直接调用；无新增循环 |
| Tool Policy | Implemented / Mock Tested | 三种模式，确定性 allow/ask/deny；未知能力拒绝，能力声明来自宿主工厂 |
| HITL | Implemented / Functionally Verified | allow once、deny、cancel、超时、无 UI 拒绝、真实 pipe-input 测试 |
| 审计和 Resume | Implemented / Functionally Verified | 审批先落 JSONL，恢复不重放调用、不恢复授权；未派发与未知结果区分 |
| Bridge IPC/进程生命周期 | Implemented / Mock Tested | 真实 Node 进程配非隔离替身 manager；复用、输出、超时、取消、故障验证 |
| Patch Worker | Implemented / Functionally Verified | 真实独立 Python 进程调用原 Patch Core；冲突、审计屏障、取消与部分结果 |
| SRT 公开 API 适配 | Implemented / NOT live verified | 固定 0.0.81 静态接口核对；加载适配器处于未接通状态 |
| Windows 真实受限 Backend | Blocked | Python 和 Node 生产入口均 fail-closed；无配置或环境变量绕开验收门禁 |
| 动态文件权限扩展 | Not Implemented | 保留 `request_write_access` 接口，返回 `dynamic_scope_unsupported` |
| 受限 MCP | 明确拒绝 | 不连接配置的服务器，避免 stdio server 在工具调用前产生宿主副作用 |
| 受限 Memory 后台索引/自动 Daily | 明确禁用 | 不创建 index/bootstrap，不生成自动 snapshot、handoff、exit summary |
| Linux/macOS OS 隔离 | Not Implemented / NOT RUN | 本轮没有进行这些平台的实际隔离验收 |

**这不是 Windows OS 级隔离验收通过或 Production Ready 声明。**

## 3. 执行架构

```text
现有 Agent Loop
  → SecurityGateway.execute(call, registry, session, execution_context, emit)
    → Local：直接调用既有 Tool.execute
    → 可信宿主服务：internal / skills / 单独批准的 memory
    → Restricted：SRTBackend 门禁（当前返回 backend_blocked）

隔离验收后才可接通的工程链路：
  BridgeClient → JSONL RPC → 长驻 Node Bridge → SRT wrapWithSandboxArgv
    → PowerShell
    → python -I <可信安装目录>/patch_worker.py → 既有 apply_patch
```

`core/security.py` 只依赖标准库和 core 类型；Agent Loop 不导入进程实现或 Node SDK。
`Tool.capability` 默认 `untrusted`，由宿主代码工厂赋值。仅伪造工具名称、描述或 MCP schema
不能取得 `internal`、`skill-read`、`shell` 等权限。Local 默认无审批、不创建 Bridge，
仍走现有 Shell、Patch、Memory、MCP 实现。默认配置没有新 Node/SRT 依赖。

`tools/sandbox.py` 包含生产门禁和工程 IPC 客户端；`srt_bridge.mjs` 是长驻进程协议实现；
`srt_adapter.mjs` 只提供可信安装路径、包名、固定版本和公开 API 的加载核对。
真实适配器没有接到生产入口，当前不会调用 SRT initialize 或改变机器设置。

### Bridge 协议与生命周期

- 每个请求有独立随机 RPC ID，模型 call_id 另行关联；请求顺序执行，不能并发占用一个 manager。
- 方法包括 initialize、execute、ack、cancel、shutdown；响应分 result、error、output、event。
- 子进程使用 argv 和 `shell:false`；PowerShell 命令文本仅作为受限 shell 的参数，
  不拼接给宿主 shell。保留 cmd→command 窄别名兼容、workdir 和 timeout 范围验证。
- stdout/stderr 用 base64 帧传输；Python 复用共享 head/tail 输出预算并增量解码 UTF-8。
- Patch Worker 的每个 `patch_diff` 需要宿主持久化成功后回 ack，再进入下一文件提交。
- 取消时先请求 Worker 在审计屏障退出，再做有界进程树清理；Shell 超时同样执行清理。
- 断管、错误关联 ID、无效 JSON、字段不完整的结果不能当作成功，禁止自动重试。
  派发后无法确认状态返回 `cleanup_incomplete` / `interrupted_unknown`，现有 Loop 会停止。
- Bridge owner 必须调用 close；退出时发送 shutdown，有界等待后清理进程。
  真实 SRT 初始化/异常退出后的资源清理仍需要独立集成验收。

### Patch Worker 的代码来源

Worker 路径由 Bridge 模块所在的安装目录确定，不能来自模型参数。Python 使用 `-I`，
忽略工作区/PYTHONPATH/用户 site；Worker 从自身路径定位 Nexus Patch Core。
Bridge 初始化拒绝把 Worker 或 Python executable 放在可写根目录内。
测试在目标工作区放置伪造 `nexus` 和 `sitecustomize.py`，真实 Worker 仍调用可信代码。
安装包外部 smoke 也验证了 wheel 自带 Worker 的真实文件修改与 audit/ack 往返。

Patch 算法没有改写：hunk、预检、路径约束、并发变更检测、原子替换、diff 预算均复用。
取消可能留下已提交文件；结果包含 partial 和已完成文件数量，不承诺跨文件事务回滚。
文件更新与 JSONL 追加仍非同一事务，崩溃时必须检查磁盘，不能声称 exactly-once。

## 4. HITL 与配置

策略只来自用户级 `~/.nexus/config.toml` 的新增 `[security]`，不读取仓库权限配置。
现有配置无需修改，默认 `local`。可用字段：

```toml
[security]
mode = "local"  # local / read-only / workspace-write
writable_roots = []  # 额外绝对路径；真实 backend 尚未开放
ask_tools = []       # 显式要求逐次审批的工具名称
allow_host_memory = false
allow_trusted_skills = true
```

例如 Local 下仅为 shell/patch 增加人工确认，可显式设置
`ask_tools = ["exec_command", "apply_patch"]`；这不会改变 Local 的实际 OS 权限。
切换为 read-only/workspace-write 后，shell 当前返回 backend_blocked；read-only 的 patch
在策略层即拒绝。一次性批准不会绕过 backend 门禁，也不会改变会话模式。

授权绑定 session_id、run_id、call_id、tool_name、arguments_digest、capability_scope，
另有随机 request_id 与 120 秒有效期。scope 包含模式、路由、工作区及配置的额外写路径。
在 UI 返回后重新核对调用参数与 session/run 绑定；过期、参数变化或复用旧决定均拒绝。
不保存任何可恢复的授权缓存。

交互 CLI 展示工具、脱敏参数预览和 scope，输入 allow/deny/cancel 后按 Enter。
Ctrl+C 取消；括号粘贴和执行期残留输入不能当成审批。长预览先脱敏再截断，避免截断
凭据后留下无法匹配的前缀。无交互审批回调时直接返回 approval_unavailable。

## 5. JSONL 与恢复

run_started 记录 execution_mode；configuration 记录安全配置和指纹。
新增 tool_policy、approval_requested、approval_resolved、tool_execution 事件；
ToolResult 继续通过既有 tool message 和 tool_finished 持久化。
安全审计只额外记录参数 digest，不再复制参数正文；沿用 Events 的已知凭据脱敏。

approval_resolved 必须完成现有 SessionLog append + flush，随后才记录派发并执行。
落盘失败会阻止调用；没有新增每次 fsync 的承诺。Resume 只恢复消息和执行事实：
有策略/审批记录但无 tool_execution 的待完成调用记 not_executed；存在派发记录而
缺结果则保留 interrupted_unknown。恢复不自动派发任何历史调用，也不复用 allow once。

## 6. Memory、Skills、MCP 边界

- update_plan：内部状态操作允许；没有操作系统执行能力。
- load_skill：默认使用用户安装的有界 Markdown 目录及既有路径检查，只读取、不运行脚本。
  可用 allow_trusted_skills=false 禁用；受限模式恢复时也清除当前选中/加载的 Skill 投影。
- Memory：受限模式默认拒绝。显式 allow_host_memory=true 仅使相关调用进入独立逐次审批；
  read-only 只允许申请读取，写入/删除/恢复/修改 scratchpad 均拒绝。
  workspace-write 的 Memory 写入属于明确批准的宿主服务，并不是 SRT 隔离操作。
  memory_search 始终拒绝，后台索引、依赖下载和自动 Daily/snapshot 均不启动。
- MCP：受限模式不启动/连接用户配置服务器；动态工具默认 untrusted，不因声明名称获得执行权。
  当前没有实现受限 MCP 执行保证，故明确禁用，不能声称它已经被 SRT 保护。

## 7. 验证和性能

所有证据位于 [stage4-engineering](../next/evidence/stage4-engineering/)。

| 检查 | 结果 |
| --- | --- |
| 全量 pytest（最终源码） | **879 passed, 10 skipped，68.97 s**；10 项为显式 opt-in Docker 测试 |
| Ruff 全仓库 | PASS，`ruff check . --no-cache` |
| mypy src tests | PASS，106 source files |
| uv lock --check --offline | PASS，66 packages |
| uv build --offline | PASS，wheel + sdist |
| Node 两个 .mjs 语法检查 | PASS |
| git diff --cached --check | PASS（提交前检查） |
| 源码目录外 wheel CLI/资源/真实 Worker | PASS，`wheel-final.txt` |
| SRT 0.0.81 公开接口 | 静态核对 PASS；未 import/initialize |
| Windows OS 级隔离 | Blocked / NOT RUN |
| Linux/macOS runtime | NOT RUN |
| 付费 Benchmark / live model | NOT RUN（按任务约束） |

具体失败尝试与修正见 [attempts.md](../next/evidence/stage4-engineering/attempts.md)。
首轮全量 7 个失败全部保留；原始失败日志字节保存在 `.txt.gz`，文本副本仅规范化空白。
没有删除失败尝试或将 Mock 结果记为真实隔离 PASS。

性能脚本：`python -m scripts.stage4_engineering_perf`。在同一 Windows / Python 3.12.10
环境交错运行 10 对固定命令，并使用同一 JSONL sink：

| 测量 | 中位数 | 限制 |
| --- | --- | --- |
| 原 Local Executor 直接调用 | 363.207 ms | 固定小命令，10 次 |
| Gateway + Local Executor | 361.774 ms | 固定小命令，10 次；无明显退化，不能解释为加速 |
| Mock manager 的 Node Bridge 初始化 | 49.040 ms | 5 次；不含 SRT 初始化 |
| Python Worker 启动 + patch + audit/ack | 193.828 ms | 15 次；非 OS 隔离环境 |

## 8. 后续集成验收与已知限制

1. 先解决任务书已登记的 Windows SRT 隔离验收阻塞，再单独评审和启用生产入口；
   不能通过一个环境变量或批准提示绕过阻塞。本轮没有扩展上游安全研究。
2. 在可信、不可被目标工作区写入的 Node/SRT/Python/Nexus 安装位置验证真实组合；
   测试读取/写入根目录、额外路径、网络策略、进程树、取消、异常退出和 SRT 清理。
3. 实际 SRT initialize 的配置、资源占用、启动延迟与 reset 语义仍待测试；
   当前只有静态 API 对齐和 mock-manager 协议证据。
4. 动态权限扩展与受限 MCP 尚未实现；Memory 的宿主服务授权不等价于 OS 沙箱。
5. 当前审批为最小终端交互，已做 pipe-input 测试，尚未完成用户真实终端的人工体验验收。
6. 成功测试的 tmp_path 按项目策略清理；本轮专用 wheel venv 已核对绝对路径和重解析点后删除。
   失败测试目录与历史 PoC 保留；未更改其权限。

参考：SRT 官方 [公开库 API](https://github.com/anthropic-experimental/sandbox-runtime/blob/main/src/sandbox/sandbox-manager.ts)
和本机已安装 0.0.81 的 `dist/sandbox/sandbox-manager.d.ts`。代码适配按版本锁定，
官方 main 文档只作辅助对照，不能代替固定版本真实验收。
