# Nexus V0.3 Stage 4B — Windows SRT 可用化与安全治理

日期：2026-10-26。依据本轮用户任务书，在 Stage 4A 上增量实现，保留单一手写 Agent Loop。
没有付费模型调用、Benchmark、逃逸 PoC、全局依赖安装或机器级安全设置修改。
SRT 正常 initialize/reset 会管理其自身会话授权，这是实际运行 SRT 的一部分。

## 1. 交付与基线

- 分支：`codex/nexus-v0.3-stage4-sandbox`。
- 开始 HEAD：`7ff32e55eaf02f306a4b634632818e9cd32a6b29`。
- 最终实现 Commit SHA / 报告编写时 Git HEAD：
  `33da5b5c875922a73dcbfe9f06fa9b8a82468383`。
  后续交付提交只增加本报告，不修改已验证源码、测试或证据；最终交付 HEAD 在推送回执中
  与 `git ls-remote origin refs/heads/codex/nexus-v0.3-stage4-sandbox` 核对。
- 按任务书核对提交关系后，已将远端 `codex/nexus-v0.3` 普通 fast-forward 到 Stage 3
  合并基线 `b13ee4e13b32bc60bf56cdad9eaf5461934c437e`，未 force push。
- Stage 4 不合入 V0.3/main。已有 Evaluation History 改动在基线中，本轮未修改该模块。
- 原 Stage 4A 报告、`.idea/`、旧 SRT PoC 脚本和证据均保留；未运行、未暂存旧 PoC。

## 2. 状态与边界

| 能力 | 状态 | 证据或边界 |
| --- | --- | --- |
| 生产 SRT Library 接通 | Implemented / Real Windows SRT Verified | 固定 0.0.81，实际 `srt-sandbox` 账户 |
| Read-only | Implemented / Real Windows SRT Verified | 读文件、Shell 写入拒绝、Gateway Patch 拒绝 |
| Workspace-write | Implemented / Real Windows SRT Verified | Shell/Python/pytest/Patch/本地 Git 成功，范围外写拒绝 |
| CLI /permissions 交互 | Implemented / Unit Tested / Functionally Verified | 实际 prompt_toolkit 输入处理器，未声称人工模型端到端验收 |
| 普通工具审批 | Implemented / Unit Tested | Stage 4A ask_tools 保留，不改变模式 |
| Read-only 单次 Patch 授权 | Implemented / Unit Tested / Real Windows SRT Verified | 拒绝、单次批准、不可复用、恢复 RO 均实测 |
| 任意 Shell/额外目录动态授权 | Not Implemented | 无可靠 OS 拒绝分类闭环，明确拒绝，无 Local 回退 |
| Memory/Skills/MCP/Resume | Unit Tested | 保留 Stage 4A 权限边界与恢复语义 |
| 强敌对代码安全认证 | Not Implemented / NOT RUN | 本轮是正常功能集成，不是安全攻防认证 |
| Linux/macOS 隔离 | Not Implemented / NOT RUN | 此适配器仅支持 Windows |

## 3. 实际执行链与可信安装

```text
Agent Loop → Registry → SecurityGateway
  Local → 原 Shell / Patch（不启动 Node）
  Restricted → SRTBackend → BridgeClient → 长驻 Node Bridge
    → SandboxManager.initialize / wrapWithSandboxArgv
      → 受限 PowerShell / python -I 可信 Patch Worker
```

Bridge 和 Worker 来自 Nexus 安装目录；Node、Python、SRT、Shell 必须在工作区和授权写目录之外。
自动发现 PATH 上全局 SRT launcher 的安装包，也支持用户级绝对安装路径配置。
不执行 npm 探测、不接受工作区伪造模块；固定检查包名、0.0.81、Node >=22.12。
从项目内源码/.venv 对同一项目启动受限 Worker 会明确失败，需要先安装 wheel 到外部环境。

Local 保持默认，无 Node 进程或 SRT 依赖。受限模式首次调用懒初始化、普通调用复用会话。
初始化失败、Bridge 断连和清理未知均 fail-closed。Shell 用结构化 argv、`shell:false`，
模型命令只交给 SRT 内 PowerShell，未先经宿主 Shell 执行。

正常退出调用 manager.reset；超时/取消结束 Node holder，利用 SRT 原生 held-by/Job 生命周期
终止另一账户子树，再允许下一次创建新会话，不重试已中断命令。Patch 结果未知立即结束 holder，
保留 interrupted_unknown；Conversation 即使沙箱关闭失败也继续释放 Memory、资源和 writer。
初始化单独设 600 秒上限，与固定版本 SRT 的 persistent-grant 上限对齐；工具执行预算不变。
`cleanup_confirmed` 只确认 reset 返回及 holder 退出；上游 ACL 恢复是 best-effort，
并不由该字段证明全部 ACL 已撤销。取消的清理阶段可能很慢，不能把 timeout_ms 当作整个调用的返回时限，
也不承诺用户按下取消后所有副作用瞬时停止。

### Patch Worker 的 Windows 适配

实际安装中，SRT 启动的 Python 没有收到 Bridge 写入的 stdin；另一个失败点是 Python 3.12
asyncio 事件循环所需 loopback socketpair 与当前受限网络环境不兼容。
因此采用宿主创建、SRT 只读的临时 IPC 目录：结构化请求文件、逐文件 ACK 文件。
Patch 内容不拼进 Shell。Worker 运行原 Patch Core，保留路径校验、冲突、原子替换和 partial 语义。
Core 当前只 await emit，Worker 同步完成 emit/ACK，不创建事件循环；未来若新增真正异步依赖，
Worker 明确失败，不吞掉 await。沙箱不能伪造宿主 ACK 文件。

## 4. 模式、网络与 Git

CLI `--permission-mode` 覆盖用户 `security.mode`，不写配置。支持根命令和 exec/resume/eval
子命令位置；同时出现时子命令位置覆盖根位置。未传参数沿用配置，默认 Local。
`/permissions` 显示当前模式、含义及 backend 初始化状态，支持数字、方向键、Enter、Esc。
切换仅在输入空闲时由用户发起，先关闭旧 Conversation，再建新 Conversation；历史保留到磁盘，
不复制在途状态或单次授权。`/new`、`/resume` 保留当前进程的临时模式选择。
Read-only 不给工作区写授权，另通过官方 `wrapWithSandboxArgv` 的 per-exec
`filesystem.denyWrite=[workspace]` 给每个命令施加整根写保护，由命令 holder 管理释放。
不能把该根拒绝放进 initialize：实测 Windows 的 kept-root 机制会让 WW→RO→WW 后仍拒绝写入。
最终改动没有取消只读保护或关闭 SRT，而是使用相同拒绝范围的正确生命周期。

网络 `allowed_domains` / `denied_domains` 从用户配置送入 SRT Proxy；默认列表为空即默认拒绝。
仅允许主机名或 `*.host`，URL/路径/端口/空值等错误 fail-closed。没有另建代理或安全过滤器。
域名许可不防止向获准域名发送敏感数据。

WW 不再禁止整个 `.git`；显式保护各授权根的 `.git/config`、`.git/config.worktree`、`.git/hooks`、
`.gitmodules`、`.nexus`，保留 index/refs/objects 写入。本地已初始化仓库的 add/commit 已实测。
新建仓库的 git init、嵌套仓库、Git worktree 和联网 Git 未纳入本轮真实验收；敏感配置需要宿主明确管理。
缺失的保护目标可能由 SRT 创建占位项，例如测试中 Git status 出现 `.gitmodules` 和 `.nexus`；
它们随 kept policy 保留，不应误认为模型生成的项目文件。本轮未绕开或删掉这些保护项。

## 5. HITL

普通 ask_tools 是执行前审批；批准仅放行当前工具，原 SRT 模式仍生效。
本轮新增的最小可用权限申请是 **Read-only Gateway 拒绝后的精确单次 Patch**：

1. Gateway 记录自己产生的 Patch 拒绝，绑定 session/run/call/workspace/原始参数/5 分钟有效期。
2. 模型用 `request_permissions(denied_call_id, reason)` 申请，不能替换 Patch 或添加路径。
3. UI 展示工具、原操作、目标工作区、参数摘要和仅本次范围；Allow/Deny/Cancel。
4. 审批事件先持久化，再关闭原 Read-only 会话，启动独立临时 WW SRT，只执行原 Patch。
5. 临时会话必须关闭；原对象回到 Read-only 懒初始化状态。拒绝记录已消费，不可复用或 Resume 恢复。

这是可信 Gateway policy-denial 流程，**不是任意 Shell OS-denial 的自动提权**。
普通非零退出码、模型声称权限不足、任意额外目录请求都不能取得该授权。
真实输入验收使用 pipe-input 向实际 TaskInput 输入 allow/deny，不使用 Mock SRT，不调用模型；
并非声称用户已亲自完成产品验收。

真实 HITL：`hitl-real-04.json` 记录拒绝不写、批准后唯一 patch_diff、授权不可复用，
随后新建 Read-only 会话读取该文件成功、写入被 OS 拒绝；默认网络经 Proxy 返回
`403 host is not on the allow list`。最终退出结果纳入独立证据核对。

## 6. 测试、构建与性能

原始证据位于 `docs/next/evidence/stage4b-engineering/`，失败尝试不覆盖。
最终全量 `pytest-06.txt`：**903 passed, 10 skipped，73.83 s**；10 个 skipped 为既有 opt-in Docker 测试。
此前相关 Security/Bridge/HITL 回归 **60 passed，11.92 s**；最后全量还覆盖了新增的 RO per-exec 生命周期验证。
Ruff 全仓通过；mypy **107 source files** 通过。Bridge IPC、Patch Worker、HITL、Session Resume
均在全量测试中保留；新增测试不替换旧用例。`uv build --offline` 及工作区外 wheel 安装通过，
CLI 帮助含三种权限模式，wheel 包含两个 `.mjs` 和可信 Worker。
最终 wheel SHA256：`5e5162f841b0513a90cd0e4b09817bd73c451016ca93d331cd30b9cf916afed1`。
逐文件核对 wheel 中 56 个 Python/MJS 文件与当前源码一致（仅忽略 CRLF/LF 差异）。

`scripts/stage4b_verify_evidence.py` 独立读取真实结果并核对预期，
`verification-final.json` 为 **PASS**，不依赖验收脚本的退出码推断成功。

| 实机项目 | 结果与证据 |
| --- | --- |
| 真实受限账户、工作区写、范围外拒绝 | PASS，SRT 12 |
| Python/pytest、Patch + patch_diff/ACK、宿主读取产物 | PASS，SRT 12 |
| Git add/commit，config/hooks 写入拒绝 | PASS，SRT 12 |
| 同目录 WW→RO→WW，RO 读成功/写拒绝，恢复 WW 可写 | PASS，SRT 12 |
| 允许域名 HTTPS | PASS，SRT 12，Python urllib 返回 200 |
| 默认网络拒绝 | PASS，HITL 04，SRT Proxy 返回 403 |
| HITL deny/allow once/不可复用/恢复 RO | PASS，HITL 04 |
| 超时、取消、关闭 holder、随后重新调用 | PASS，SRT 10；12 明确跳过重复 stop 测试 |
| 初始化缺依赖不回退 | Unit Tested，缺 Node/Shell；早期真实错误尝试均未回退 |
| 最终进程清理 | PASS，`process-cleanup-final.json`：0 残留 |
| 配置额外写根、denied_domains 覆盖、联网 Git | Implemented，但本轮未做独立实机验收 |

HITL 04 和 SRT 10 是对应修复阶段的真实证据；最终 RO deny 生命周期由 SRT 12 验证，
最终审批日志精简由全量测试验证，不将历史 JSON 改写为最终实现版本。

已测 Local 对照每组 5 次：原 executor 中位 379.963 ms，经 Gateway 384.775 ms，
差值 4.812 ms（约 1.27%）。是小样本单机观测，不能据此断言普遍开销比例；没有新增模型调用。

`srt-attempt-10.json` 记录首次 WW 调用约 59.08 s，复用 Shell 通常 0.6–0.8 s，
pytest 约 1.38 s，Patch 约 0.391 s，白名单 Python HTTPS 约 3.93 s。
首次总时长包含安装发现/初始化/首个命令，不能冒称纯 Node 启动时间；同条 execution duration
约 0.625 s，余量主要为初始化。该尝试 1.5 s 命令超时的总返回时间约 58.86 s，
因为需等待 SRT 清理。取消后重建会话约 58.27 s。当前初始化/重置明显慢，尚未优化。
后续 HITL 04 首次 Read-only 总计 **124.55 s**、批准后的独立 Patch 流程 **244.75 s**
（含关闭原会话、创建临时会话和关闭临时会话），恢复后的首个 Read-only 调用 **123.62 s**。
旧 120 秒初始化预算因此确实不够；调整后该流程完成，不把超时尝试藏入平均数。
最终 SRT 12：首个 WW 调用 **57.99 s**、复用 Patch **380.5 ms**、pytest **1.363 s**、
Git **787.5 ms**、白名单 HTTPS **3.659 s**，切回 RO 首次 **59.07 s**、再回 WW 首次 **60.19 s**。
没有用不同轮次的时差推导优化收益。

## 7. 保留的权限边界与平台限制

- 受限模式不连接 MCP、不后台启动 QMD/Memory 索引、不自动写 Daily/Snapshot。
  host Memory 默认拒绝；显式启用后仍逐次审批，RO 禁止 Memory 写入。Skills 不提升 Tool capability。
- Resume 不重放已执行调用、不恢复一次性授权、不从历史恢复更宽策略；未知结果仍保留未知。
- Windows SRT 0.0.81 使用 write-restricted token；官方说明 Schannel 客户端不可用，
  System32 curl/Invoke-WebRequest/.NET 等不能据域名获准就假定可用。实测 Python/OpenSSL HTTPS 成功。
- 上游 keep-write-grants 在 reset 后保留；写权限按本会话 root SID 限制，**读取不是同样隔离**，
  其他 SRT 会话可能读取曾授权的 kept root。reset 不等于撤销所有磁盘授权。
- 上述平台事实依据本机固定 0.0.81 包 README 的 Windows Sandbox / Kept Write Grants / Schannel
  说明，以及 `dist/sandbox/sandbox-manager.js` 的 reset 实现；未据此声称 Windows 安全问题已解决。
- 上游既有 profile-read grant、WFP 账户边界及系统服务委托等限制仍存在；本轮不扩大 profile grant，
  不修改上游、机器账户、WFP 初始化或 ACL 来规避失败，不声称完成漏洞修复。
- 狭窄 allowRead 不能保证 Windows 对某些用户目录祖先的遍历可用；早期 AppData 夹具失败已保留。
  测试夹具 `mkdtemp()` 的 owner-only ACL 也会让 SRT 新文件对宿主不可读。最终使用普通新建、
  继承现有父目录权限的独立测试目录，未修改 ACL。
- Windows 外平台、任意 MCP 受限执行、任意 OS-denial scoped escalation、强安全认证未交付。

## 8. 用户手动验收

2026-10-26 后续人工验收发现系统 Windows PowerShell 初始化 P0；本报告的历史成功结果
不覆盖该安装路径。修复、复验、初始化分阶段计时和新候选入口见
[人工验收问题记录](stage4b-manual-acceptance-issues.md)。以下旧候选路径仅保留为历史记录。

先在工作区之外安装本轮 wheel；保留既有全局 SRT 0.0.81 初始化，不需要在 Nexus 仓库 npm install。
本轮独立候选安装位于：
`D:\WorkSpace\nexus-stage4b-install-929ee5906c6e42f3a3084173fca0dd03`。
原全局 `nexus` 未被覆盖；可在 PowerShell 使用明确路径，避免测到旧版：

```powershell
$nexusStage4 = 'D:\WorkSpace\nexus-stage4b-install-929ee5906c6e42f3a3084173fca0dd03\Scripts\nexus.exe'
& $nexusStage4 --permission-mode local
& $nexusStage4 --permission-mode read-only
& $nexusStage4 --permission-mode workspace-write
& $nexusStage4 exec '读取并说明当前项目' --permission-mode read-only
```

使用已有模型配置；上述启动交互可能在发送任务后调用用户模型，本轮工程验收未执行付费调用。
建议先在普通新建的独立小项目目录操作。不要把候选 Nexus 安装目录放进被授权写的工作区。

用户级 `~/.nexus/config.toml` 可加入/更新下列字段（不要删除已有 model 配置）：

```toml
[security]
mode = "local"
allowed_domains = ["example.com"] # 示例：只填写你主动允许访问的域名
denied_domains = []
# writable_roots = ['D:\YourExplicitExtraDirectory']
# ask_tools = ["exec_command"]
# node_path = 'D:\software\nvm4w\nodejs\node.exe'
# srt_package = 'D:\software\node\node_global\node_modules\@anthropic-ai\sandbox-runtime'
```

1. Local 执行普通读写，默认不弹审批；输入 `/permissions` 查看并选择模式。
2. Read-only 读取项目/运行探索命令；尝试 Shell 写入应拒绝。
3. Read-only 请求用 apply_patch 新增小文件；工具拒绝后申请 request_permissions。
   输入 deny 应不写；重新申请后输入 allow 仅执行审批所展示的 Patch，随后仍是 Read-only。
4. WW 创建/修改文件、运行 pytest，在已有测试仓库执行本地 git add/commit；范围外写入应失败。
5. 首次初始化本机实测约 1–2 分钟，单次 Patch 授权完整流程约 4 分钟，初始化最多等待 600 秒。
   不要把“正在清理”误当作已安全结束；未知清理会明确报错。
6. 再次 `/permissions` 查看 backend 状态；切换新会话，旧历史可用 `/resume` 查看。

## 9. 失败与临时产物

失败尝试及最终临时目录盘点见证据目录 README。遇到权限拒绝的临时内容保留并报告，
不修改 ACL 强制删除；候选安装保留供用户验收。已知不可用项均在上文列出，未以 Mock 通过冒充真实成功。
SRT 11 的旧夹具保留了当时错误配置产生的根写拒绝；未运行 acl forget/recover 去清除它。
不要把旧失败夹具复用为最终产品验收目录。
`temp-cleanup.json` 有 8 条已清理记录（通过测试目录、弃用安装及已归档的孤立 ACK 目录）；
`temp-retained-final.json` 列出保留的失败/功能检查夹具、缓存和 D: 候选安装。
