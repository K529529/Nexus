# Stage 4 Windows Native Hardening

日期：2026-10-26。分支：`codex/nexus-v0.3-stage4-sandbox`。
基线：`8eb239967159e27cb3d7c37b99ce0cafaf8b2ee7`。
最终实现 / 独立安装包对应提交：**`c2802cf5aed5e9e0c51ee43bce720060c0dbea71`**。
后续报告与证据提交不改变该候选包代码；交付 HEAD 可由 `git rev-parse HEAD` 查询。

**P1 首次持久 ACL 授权已实现；P0 Python 0700 未解决，按任务书停止条件终止扩大底层研究。**
这次没有达到“正常 Python 开发 / pytest”的最终验收标准，不是 Production Ready，
也不代表用户人工验收完成。Linux Docker 阶段未开始，没有合并 V0.3/main。

## P0：精确冲突与停止结论

实测 Python 为 3.12.10。普通 mkdir、普通 TEMP 文件读写正常；0700 创建的目录缺少
受限 Token 所需的 Capability ACE。原始验收的 6 个失败继续保留，没有改成 xfail 或 PASS。

[CPython v3.12.10 的 os_mkdir_impl](https://github.com/python/cpython/blob/v3.12.10/Modules/posixmodule.c)
在 Windows 且 mode=0700 时，向 CreateDirectoryW 显式提供以下安全描述符：

```text
D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;OW)
```

`P` 表示 protected DACL；SYSTEM、Administrators、OWNER RIGHTS 的 ACE 不包括 Nexus 的
workspace / TEMP capability。Pi 的 WRITE_RESTRICTED Token 在写访问时另做 restricting-SID
检查，因此“当前用户是目录 owner”并不等于当前 restricted token 能通过此写检查。
[Python 文档](https://docs.python.org/3.12/library/os.html#os.mkdir)明确记载 Windows 从 3.12.4
开始特殊处理 0700；其他 Python 版本本轮没有实测。

任务书原文程序已原样写入 `requested.py`，以普通受限 Python 子进程执行。它更早就在
`target.mkdir(mode=0o700)` 失败：外层 TemporaryDirectory 已创建 0700 根，受限进程不能在
其中创建子目录；随后 Python 自身的 cleanup 也报错。宿主正常关闭会话后的 TEMP 清理通过。
完整 traceback 保留在 `targeted-wheel-01.json`，未改写原文或隐藏异常。

| 考察方向 | 本轮结论 |
| --- | --- |
| 给私有 TEMP 父目录增加可继承 capability | 显式 protected DACL 阻断继承，当前机制不能覆盖它 |
| 修改 Token Default DACL | Pi 已为匿名管道等默认对象配置；不能覆盖调用者显式提供的 DACL |
| 把当前用户 / 更宽 SID 加入 restricting list | 不具备目录范围约束，可能使宿主既有写权限重新通过；没有安全证明，不实施 |
| 把 capability 设为 Token 默认 owner | 默认 owner 须是 Token 中符合条件的 user/group，不能把任意 restricting SID 直接当 owner |
| 监听后修改新目录 ACL | 存在创建到修复的竞态、扩大可信写代理范围；不能保证普通子进程语义，不引入 |
| CPython hook、旧版本绕过、Local 执行 | 不满足任务约束，不采用 |

上述推论依据微软的 [新对象 DACL 选择规则](https://learn.microsoft.com/en-us/windows/win32/secauthz/dacl-for-a-new-object)、
[CreateRestrictedToken / WRITE_RESTRICTED](https://learn.microsoft.com/en-us/windows/win32/api/securitybaseapi/nf-securitybaseapi-createrestrictedtoken)
与 [TOKEN_OWNER](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-token_owner)。
它是对**当前 Pi 机制及本轮约束内局部适配**的阻塞判断，不声称所有 Windows 隔离方案均不可能。

重新检查 [Pi 上游](https://github.com/yandy/pi-packages/tree/49271fada9814ede5687fcdcc8188872887f0636/pi-sandbox)：
本轮检查时 main 仍为已固定提交 `49271fada9814ede5687fcdcc8188872887f0636`，没有可直接复用的
0700 修复。继续保留 MIT / DeepSeek 归属。没有修改 Token、SID、Job、ACL 原语，也没有运行
扩大 SID、修改系统 ACL 的实验或旧 SRT PoC。

## P1：首次授权与配置边界

启动 `--permission-mode workspace-write` 和 `/permissions → Workspace-write` 都先经过
可信宿主交互。界面显示 canonical workspace、持久 capability ACE / delete-child deny /
Low Integrity label 的影响，以及 `Network: unrestricted / Windows partial enforcement`。

- `A` 允许并保存；`D`、Enter、Esc、Ctrl-C、Ctrl-D 拒绝。默认 Enter 不授权。
- 启动拒绝时不创建 Conversation；模式切换拒绝或保存失败时保留当前会话。
- 用户批准的是持久 ACL 变更，明确区别于 Tool Call 的 HITL Allow once。
- 保存到用户 `~/.nexus/config.toml` 的 `security.windows_acl_roots`，只加入当前精确根。
  不更改默认 mode；切换仍是 session-only。已有精确 TOML 授权继续有效。
- 使用 OS writer lock、读取最新 TOML、保留注释与其他配置、写前复核、fsync + atomic replace。
  写入失败不覆盖原配置；忙锁和非法 TOML 均 Fail-closed。
- 拒绝系统目录、Home 根、共享 TEMP 根、敏感宿主状态根及风险祖先。
  检查 root / config 的 Symlink、Junction 和祖先；旧批准路径不会因后来成为 Junction 而
  转化成另一个目标目录的授权。已删除的无关历史根不阻塞当前有效 workspace。
- 用户配置必须在 workspace、extra writable roots、已批准写根之外；workspace 内模型可写
  TOML 不能建立授权。非交互缺授权时直接给出交互操作指引。
- Extra roots 仍需用户显式 TOML 授权；首次弹窗不会连带批准它们。RO 不要求 workspace 写授权。
- 同意只保存授权，不立即修改 ACL。Backend 在实际工具调用时初始化，使用现有 Pi Runner。
  同意不能补足 WRITE_DAC / WRITE_OWNER；失败返回清晰诊断，不提权、不修改 owner、不回退 Local。

改动入口为 `app/acl_consent.py`、`app/cli.py`、`app/config.py`、`app/tui.py`、
`tools/windows_native.py`；工具描述同步去除过时的 SRT 名称。
Single Agent Loop / Gateway / Registry 执行架构没有改变。

## 测试与证据

所有实际 ACL 测试只涉及本轮新建、独立的专用目录。测试进程是普通 Archer 用户；工具权限
提升仅允许访问这些测试目录，不是 Windows UAC / Administrator 提权。没有付费模型请求。

证据目录：[stage4-windows-hardening](../next/evidence/stage4-windows-hardening/)。

| 验证 | 结果 / 证据 |
| --- | --- |
| 完整 Python 回归 | **949 passed / 10 skipped**；`pytest-full-02.log`，skip 均为 opt-in Docker |
| Ruff / mypy | PASS；112 个源文件类型检查，`ruff.txt` / `mypy.txt` |
| Pi Runner 单元回归 | **68 passed / 1 skipped**；`pi-unit.log`，没有运行修改整个宿主 TEMP 的上游 E2E |
| 原始 Native 实机矩阵，独立 wheel | **50 PASS / 6 FAIL**；`native-wheel-01.json`，与基线相同的 Python 阻塞 |
| 新专项，源码 / 独立 wheel | 各 **20 PASS / 4 FAIL**；`targeted-01.json` / `targeted-wheel-01.json` |
| 缺失真实 ACL 修改权限 | PASS；`missing-rights.json`：exit 127、WRITE_DAC / WRITE_OWNER 诊断、命令未执行，icacls 前后输出一致 |
| Wheel / 独立安装 | PASS；`wheel-build.log` / `wheel-install.log` / `wheel-manifest.json` |
| Local 行为、依赖与小样本耗时 | PASS；`local-baseline.json` / `local-candidate.json` |

原始矩阵通过项包括系统 PowerShell FullLanguage / Unicode / Low Token，环境密钥过滤，
RO workspace 写拒绝，WW Shell / Patch / Git，工作区外 create/update/delete 拒绝，
HITL 实际 Patch 一次授权及禁止复用，JSONL 审计，Timeout/Cancel 子孙清理，Bridge crash
后 Fail-closed，TEMP 子 Junction 不跟随，正常 TEMP 清理，系统保护目录 ACL 前后一致。
这些是限定测试夹具的实测，不能扩展为任意宿主 ACL 均严格隔离的宣称。

新增专项通过真实 TUI 输入管道按 A / D / Esc，验证拒绝不写配置、不改 ACL，批准后精确保存且
重启重用；两种模式各启动两个真实 Native 会话，验证独立 TEMP、跨会话写拒绝、关闭一个
不破坏另一个、Python 异常后 workspace 边界仍符合模式。四项 FAIL 是两种模式下原文
TemporaryDirectory 程序、pytest tmp_path / tmp_path_factory。不是 Mock 隔离验收。
pytest 测试函数同时使用两种 fixture；失败发生在 fixture setup，不能称其中任何一个已兼容。

单元交互还覆盖 `/permissions` 同意前旧会话不关闭、拒绝保留模式、同意后新会话且初始化
保持惰性、原子保存失败、锁竞争、配置来源、旧根 Junction 替换等。自动按键不等于用户亲验。

### 保留的失败尝试

- `pytest-full-01.log`：948 PASS / 1 FAIL / 10 SKIP；新增测试误断言退出后 backend 仍为
  not_initialized，实际正确为 closed。修正为同时检查构建时未初始化、退出后 closed。
- `pytest-consent-01.log`：20 PASS / 1 FAIL，同一生命周期断言。修复测试后完整回归通过。
- 一次未设置独立 TEMP 的定向 pytest 在已有 `pytest-of-Archer` setup 阶段 AccessDenied，
  未执行测试体。没有修复该既有目录 ACL；改用本轮短且独占的系统 TEMP 父目录。
- 早期 mypy 检出测试间接导入和 Optional backend 类型错误，已改为直接导入与类型收窄。
- `targeted-01`、`targeted-wheel-01` 和 `native-wheel-01` 的所有 P0 FAIL 始终保留。

## 性能与隔离限制

Native wheel 首次 RO Shell **1097.42 ms**，首次 WW Shell **1171.22 ms**；三次热 Shell
为 **1007.44 / 1252.19 / 1031.53 ms**。与专项双会话测试并行运行，存在系统负载影响，
不用于宣称性能提升。Win32 原语没改，基线分步骤 ACL / Token / PowerShell 耗时分析仍见
[上一轮报告](stage4-windows-native-acceptance.md#性能)。未通过删减隔离检查提速。

Local 测试在 Native 测试结束后顺序比较两个独立安装包，同一 PowerShell 命令各三次：

| 项目 | 基线安装包 | 本轮候选 |
| --- | ---: | ---: |
| 三次简单 Shell（ms） | 1133.41 / 828.24 / 777.67 | 898.38 / 808.81 / 786.67 |
| Patch（ms） | 3.16 | 2.92 |

样本很小，不做统计改善结论。两边 Shell 写文件、Patch 内容验证均通过；PATH 不包含 Node，
`gateway.backend is None`，Native 模块未导入。Local 原执行器未改。

Windows 仍是 **partial enforcement**：读访问沿用宿主 ACL，网络不隔离；Pi 共享的
Everyone / logon SID 在特定既有 ACL 下仍有环境权限限制，不能描述为完整容器。
拒绝旧网络白名单/黑名单配置，受限子进程过滤模型凭据，MCP 受限路由不变。
路径检查防止已存在的链接与授权重定向；未新增能对抗所有宿主并发路径替换的内核机制。

## 人工候选入口

安装路径：`D:\WorkSpace\nexus-native-hardening-install-20261026`。
代码提交：`c2802cf5aed5e9e0c51ee43bce720060c0dbea71`。
安装后用 `python -I` 实测，manifest 对比关键源文件哈希并列出随包发布的 13 个 Pi 文件。
此前基线安装目录未覆盖。

本轮已新建人工目录 `D:\WorkSpace\nexus-native-hardening-manual-20261026\workspace`，
只为新父目录授予当前用户 FullControl，尚未授予 Native capability 或 Low label。
含任务书原文 `requested.py` 和双 fixture 的 `test_private.py`。
没有改真实用户 `~/.nexus/config.toml`，没有预先批准该 workspace。

现有用户配置没有 `[security]` 运行时配置。首次使用候选前，在用户配置中加入以下内容
（若已有该表则合并字段，不创建重复表；保留原模型配置）：

```toml
[security]
windows_koffi = 'D:\WorkSpace\nexus-native-runtime-20261026\node_modules\koffi'
```

这是一次性的已安装 runtime 定位配置；**不用手写 windows_acl_roots**。
模型 API Key 仍按现有用户配置从环境读取。本轮自动测试没有调用模型；以下交互任务若交由
Agent 执行会使用用户正常模型配置。

在普通 PowerShell 中启动（不要管理员启动）：

```powershell
Set-Location 'D:\WorkSpace\nexus-native-hardening-manual-20261026\workspace'
$candidate = 'D:\WorkSpace\nexus-native-hardening-install-20261026\Scripts\nexus.exe'
& $candidate --permission-mode workspace-write
```

1. 首次确认应显示上述完整 workspace。先按 D：退出且不执行命令。重启按 A：保存精确根，
   之后重启不应再次询问。也可从 Local 启动后 `/permissions` 选择 Workspace-write。
2. WW 任务：用系统 Windows PowerShell 创建并读取 `ww.txt`，用 apply_patch 修改它，
   执行 Git init/add/commit（可在命令中设置仅本次提交的测试作者）。不要操作真实项目。
3. `/permissions` 查看模式 / Backend 状态，应明确 Network: unrestricted 与 partial
   enforcement；切到 Read-only 会关闭旧 Sandbox、开启新会话，不要求 workspace 写授权。
4. RO 任务：读取 `ww.txt`，执行 `$ExecutionContext.SessionState.LanguageMode` 应为
   FullLanguage；在 `$env:TEMP` 写普通文件应成功；尝试修改 workspace 文件应拒绝。
   apply_patch 的一次性审批可单独测试，拒绝后不变，批准后仍维持 RO 模式。
5. Python 兼容检查：请让 Agent 原样运行下面两个命令。**当前预期仍为 FAIL**，不得改
   mode=0700、切 Local、隐藏异常来把它判成通过。

```powershell
& 'D:\WorkSpace\nexus-native-hardening-install-20261026\Scripts\python.exe' requested.py
& 'D:\WorkSpace\nexus-native-hardening-install-20261026\Scripts\python.exe' -m pytest -q -p no:cacheprovider test_private.py
```

6. `/permissions` 切 Local 会结束受限会话；`/exit` 正常退出并清理该会话 TEMP。
   强杀与正常退出不同，可能遗留已报告的独占 TEMP。

## 残留、撤销与后续门槛

正常会话 TEMP 清理通过。强杀测试保留的 TEMP 为
`C:\Users\Archer\AppData\Local\Temp\nexus-native-temp-ofgxJn`，后续不会复用它。
本轮 P0 失败夹具、缺权限夹具、失败单测目录记录在 `retained-artifacts.json`。
已通过的完整 pytest 独占父目录与 Local 两个专用根核对绝对路径、无 reparse 后清理，
记录见 `cleanup.json`。原 SRT 证据和无关未跟踪文件未动。

Workspace capability ACE / Low label 是持久修改，退出不回滚；删除用户配置中的授权条目
只撤销 Nexus 后续使用许可，**不会恢复已有 ACL**。不自动反向编辑 ACL、修改 owner 或
扩展宿主 TEMP 权限。专用实验目录和崩溃残留仅在确认没有活动会话、用途已结束、路径及
链接状态无误后清理；遇到拒绝保留并报告。

**P0 仍阻塞 Stage 4 Windows 最终验收。本轮可交付的是 P1 与可信证据；没有满足 Python
开发兼容目标，不能据此启动 Linux 后续阶段或宣布用户验收完成。**
