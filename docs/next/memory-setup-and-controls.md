# Stage 2：Memory 首次安装与开关体验

本轮仅补齐依赖准备和开关，继续 `codex/nexus-v0.3-stage2-long-term-memory`。

## Dependency Bootstrap

`app/memory_setup.py` 在 Session 启动后的后台任务中准备依赖，不在 pip/uv 安装阶段执行。
先探测可执行的 Node >=22 与 QMD 2.8.3；同一个 Conversation 缓存结果，后续 Run 不启动 npm。
已有兼容 QMD（包括原 `%LOCALAPPDATA%/Nexus/qmd`）直接复用。其他版本不覆盖，
安装进版本化的用户目录 `qmd-2.8.3`。未指定自定义 `memory.qmd_command` 才自动准备。

缺少可用 Node/npm 时下载固定 Node 22.23.4 官方 archive，逐平台固定 SHA-256；
QMD 顶层 tgz 固定 npm 官方 SHA-512，npm 负责依赖及锁文件完整性。
使用官方 registry、隔离 npm 配置、不改系统 PATH；仅对子进程添加 Node 路径。
来源：[Node checksums](https://nodejs.org/dist/v22.23.4/SHASUMS256.txt)、
[QMD npm metadata](https://registry.npmjs.org/@tobilu/qmd/2.8.3)。
依赖安装仍运行 npm 包的标准安装脚本；不是自行实现独立供应链或系统包管理器。

下载/解包/npm 在独立 `.setup-*` 目录执行，通过 executable version check 后 rename 发布。
失败或中断阶段不作为成功安装复用，保留诊断残留；下一 Conversation 有限重试。
损坏的 Nexus 管理目录保留为 `.invalid-*` 后重装；不删除用户 Markdown 或覆盖全局 QMD。
npm 有900秒执行上限和1次 fetch retry；下载有socket超时、大小上限、固定摘要校验。
取消安装会终止 npm 进程树；已在下载的线程可能在当前网络读取结束后才退出。
不支持多进程同时向同一个依赖目录安装；目前仍是单用户单活动 Conversation 的边界。

用户目录：Windows `%LOCALAPPDATA%/Nexus`；Linux `$XDG_DATA_HOME/nexus` 或
`~/.local/share/nexus`；macOS `~/Library/Application Support/Nexus`。
为 Windows/Linux/macOS 的 x64/arm64 提供官方 archive 映射；其他架构明确 unavailable。
Linux musl/老 glibc、缺少原生依赖构建工具的环境可能无法运行官方二进制或 npm 原生模块，
会明确失败，普通编码仍可用。**Linux/macOS 本轮没有真实机器安装验收。**

复用原 `MemoryIndex`；已有有效 source fingerprint 和健康索引不执行 update/embed。
首次 collection、索引/Embedding、首次搜索模型准备有阶段提示；后续健康启动保持安静。
GGUF 由 QMD 原生缓存与按需加载；不无条件 `qmd pull`，不打包模型。

## 真实安装证据（Windows x64）

`scripts/memory_setup_smoke.py` 为可重复入口，无付费模型 API：

```powershell
$env:PYTHONPATH = 'src;.'
python scripts/memory_setup_smoke.py --phase fresh --home <不存在的隔离目录> --output <新证据目录>
python scripts/memory_setup_smoke.py --phase reuse --home <同一隔离目录> --output <另一新证据目录>
```

它将进程 PATH 缩至系统工具目录，断言 Node/QMD 不可发现；使用独立依赖、npm 和
`XDG_CACHE_HOME`，不继承用户已有 GGUF。不是卸载宿主依赖或一台全新操作系统。

- `evidence/memory-setup/clean-1/report.json`：PASS，406.343秒。真实下载 Node、QMD、
  3个 GGUF，总模型字节2560742688，创建索引并通过中文查询召回英文记录。
- `reuse-1`：新 Python/QMD 进程 PASS，模型大小及mtime不变、没有安装事件、没有
  update/embed事件，真实检索再次成功。
- `node-only-1`：只有 Node 可发现，实际 npm 安装 QMD，未下载 Node。
- `existing-1`：本机原有兼容安装直接复用，安装事件为空。

定向75项通过。首轮全量614 passed / 10 skipped / 1 failed，失败为旧 CLI 替身没有
新增 start_memory 方法，已补齐并重验。另一次测试启动遇到共享 pytest 临时目录权限，
48 passed / 27 setup errors；改用独立系统临时目录，不改系统权限。
故障恢复及平台映射由离线替身覆盖，不冒充其他平台真实安装证明。
修复替身后全量 **615 passed / 10 skipped，43.42秒**，Ruff/Mypy 通过。
再次启动复用路径总耗时7.735秒（含真实搜索），原始输出均保留。

## Memory UX

实现与最终验证结果在后续功能提交补充。
