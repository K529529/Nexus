# Stage 4B 人工验收问题记录

## P0-01：系统 PowerShell 首次 SRT 初始化失败

2026-10-26，分支 `codex/nexus-v0.3-stage4-sandbox`，基线 `76c36d1`。
本轮只修复该阻塞及回归暴露的顺序调用竞态，不合并到 V0.3 总分支。

**状态：P0 修复与功能复验 PASS。** 独立核验器验证 11 份真实 SRT 初始化策略均未向系统
目录提交 ACL 根，系统 PowerShell/System32/Program Files DACL 前后完全一致，测试令牌
均非管理员，本轮 Bridge/原生子进程未残留。旧授权恢复警告和上游 reset 超时保留为已知限制。

用户原工作区 `D:\WorkSpace\nexus-stage4-manual` 已用最终 wheel 实测，首次
`whoami; Get-Location` 成功：身份 `srt-sandbox`、目录正确、stderr 为空，总计 74.533 s。
其中 SRT initialize 73.168 s、原生 ACL grant 72.771 s、命令执行 1.126 s。
关闭耗时 60.804 s，包含上游 acl restore 超时；此事实不改变“首次初始化与执行成功”的结论，
也不被正常退出掩盖。完整核验见 `verification.json` 与 `timing-summary.json`。

### 现象与根因

用户在 `D:\WorkSpace\nexus-stage4-manual` 使用 Workspace-write，首次 exec 返回
`initialization_failed`。原始工具错误见
[original-failure-tool.json](../next/evidence/stage4b-system-powershell-p0/original-failure-tool.json)。
19 个 ACL 目标中，系统 PowerShell 目录分别以 ReadOnly 和 WriteDeny 进入同批操作，
两项均因 `SetNamedSecurityInfoW / ACCESS_DENIED (0x5)` 失败，整批回滚。

完整调用链：`SRTBackend.installation()` 将 Python venv、base prefix、Nexus 包、shell 父目录
列为 read_roots → Bridge canonical realpath → 同时加入 allowRead / denyWrite →
SRT 0.0.81 `computeWindowsFsAccessSet()` 展开、排序 →
`grantWindowsAclPersistent()` 通过 stdin 一次提交原生 `acl grant --persistent`。
allowRead 和 denyWrite 都会写 DACL，不能把“读取系统程序”误认为“必须给系统目录新增 ACL”。

PowerShell 安装目录归 TrustedInstaller 所有，已有 BUILTIN\Users 读取/执行权限；普通 Archer
无法修改该目录 DACL。此前 Stage 4B 真实验收使用 Codex 附带的 pwsh 路径，未覆盖系统
Windows PowerShell，因此漏掉了这个初始化路径问题。

### 修复边界

- Bridge 统一识别 SystemRoot/windir、ProgramFiles、ProgramFiles(x86)、ProgramW6432，
  使用 canonical path 和祖先关系过滤系统安装目录，覆盖 **allowRead 和 denyWrite 两处**。
- worker site-packages、Python exe 即使不在 readRoots，也经过同一过滤；普通非系统 runtime
  的只读授权与写拒绝保留。没有删除 Python/Worker 保护来缩短耗时。
- workspace、显式 writable roots、IPC 位于系统目录内或覆盖系统目录时，初始化前拒绝；
  runtime root 是系统目录祖先（如 `C:\`）时同样拒绝，避免从父目录间接授予权限。
- 工作区、Git 配置/hooks、`.gitmodules`、`.nexus`、IPC 保护和逐命令 RO deny 不变。
  系统程序读取依赖原有 OS 权限，写隔离仍依赖 SRT write-restricted token；没有 Local fallback。
  若某个自定义系统安装本身不允许 sandbox 用户读取，后续执行按 OS 权限失败；不会自动
  提权或给整个系统树补授权。本轮真实执行覆盖系统 PowerShell，系统目录内 Python/Worker
  的过滤由策略回归验证，不冒称已安装并运行这些额外布局。
- SRT 的其他初始化输入是其自有 CA 目录和 credential deny；本适配器未新增这些路径或关闭
  filesystem/network 检查。Node/SRT 自身是宿主桥接程序，不需要新增系统安装目录授权。
- 不修改上游包、不运行 windows-install、acl forget/recover 或旧安全 PoC；不使用管理员令牌，
  不更改 System32/Program Files ACL。

### 验证与计时

原始证据目录：[stage4b-system-powershell-p0](../next/evidence/stage4b-system-powershell-p0/)。

- 最终全量：**910 passed / 10 skipped**（显式 opt-in Docker），81.02 s。
- 针对性：20 passed；新增 6 个系统路径用例及 1 个延迟 IPC 清理用例。
- Ruff 通过；mypy 108 source files 通过。wheel 与外部候选 Bridge SHA256 一致。
- 最终真实 SRT 已验证：系统 PowerShell 的 WW 读写、Python pytest、Patch 后立即 Git add/commit、
  Git 配置/hooks 拒写、工作区外拒写、白名单 HTTPS、同一工作区 WW→RO→WW。
  原工作区复验结果见下方交付状态。
- 原首轮真实 RO 读取/拒写/拒 Patch 通过；Git `bridge_busy` 失败保留。
  本轮真实 timeout/cancel 专项未重复运行，自动化回归覆盖仍通过；没有运行旧安全 PoC。

计时由进程级 `NODE_OPTIONS --import` 观察现有 SRT 方法和原生子进程，未修改上游源码、
未禁用 WFP/credential 检查、未改生产根策略。下面是单次串行实测（秒），不是多轮均值：

| 根组合 | ACL grant | WFP verify | SRT initialize | reset |
| --- | ---: | ---: | ---: | ---: |
| 原首轮生产完整根，RO | 87.101 | 0.310 | 87.517 | 60.832 |
| 原首轮生产完整根，WW | 127.086 | 0.312 | 127.500 | 60.748 |
| 对照：shell-only 候选 | 72.145 | 0.299 | 72.587 | 2.387 |
| 对照：shell + venv | 73.379 | 0.293 | 73.773 | 60.686 |
| 对照：shell + base Python | 70.471 | 0.275 | 70.838 | 2.396 |
| 最终版本首个完整根 WW | 71.917 | 0.274 | 72.288 | 见完整 trace |
| 最终版本，用户原工作区 WW | 72.771 | 0.288 | 73.168 | 60.795 |

三个对照均保留 workspace、IPC、worker site-packages、Python exe、Git 特殊路径保护；
shell 候选会被系统目录过滤器移除。不同之处仅是待测 read_roots 候选及其相应 denyWrite。
**对照只 initialize/close，不执行任何 shell/worker，不是削弱后的生产运行模式。**

初始化慢的已定位原因是原生 `acl grant --persistent` 批处理，而非 PowerShell 启动或模型调用。
模块 import 约 0.15–0.20 s；user/wfp status 单次通常 0.03–0.07 s；WFP verify 约 0.3 s；
其余 initialize 的差额约 0.1 s，包含状态校验、配置/CA/proxy 处理，未伪造更细内部计时。
SRT 源码明确首个 root grant 遍历整棵树，预算为 600 s；本机 base Python 有 33,437 文件/
3,131 目录，venv 有 4,780 文件/536 目录，Nexus 包有 73 文件/10 目录。

但仅 shell 候选的对照也约 73 s，最终完整根也约 72 s，因此不能用目录数量单独解释全部
耗时，更不能把先后轮次差异说成提速收益。各组共享既有持久授权数据库、旧失败 holder、
OS 缓存，`acl grant` 时间包含原生授权协调/恢复和根处理。本次测到的是**每组批处理时间**，
不是每个 ACE/文件的时间；没有清库或修改 System32 来制造“干净”性能数据。

另一个已定位的耗时来源是 reset 的同步 `acl restore`：多次恰好耗尽上游 60 s 预算，
trace 保留 `ETIMEDOUT`。`acl revoke`、`acl restore` 各自耗时均有记录。上游捕获该异常，
所以正常 close 不能证明所有磁盘授权已回滚。P0 的新策略错误与该既有回收问题分别记录。

真实回归第一轮另发现 Patch→Git 的 `bridge_busy`：Patch terminal result 在 IPC unlink
完成之前已经送给 Python，后续顺序调用被仍然置位的 executing 拒绝。保留
`functional-01.json` 的失败，不把 runner 退出码当作通过。窄修复为 execute 先完成 finally
清理、释放 busy gate，再发送 terminal frame；新增故意延迟 unlink 的确定性回归测试。
最终版本在 `functional-final.json` 重新验证立即 Patch→Git 和 WW→RO→WW。
初始化根对照仅初始化、关闭，从不运行命令；它测量的是相同 ACL 策略的早期候选，未包含
后来这个结果发送顺序修复，不冒称所有历史证据来自同一 wheel。

### 历史残留与范围限制

本机已有失败 holder 46652 的 System32 WriteDeny 记录。SRT 自动恢复时仍可能输出
`recover grant ... ACCESS_DENIED; rows kept for retry`；这不是新策略再次申请该目录。
本轮保留该事实，不通过修改系统 ACL 或清空 SRT 授权数据库消除警告。
上游 reset 是 best effort；Node 正常退出不等于所有持久授权已撤销。
本轮结果不构成 OS 安全认证，也不把初始化对照配置作为可执行生产策略。

### 人工复验入口

本轮候选独立安装：`D:\WorkSpace\nexus-stage4b-p0-install-20261026`。
从测试工作区使用显式路径，避免运行旧的全局 nexus：

```powershell
Set-Location D:\WorkSpace\nexus-stage4-manual
& D:\WorkSpace\nexus-stage4b-p0-install-20261026\Scripts\nexus.exe --permission-mode workspace-write
```

原全局安装与旧候选没有被覆盖；发送交互任务会使用用户的模型配置。

本轮 4 个普通 pytest 专用临时目录均已核对绝对路径、reparse 点并清理，记录在
`test-temp-*.json`。真实 SRT 夹具与候选安装为复核证据保留，具体路径和用途见
`retained-artifacts.json`；不修改权限来强行清理已有 SRT 持久状态。初次测试遇到的旧
`pytest-of-Archer` 拒绝访问目录不属于本轮创建，未改动。
