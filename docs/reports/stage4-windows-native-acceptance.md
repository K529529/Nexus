# Stage 4 Windows Native：实现与真实验收记录

日期：2026-10-26。分支 `codex/nexus-v0.3-stage4-sandbox`；基线提交
`f355849928d7c9842a991f546afbef8b1a94b8f1`。本增量按用户授权作为 Stage 4 开发候选交付，
保留全部失败记录；不合并 V0.3。
**结论：开发候选，未满足全部验收条件，不是 Production Ready。Linux 阶段未开始。**

## 已实现的边界

固定复用 Pi 1.8.0，提交 `49271fada9814ede5687fcdcc8188872887f0636` 的 Win32 Runner。
MIT 和 DeepSeek 归属随 wheel 发布。Token/ACL/Job primitives 没有重写；适配项是可信 Koffi
位置、跨会话 ACL 锁、私有 TEMP 的 RO 写 Capability、父 Bridge 生命周期 guard。

两个用户确认的语义已落实：

- **Network: unrestricted / Windows partial enforcement**；旧网络域名配置显式报错。
  子进程只继承必要 OS 环境，过滤模型 API Key、配置指定的密钥名称、NODE_OPTIONS、
  PYTHONPATH、代理凭据变量；不继承不兼容的外层 PowerShell 7 PSModulePath。
- RO 保护 workspace，允许会话独占 TEMP。整个宿主 TEMP 不授权；WW 仅授权用户精确批准的
  workspace/extra roots 和私有 TEMP；所有读权限依赖现有 OS ACL，不申请系统目录 read grants。

Local 原执行器、Gateway/HITL、JSONL、Resume、Registry、Single Agent Loop 保留。
Native 共用既有 IPC/ACK transport，不加载 SRT 包，不启动 srt-win。系统目录、home 根、
共享 TEMP 根及敏感宿主状态目录拒绝作为授权根。安装必须在写根外。未自动合并 V0.3。

## 真实验收

所有 OS 测试在普通用户 Archer 下运行，未提权为 Administrator。每次新建独立目录，仅给
这个新目录当前用户 FullControl（Pi 设置 Low label 要求 WRITE_OWNER），不修复已有用户目录。
测试脚本 `scripts/stage4_windows_native_acceptance.py`；无模型请求、无旧 SRT PoC。

最终 wheel 运行使用 `D:\WorkSpace\nexus-native-install-20261026\Scripts\python.exe -I`，
避免通过 PYTHONPATH/当前源码冒充安装包。Koffi 3.3.3 固定安装在
`D:\WorkSpace\nexus-native-runtime-20261026\node_modules\koffi`。
wheel 包含 16 个 Native 源文件/归属文件，见 `wheel-manifest.json`。

证据目录：[stage4-windows-native](../next/evidence/stage4-windows-native/)。
`acceptance-wheel-01.json` 共 **56 项记录，50 PASS、6 FAIL**；6 个失败是同一个 Python
兼容问题在 RO、WW、再切回 RO 下的 pytest 与 mkdir0700 两种表现，不能合并成“验收通过”。

| 检查 | 结果 |
| --- | --- |
| 系统 PowerShell FullLanguage、中文 UTF-8、真实 Low token | PASS |
| Python 执行、私有 TEMP 普通读写、默认 mkdir、密钥环境过滤 | PASS |
| RO 工作区写拒绝、Patch 拒绝；WW 工作区 Shell/Patch 写入 | PASS |
| 两种模式下工作区外 create/update/delete 拒绝 | PASS（限定测试夹具） |
| Patch 后 Git init/add/commit | PASS |
| HITL 真实一次性 Patch、拒绝复用授权、原会话仍为 RO | PASS |
| Patch diff + approval 持久 JSONL | PASS |
| Timeout/Cancel 后子孙进程消失、延迟写标记未出现 | PASS |
| 正常关闭清理 TEMP；TEMP 中 Junction 不被跟随、目标文件保留 | PASS |
| 强杀 Bridge 后 Job 进程停止，后续调用 Fail-closed | PASS |
| 强杀后准确报告孤立 TEMP；不自动复用/修复权限 | PASS（残留保留，未声称已清理） |
| Windows、PowerShell 安装目录、Program Files、home、宿主 TEMP ACL 前后一致 | PASS |
| Python 0700 与 pytest tmp_path | **FAIL，阻塞** |

单元测试只用于代码回归，不代替上面的真实隔离验证。复用的 Pi 单元测试含 Nexus 私有 TEMP
扩展：68 PASS、1 个 Windows 平台预期 skip；没有运行会授权整个宿主 TEMP 的上游 E2E。
Ruff 与 Mypy（110 个源文件）通过。Python 全套回归 **928 PASS、10 SKIP**，见
`pytest-03.txt`；跳过项是需 opt-in 的 Docker 验收，不代表新 Linux Backend 已实现。

## NATIVE-P0-01：Python 0700 的保护 DACL

Python 为 **3.12.10**。真实最小复现：在私有 TEMP 中 `Path.mkdir(mode=0o700)` 返回成功，
随后写其子文件报 PermissionError；同位置 `mkdir(mode=0o777)` 与写入成功。
宿主只读回查新目录的 DACL 为：

```text
D:P(A;OICI;FA;;;OW)(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)
```

该 protected DACL 没有继承私有 TEMP 的 Capability ACE。代码/ACL 证据与受限 Token 的
写访问检查一致；不能仅靠更换 TEMP 路径解决。pytest 的 tmp_path 内部明确使用 mkdir0700，
因此失败。完整 stdout、traceback、SDDL 保留在最终报告，无重试抹除失败。

[Python 官方 os.mkdir 文档](https://docs.python.org/3.12/library/os.html#os.mkdir)
说明 Windows 自 3.12.4 起对 0700 应用专用访问控制。
[CPython #134587](https://github.com/python/cpython/issues/134587) 是相关 AppContainer
兼容问题，作为机制背景；Nexus 不是 AppContainer，未把该 issue 的修复视为当前可用解法。

当前不修改 CPython、不 monkeypatch mkdir、不把当前用户 SID 加进所有写限制列表，
不关闭 Low integrity，不让受限进程通过 Local 执行 pytest，也不回退存在安全修复差异的旧 Python。
要通过本项，需要另行确定范围内、可审查的兼容修复；本候选先保留明确失败。

## 性能

`profile-{cold,warm,ro}.json` 使用真实 Win32 API 计时；500 个普通文件的独立测试工作区，
不含大型依赖树。每个配置单次测量，只是本机工程证据，不是统计基准。

| 阶段 | 冷 WW | 热 WW | RO（私有 TEMP 已授权） |
| --- | ---: | ---: | ---: |
| FFI bindings | 9.61 ms | 9.39 ms | 9.90 ms |
| workspace SetNamedSecurityInfoW | 44.67 ms | 精确匹配跳过 | 不调用 |
| private TEMP SetNamedSecurityInfoW | 0.29 ms | 精确匹配跳过 | 精确匹配跳过 |
| CreateRestrictedToken | 0.018 ms | 0.018 ms | 0.035 ms |
| CreateProcessAsUserW | 8.39 ms | 7.91 ms | 11.93 ms |
| 等待 PowerShell 执行退出 | 1030.43 ms | 1007.86 ms | 1073.77 ms |
| 总计（不含外层 Python Bridge 启动） | 1097.64 ms | 1028.81 ms | 1099.39 ms |

最终安装包的 Bridge 首次 Shell：RO 1143.64 ms，WW 1173.39 ms；热 Shell 三次为
1049.86 / 1024.71 / 1037.56 ms。主要耗时在 PowerShell 启动/执行，不在 Token 创建。
首次 ACL 仍会继承传播，目录规模变大会增加成本；没有为了提速取消隔离检查。

## 失败历史、残留与人工门槛

- smoke-01：Python mkdtemp 创建的 OWNER RIGHTS 夹具无法供 Low token 正常读取。
- smoke-02：普通继承夹具缺 WRITE_OWNER，设置 Low label Fail-closed；RO 原始无可写 TEMP
  进入 ConstrainedLanguage，旧 UTF-8 初始化失败。
- smoke-03：专用 FullControl 夹具 WW 正常，RO 兼容问题确认；随后用户批准私有 TEMP 语义。
- acceptance-01：测试宿主混入 PowerShell 7 模块路径，快照阶段失败。
- acceptance-02：guard 同步 FFI 等待导致启动迟缓，手动终止本次测试树；改为原父进程
  handle 非阻塞轮询。该尝试部分用例后来继续返回，不能把它简单计为全程未执行。
- acceptance-03：验收脚本的 Windows argv 引号问题，后改为编码传输 Python 测试代码。
- acceptance-04：3 秒 timeout 在孙进程启动前触发，缺 PID 不能证明孙进程清理；后续增加
  启动窗口和延迟标记验证，05 和 wheel 复验通过。Python 0700 失败始终保留。
- pytest-02：6 个评估夹具因测试专用父目录过长触发 Windows 路径长度限制；失败样本保存。
  后改用系统 TEMP 下短且唯一目录重跑，没有改评估逻辑。

强杀测试遗留私有 TEMP 及全部专用夹具路径记录在各 JSON 中，汇总见
`retained-artifacts.json`。它们不是通用可写宿主根；不复用目录/SID，不强制修复 ACL。
正常关闭的会话 TEMP 已验证删除。standing workspace ACL 不自动撤销；其影响见
[平台路线说明](../next/stage4-platform-backends.md)。
两次通过的普通 pytest 专用 TEMP 已核对绝对路径、无 reparse 点后删除，见
`pytest-temp-cleanup.json`；失败夹具作为证据保留。

**人工验收门槛尚未达到：先处理或明确决策 NATIVE-P0-01；未经用户亲验，不启动 Linux Backend。**
