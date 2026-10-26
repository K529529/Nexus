# Stage 4 平台 Backend 路线（2026-10-26 用户冻结）

本增量取代 SRT 作为长期 Windows 路线的选择；已有 SRT 分支提交、代码、测试和报告保留，
不扩大 SRT 专项。Single Agent Loop、Tool Registry、Security Gateway、HITL、JSONL Audit、
Resume 不变；Local 调用原执行器，不依赖任何 Sandbox Runtime。

2026-10-27 Stage 4C 用户指令更新：Windows Native V1 暂时冻结，Python 3.12 的 `0700` /
`tempfile.mkdtemp()` / pytest `tmp_path` 保留为已知限制，本轮获准实现 Linux Docker。
此前“Windows 验收后才进入 Linux”的实施顺序由该指令替代，不能据此声称 Windows 已通过验收。
完整结果见 [Native Hardening 报告](../reports/stage4-windows-native-hardening.md) 与
[Linux Docker 报告](../reports/stage4-linux-docker-acceptance.md)。双平台最终人工验收仍由用户完成。
macOS 受限模式 Fail-closed；Local 保持可用。不自动合并 V0.3，不运行旧 SRT PoC。

## Windows Native

固定复用 Pi Sandbox 1.8.0 的 Windows Runner，源提交
`49271fada9814ede5687fcdcc8188872887f0636`。MIT 与 DeepSeek 归属随源码一起保存于
`src/nexus/_vendor/pi_windows/`。使用 WRITE_RESTRICTED Token、Capability SID、NTFS ACL、
Low mandatory label、先 suspended-create 再放入 kill-on-close Job 的进程生命周期。
不自行重写 Win32 FFI、Token、SID、ACL 算法。

- 仅 Local / Read-only / Workspace-write；Windows PowerShell/pwsh 和既有 apply_patch Worker。
- **Windows partial enforcement / Network: unrestricted**。网络默认不限制，这是用户明确选择。
  旧域名白名单/黑名单配置显式报错，不静默忽略。子进程环境使用必要 OS 变量白名单，
  排除模型/压缩模型配置指向的 API Key 变量，不继承 NODE_OPTIONS/PYTHONPATH/凭据变量。
- 读取遵循调用者现有权限，不提供敏感文件读取隔离；环境过滤不等于凭据文件隔离。
- 运行时使用 Nexus 新建的独立 TEMP/TMP，绝不授予整个宿主 TEMP。跨会话 ACL 锁仍共享在
  `~/.nexus/native-acl-locks`，避免 TEMP 隔离破坏同目录授权互斥。
- 用户已确认调整 Pi 的 RO 语义：RO 仅携带独占 Session TEMP 的 Capability，使 PowerShell
  正常使用 FullLanguage；不携带 workspace/extra-root 写 Capability。正常关闭删除该目录；
  删除不跟随子 Junction，根被替换则拒绝清理并报告。强杀 Bridge 时独立 guard 持有原父进程
  handle，终止 Runner、关闭 Job；未清理 TEMP 的准确路径进入失败诊断，当前 Backend 禁止再用。
  不自动回收无法证明归属/生命周期的残留目录，不自动复用其 SID 或修复 ACL。
- 工作区授权留下 Capability Allow ACE、Everyone FILE_DELETE_CHILD Deny 和 Low label。
  RO 不携带写 Capability；切换 RO 并不撤销磁盘上永久 ACL。每个根必须明确列入用户级
  `security.windows_acl_roots` 才能进行 WW/一次性 Patch 授权。该列表不是模型可申请的权限。
- 系统安装目录、home/宿主 TEMP 根及其祖先、Nexus/Codex/密钥配置目录拒绝作为授权根。
  安装、Python、Node、Koffi 必须位于 workspace/write roots 外。失败绝不降级 Local。
- 保留上游 partial 限制：硬链接别名、Everyone/logon 的环境固有权限、Low token 与被其他
  AppContainer 标记文件的兼容性；首次继承传播成本和对部分 FullControl 打开的影响。
- 与旧 SRT 的规则不同，Native WW 的授权单位是整个根；根内 `.git/config`、hooks、`.nexus`
  没有额外路径 deny。Nexus 安装和宿主会话存储必须在写根之外。工作区目录还需要调用者拥有
  WRITE_DAC/WRITE_OWNER；不满足时直接失败，不自动增加用户权限。
- 本轮不会对真实用户项目/共享 TEMP/系统目录进行 ACL 实验；真实测试只在专用新建目录。

Native 复用已有 BridgeClient 和 `serve(manager)` 的顺序 IPC、流式输出及 Patch Audit ACK。
该普通 transport 当前保留在历史 `sandbox.py` / `srt_bridge.mjs` 文件中；Native 不构造
SRTBackend、不加载 Anthropic 包、不启动 srt-win。不为重命名而重构历史 SRT 代码。

独立安装的候选配置示例（测试前阅读上述 standing ACL 影响；不修改已有全局配置）：

```toml
[security]
mode = "read-only"
windows_koffi = 'D:\WorkSpace\nexus-native-runtime-20261026\node_modules\koffi'
# 仅实际批准过持久 ACL 修改的根可列在这里；WW / 一次性 Patch 需要精确匹配。
windows_acl_roots = ['D:\WorkSpace\your-dedicated-test-workspace']
```

切勿添加旧 `srt_package` 或网络域名规则。依赖安装必须位于 workspace/write roots 外。
`/permissions` 显示 **Windows partial enforcement / Network: unrestricted**，过滤敏感环境；
Local 保持原行为。过滤环境变量不阻止读取现有凭据文件，不承诺网络保密性。

## Linux Docker V1

Nexus 在 Linux / WSL2 Ubuntu 内运行时自动选用 Docker；Windows PowerShell 启动仍走 Native。
参考 pi-container-sandbox 的长驻容器和 `/workspace` bind 模式，每个受限 Conversation 惰性
创建唯一容器并复用。RO bind 为只读，WW bind 为可写；内部 `/tmp` 独占可写，镜像根文件系统只读。
以宿主非 root UID/GID 执行，cap-drop ALL、no-new-privileges，默认 2 CPU / 1024 MiB / 128 PID。
不挂载 Home、凭据、Nexus 会话数据、Docker Socket，不共享宿主 PID/network namespace。
宿主配置、安装及 Docker CLI 必须位于 workspace 外；bind 关闭递归子挂载。

```toml
[security]
mode = "read-only"
docker_network = "bridge" # 默认可联网；none 禁止常规外部网络
docker_cpus = 2.0
docker_memory_mb = 1024
docker_pids = 128
# docker_image 可指定兼容镜像；必须匹配当前安装的 Patch 源码/协议标识。
```

只读诊断：`nexus sandbox doctor`。显式构建：`nexus sandbox setup`（可能下载构建依赖）。
普通工具调用不拉取镜像；Docker/镜像缺失、状态不符、旧域名策略、extra writable roots、
Windows/SRT 专属配置均明确失败，不回退宿主。不支持自定义任意 Docker 运行参数。

Linux Patch Worker 来自匹配内容哈希的可信镜像，通过 stdin/stdout 与宿主通信。
按用户确认，仅 Linux 使用 planned Diff → 持久化 ACK → 写入 → committed fact/ACK 的屏障；
Windows/Local 保留原默认顺序。ACK 绑定独立请求 ID、Tool Call、文件序号和事件种类。
RO Allow once 使用独立短命 RW Patch 容器，原 RO 容器不改挂载。

Timeout/Cancel 销毁所属容器并确认消失，防止只终止 docker exec CLI 却留下子孙进程。
中断会话不再复用，需新建 Conversation；清理不确定时显式 cleanup_incomplete。
异常宿主退出的孤儿由 doctor 根据 labels、机器/启动标识、PID starttime 诊断，不自动重用或删除。
