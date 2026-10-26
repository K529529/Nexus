# Stage 4 平台 Backend 路线（2026-10-26 用户冻结）

本增量取代 SRT 作为长期 Windows 路线的选择；已有 SRT 分支提交、代码、测试和报告保留，
不扩大 SRT 专项。Single Agent Loop、Tool Registry、Security Gateway、HITL、JSONL Audit、
Resume 不变；Local 调用原执行器，不依赖任何 Sandbox Runtime。

当前状态：**Windows 开发候选，尚未通过全部真实验收**。Python 3.12.10 的 `0700` /
`tempfile.mkdtemp()` / pytest `tmp_path` 存在阻塞；不得标注 Production Ready 或进入 Linux 阶段。
完整结果及历史失败见 [Native 验收报告](../reports/stage4-windows-native-acceptance.md)。

实施顺序：Windows Native 实现与真实 Shell/Patch/RO/WW/Git/Timeout/Cancel/性能验收 →
用户亲自验收 → Linux/WSL2 Ubuntu 长驻 Docker Backend。当前 Linux 受限执行 Fail-closed，
尚未实现/验收；macOS 不支持。不自动合并 V0.3，不运行旧 SRT PoC。

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

## Linux 后续边界

参考 pi-container-sandbox：长驻 Docker 容器、workspace bind mount（RO/WW）、Shell/Patch
均进入容器，非 root、drop all capabilities、no-new-privileges、CPU/Memory/PID 限制，
默认无 Docker Socket，显式容器网络策略。缺 Docker Fail-closed。必须在真实 Linux/WSL2
环境验证；Windows 未经用户亲验之前不进入该实现阶段。
