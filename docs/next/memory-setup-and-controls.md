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

默认 `enabled=true`。`/memory` 与 `/memory status` 查看配置值、活动状态、QMD 命令、
当前或上次保存的索引状态、Snapshot tokens；查看状态不会启动 npm、QMD 或模型。

| 操作 | 生效方式 |
|---|---|
| `/memory off` | 空闲提示符处保存配置，关闭当前资源，自动开启 Memory Off 的新 Session；下一 Run 不含旧记忆历史、Snapshot 或六个 Memory Tools |
| `/memory on` | 保存配置，在同一空闲边界开启新 Session 并异步准备依赖和索引；普通编码可继续，Memory Ready 后可搜索 |
| 重复相同开关 | 保留当前 Session，不重复安装；若 on 时索引已失败，则建立新 Session 做一次恢复尝试 |
| `nexus memory off` | 无模型调用，保存配置；后续新 Session 生效，不远程控制其他已运行进程 |
| `nexus memory on` | 无模型调用，保存配置并等待依赖/索引就绪；失败明确返回非零，开启选择仍保存，下次启动可重试 |

切换时创建新 Session 的原因：旧 Memory 内容可能已经出现在工具结果或 Semantic Summary，
只移除 system Snapshot 不能保证真正关闭。保留原 JSONL、Plan、旧摘要，不清洗或重写历史。
因此切换会重置当前工作会话的历史、Plan 和显式 Skill 选择；原会话保留供恢复。
关闭状态下 `/resume` 选择含 Memory Snapshot/工具调用的旧会话会明确拒绝并提示先 on；
关闭状态下的会话仍能正常 Resume。默认开启时原 Resume/Compaction 语义保持。
命令由 CLI 的串行交互循环在 Run 结束后处理，不会中途更换正在请求的模型上下文或工具。

使用 [TOML Kit](https://tomlkit.readthedocs.io/en/latest/quickstart/) 保留原文件结构和注释，
只更新 `memory.enabled`。保存前用 stdlib TOML 解析比较全部值，只允许目标布尔值变化；
检查文件是否被并发编辑，再 fsync 临时文件和原子 replace。普通表、点号键和内联表均测试。
损坏 TOML、链接配置文件或写盘失败会明确报错；不会自动修复或覆盖其他配置。
未实现多进程配置写事务；并发编辑检测与 replace 之间仍有很小的竞态窗口。

### 验收

- 24项 UX/Snapshot/CLI 定向测试通过：开关持久化、关闭后模型请求中没有 sentinel、
  没有 Memory 工具、恢复保护、旧 Snapshot/压缩语义回归。模型请求隔离采用 scripted model，
  不是付费主模型 Dogfooding。
- `scripts/memory_controls_smoke.py` 运行6个真实独立 CLI 进程：off/status/on/status/off/status。
  使用隔离用户配置及先前干净安装的真实 QMD；on 真正创建索引，全部退出码0。
  Markdown/Recovery 内容哈希、GGUF 大小与mtime不变，SQLite 在 off 后仍存在，其他 TOML 值
  和注释保留。证据 `evidence/memory-setup/controls-1/report.json`。
- 首轮 UX 全量 **623 passed / 10 skipped，44.67秒**；Ruff/Mypy 通过。10项为未启用的
  Docker 验收，本轮未扩大 Docker/Stage1 功能范围。
- 最终全量仍为 **623 passed / 10 skipped，44.98秒**，见 `ux-final-pytest.txt`。
  Ruff、Mypy（89个源文件）、`uv lock --check`、`uv build` 全部通过。
  最终真实 QMD 复核 `reuse-final` 继续通过，依赖、索引和 GGUF 均复用。
- 一次 UX 测试父临时目录未成功创建，4 passed / 20 setup errors；重新创建专用目录后24项通过。
  该次没有执行失败用例主体，不计作产品行为失败或成功。

## 平台与边界

- **已验证**：Windows x64 官方 Node/QMD 真实安装、已有安装复用、独立进程缓存/索引复用、
  真实 CLI 开关及数据保留、全量自动化回归。
- **已实现但未实机验证**：Linux/macOS x64/arm64、Windows arm64；平台选择和 archive摘要映射
  只有单元测试。不能把这些测试当作实际 native module 安装成功。
- **未充分验证**：极慢网络、代理证书环境、低内存/显存机器、所有 npm 原生模块构建回退情况。
- 不安装管理员级系统依赖，不改 PATH，不下载到包安装阶段，不新增记忆功能或运行时编排。
  显式 `qmd_command` 视作用户管理的命令，不自动覆盖它；错误会报告为 Search unavailable。
- 本轮没有调用付费主模型或压缩模型。GGUF 首次准备约2.56GB，后续验收未重复下载。
  干净验收的依赖和模型缓存在系统临时隔离目录保留，供复核使用。

## Git 交付

- Dependency Bootstrap：`8c358fdaf1b6b6d6d0598506d3bb31d140c323e5`，已推送 Stage2，远端SHA一致。
- Memory UX：本报告所属 `feat(memory): persist on-off controls with safe session boundaries` 提交；
  交付回复记录最终 SHA。没有合并 main 或发布 PyPI。
