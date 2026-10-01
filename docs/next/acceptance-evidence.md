# Nexus Next V0 Phase 2：实际验收证据

执行日期：2026-10-01（Asia/Shanghai）。本文件记录实现与实际执行结果，不增加设计契约。

工作区清理记录（2026-10-01）：关键原始证据已归档到 [Next Phase 2 验收档案](../../artifacts/next-v0-phase2-evidence.zip)，包含 132 个证据文件及 SHA-256 清单。下文 `.pytest-tmp/` 是执行时的历史路径，可在压缩包中按同名查找；该临时目录、临时安装环境、单元测试生成目录和旧 Day8 目录已从工作区删除。SWE-bench prediction、官方任务行和评分日志已保留，后续可解压到独立临时目录继续评分。

## 终端 UX 与交互性能优化（基于 004d1d8）

本轮按用户附加要求实施，不改变冻结架构。分支 `refactor/lean-agent-core`，起始提交 `004d1d8 nexus-next V0 第一版`；`core/agent.py`、native registry、model adapter、ToolResult、session 格式、依赖与 packaging 均未改。未 commit/push/merge，用户模型配置未改。

### 改动与生命周期

- `src/nexus/app/tui.py`：不永久打印 model_started；普通成功工具合并为一行摘要、状态和秒数；支持的终端在工具执行期间显示可擦除活动行，重定向和 dumb terminal 无临时输出。Read/Search/Test/Inspect git 仅做 UI 分类，复杂或长命令显示 Run shell command。失败、partial、truncated 和 patch 有界预览仍可见，assistant streaming/final 不裁剪。
- `src/nexus/app/bootstrap.py`：ChatModel、native registry、MCP 连接与发现结果从每 turn 重建改为首次任务惰性建立、Conversation 跨 turn 复用；`close()` 变为 awaitable，释放连接/client 后关闭 writer。
- `src/nexus/app/cli.py`：`/new`、选择 resume、退出均 await 旧 Conversation.close；MCP 上下文在同一 async task 内打开/关闭。SIGINT 取消当前 turn，回合结束后的延迟取消回调不会误伤下一次输入；异常退出也移除临时活动行。
- `src/nexus/tools/mcp.py`：取消中的请求具有未知副作用，和调用阶段连接故障一样停用该 server，当前 Conversation 内不重新连接；新 Conversation 才重新发现。没有 reconnect/pool/manager 层。
- `src/nexus/core/context.py`：只增加独立只读探索可 batch 到一次有界 exec_command 的短提示，以及命令可读、不合并副作用的限制。未自动合并调用或新增 workflow。
- `scripts/swebench_v0.py`：仅将已有清理调用改为 `await conversation.close()`，未处理 Docker、未修改评测设计。
- `tests/test_conversation.py`、`tests/test_mcp_tui.py`、`tests/fixtures/mcp_server.py`：覆盖 UI 分类/限幅/临时行/JSON 完整性、client 复用、MCP 一次连接、真实 stdio 跨回合与 SIGINT、disabled 不重连、new/resume 释放、关闭与迟到取消。同步双语 README、本开发设计与本证据页。

### 同题仓库解释的真实前后对照

任务固定为“解释当前仓库的核心结构和 Agent 执行流程”。两组分析相同的 `004d1d8` 源码副本，45 个文件在运行后均与预先保存的 SHA-256 清单一致；仅用于验证仓库存在的临时 Git 初始化无 commit。优化前使用保存的旧实现，优化后使用本轮实现。相同百炼 endpoint、`deepseek-v4.1-flash/high`、1,000,000 context、32,768 output、40 max_steps、32 KiB tool output；独立新会话，终端宽度 100。耗时包含 Conversation 初始化、模型、工具与关闭，不包含准备副本的时间。

| 指标 | 优化前 | 优化后 |
| --- | ---: | ---: |
| outcome | completed | completed |
| wall clock | 252.164 s | 62.223 s |
| model_calls | 26 | 10 |
| tool_calls | 42 | 18 |
| reported input/output/total tokens | 1,236,945 / 11,033 / 1,247,978 | 262,912 / 5,711 / 268,623 |
| transcript 总行数（含回答） | 380 | 135 |
| 工具摘要行 | 42 | 18 |
| 额外独立工具状态行 | 42 | 0 |
| 永久 generating 行 | 26 | 0 |

这是每组一次的观察，不是稳定性能比率。旧版主动执行安装/测试并修复环境错误（4 次 exec 非零）；新版仅进行只读解释。因此耗时差异同时受到模型探索/验证选择、服务延迟和缓存影响，不能全部归因于连接复用或 batching hint。两次均检查实际源码并说明单一循环、两个工具、会话与执行路径；没有以模型自称 completed 证明代码测试通过。

额外用**同一份优化前事件**重放新 TUI，回答、工具结果和调用数完全相同：380 → 195 行，42 个工具摘要保持 42，26 个 generating → 0。这单独验证渲染噪声减少，不依赖模型少读文件。

### reasoning_effort A/B 实验

复用初次 Windows 产品验收的 A（mean 空输入修复）和 B（slugify + 新测试 + README）的原题及初始文件，不为 low 改题。两种 effort 均使用本轮实现、同一 endpoint/model/预算、20 max_steps；顺序 A-high → B-low → A-low → B-high，每格一次。原有测试全文均保留；外部 unittest 与固定行为断言分别执行。以下 token 全为 provider reported，非估算。

| Case / effort | 整体任务结果 | wall clock | model / tool calls | input / output / total tokens | 额外修复证据 |
| --- | --- | ---: | ---: | ---: | --- |
| A / high | PASS | 19.603 s | 7 / 7 | 17,717 / 1,377 / 19,094 | 有：1 次失败补丁后修复 |
| A / low | PASS | 12.908 s | 6 / 7 | 14,322 / 710 / 15,032 | 无；任务要求的初始红测试不算修复失误 |
| B / high | PASS | 40.332 s | 12 / 11 | 43,608 / 4,039 / 47,647 | 有：1 次 shell 失败、3 次补丁失败后修复 |
| B / low | FAIL / limited(max_steps) | 74.388 s | 20 / 21 | 96,852 / 7,961 / 104,813 | 有：2 次 shell 失败、14 次补丁失败，预算耗尽 |

B-high 完成实现、4 个新测试和 README，完整 suite 与行为断言均通过。B-low 的函数通过外部固定断言，但新测试文件只有 `import unittest`、没有测试方法，README 缺失，模型未运行修改后的完整 suite、未正常给出 final；不能因为外部函数断言通过就算完整任务 PASS。A 两组都实际展示初始失败、修复并运行原测试；high 的额外错误是 unified diff 格式，low 没有额外补丁失败。

**建议：当前 V0 百炼配置继续使用 high 作为默认。** low 在简单 A 上更快，但 B 明显降低交付完整性、增加 repair 与 token 成本，尚无证据支持整体切低。用户若为简单单点工作手动试用 low，遇到多文件修改、补丁格式/上下文连续失败或需要补齐测试与文档时应手动切回 high。此结论只基于这两个小案例，不代表普遍胜率；未实现自动 effort selector，也未改变现有 configurable 行为。C live 本轮未追加，生命周期和 resume 由离线/真实本地 MCP 测试验证。

### 验证、证据与边界

- Windows 全量 pytest：**114 passed，23.11 s**；Ruff：PASS；mypy：PASS（30 source files）；diff-check：PASS。
- packaging 未改，按本轮要求未重跑 build；Linux/远端 CI/PyPI 未执行。本轮未继续处理 SWE-bench Docker 阻塞。
- 保留两次前期探索记录但**不纳入正式前后比较**：`repo-before` 使用实验 20 步上限，limited；`repo-before-40` 的复制脚本遗漏 Git `--full-tree`，工作副本为空，模型转而读父仓库，属于实验准备错误。修正后加入 README/core 文件存在断言，并保存正式两组 45 文件的 hash 验证。
- [本轮原始证据档案](../../artifacts/terminal-ux-evidence.zip)：121 个文件加 archive-manifest，包含脚本、原题、前后文件、全部尝试、公开事件、session JSONL、结果与同事件 UI 重放。ZIP 完整性通过，已扫描确认不含本次配置的 API key；归档仅在本地 ignored artifacts 中。SHA-256：`5063cc9f1ee1d19dfda4eaadc8ee587d9caa15932f47146e77dbbbdc546de00c`。

无需新的架构裁决；保持 high 即可。以下章节保留初次 Phase 2 交付与随后第一轮 TUI 收缩的历史验收，不把本轮 Windows 结果冒充旧 Linux/build/live/SWE gate 的重跑。

## 交付状态

S1–S4 实现与验收通过。S5 的代码、回归、安装和文档检查通过；官方 SWE-bench 测试未执行，镜像准备阻塞，collector 为 `unknown`。**本次交付是实现候选，不能宣称 V0 全部验收完成。**

依据为冻结架构、已批准开发设计 v0.2 和实施验收清单。用户在本次任务授权连续实施 S1 → S5。工作目录为 `D:\WorkSpace\releasePro\github\Nexus\Nexus-next`，分支 `refactor/lean-agent-core`，开始与交付基准均为 `ff4eab2574b0d7aceb901a8e496460fe88b958e6`。核对时远端 `next` 与当前开发分支也指向该基准，无需合并或切换分支。

所有实现保留为未提交工作区改动；未 commit、push、merge、修改默认分支或发布。冻结架构原文及根 AGENTS 未改；用户原有 `.idea/` 保留。未改真实用户 `~/.nexus/config.toml`，所有 live/引导使用隔离配置。实施结束时保留了测试临时目录与安装环境，后按用户要求归档关键证据并清理，当前状态以顶部清理记录为准；未清理用户数据库或服务。

## 环境与门禁

后续 TUI 实测修正：根据用户反馈，默认终端不再打印 shell/MCP 文件正文或工具输出流，仅展示简短操作与状态；失败保留四行摘录，patch 保留最多三个文件的行数与六行 diff 预览。模型公开进度与最终回答保持原样，私有 reasoning 仍不展示。工具结果、模型输入、JSONL 与 `--json` 数据未缩减。Windows 离线回归 **85 passed（21.21 s）**，Ruff 与 mypy（29 source files）通过；新增验证覆盖长输出、失败/超时/部分修改、diff 限幅、模型与日志信息保留以及纯文本降级。该次修正未重跑真实模型或 Linux，也未重新打包；下表 75 项测试及 dist 是初次交付基线。

| 检查 | 结果 | 实际证据 |
| --- | --- | --- |
| Windows 全量 pytest | PASS，75 passed，22.65 s，无 skip | `.pytest-tmp/windows-pytest-final.txt` |
| Linux 全量 pytest | PASS，75 passed，12.01 s，无 skip | `.pytest-tmp/linux/pytest-final.txt` |
| `ruff check .` | PASS | Windows 终端输出；Linux `.pytest-tmp/linux/ruff-final.txt` |
| `mypy src tests` | PASS，29 source files | Windows 终端输出；Linux `.pytest-tmp/linux/mypy-final.txt` |
| `uv lock --check` | PASS，56 packages | 锁文件与本次命令输出 |
| `uv build` | PASS，wheel + sdist | `dist/`；归档检查 `.pytest-tmp/delivery-audit.json` |
| `git diff --check` | PASS | 本次命令输出；只有 Git 的 LF/CRLF 提示 |
| Windows 干净 venv wheel 安装 | PASS | `.pytest-tmp/pip-install-final.txt`；在 `C:\Windows\Temp` 验证 import、help、version |
| Windows `uv tool install` | PASS | `.pytest-tmp/uv-tool-install-final.txt`；源码外执行安装后的 `nexus.exe --version` |
| Linux wheel 安装 | PASS | `.pytest-tmp/linux/install-final.txt`；在 `/tmp` 验证 site-packages import 和 version |
| Linux `uv tool install` | PASS | `.pytest-tmp/linux/uv-tool-install.txt`；源码外执行安装后的入口 |
| GitHub CI / PyPI 发布 | NOT RUN | CI 已改为 Windows/Linux Python 3.12；未 push，因此没有声称远端 CI 已运行 |

Windows 为 Windows 10 19045、CPython 3.12.10、PowerShell；Linux 为 Docker 内 Debian / Linux 6.18.33.2 WSL2、CPython 3.12.14、`/bin/sh`。Nexus 包版本 0.2.0；uv 0.11.30；锁定直接依赖 openai 2.54.0、mcp 2.2.0、prompt-toolkit 3.0.53、rich 14.3.4。开发依赖 pytest 9.1.1、pytest-asyncio 1.4.0、mypy 1.20.2、ruff 0.16.4。

Windows 测试使用 `.venv\Scripts\python -m pytest -q -p no:cacheprovider --basetemp .pytest-tmp/final75-windows`。进程树测试在当前用户权限下运行：Codex 沙箱账户执行 `taskkill` 会被 Windows 拒绝，已单独验证；没有把 `cleanup_incomplete` 冒充清理成功。Linux 在临时 `nexus-next-v0-verify:local` 容器中安装 wheel、复制当前 src/tests/scripts/pyproject 到 `/tmp/nexus-check`，运行同一测试集；源码挂载为只读。离线测试本身无需 Docker、数据库、API key 或网络，容器仅用于取得真实 Linux 环境。

Windows pip 验证使用 `--isolated --disable-pip-version-check --no-index --no-deps` 安装本地 wheel，所需依赖已按锁文件安装；这样不继承用户 pip 网络配置。wheel 安装和 `uv tool install` 是不同检查，不以源码目录 import 代替安装验证。

## T01–T17 行为证据

以下 PASS 对应最终两平台 75 项测试及注明的真实运行，不代表全部可能边界已经穷尽。

| 项目 | 结果 | 对应测试/实际运行 |
| --- | --- | --- |
| T01 消息驱动闭环 | PASS | `test_real_read_patch_test_loop`；Windows A/B/C、Linux A |
| T02 同批顺序与失败修复 | PASS | `test_same_batch_failure_continues_and_model_repairs`；live C2、Linux TTY 的 patch 错误恢复 |
| T03 终止、usage、计数 | PASS | `test_invalid_model_response_never_dispatches`、`test_last_step_executes_tools_without_extra_summary`、未知 usage fixture |
| T04 streaming 与续接 | PASS | `test_stream_tool_fragments_usage_and_private_reasoning`、`test_partial_stream_not_retried_or_returned`、绑定续接 fixture；真实 S1 probe |
| T05 配置与重试 | PASS | `test_retry_before_delta_only`、`test_unknown_usage_and_optional_request_fields`、config fixture；真实 high 请求 |
| T06 实际 shell | PASS | `test_shell_output_cwd_unicode_stdin_and_nonzero`；两平台真实命令 |
| T07 输出预算与进程回收 | PASS | `test_shared_budget_drains_and_keeps_tail`、`test_timeout_and_cancel_reap_child`、cleanup failure fixture、两平台实际 Ctrl+C |
| T08 patch 正常行为 | PASS | `test_patch_multifile_crlf_no_newline`、mode preservation fixture；live 磁盘变更 |
| T09 patch 失败与边界 | PASS | path/preflight/ambiguity/partial/concurrent fixtures；Windows junction、Linux symlink 均实际执行 |
| T10 JSONL 与隔离 | PASS | private/public/resume、single writer、workspace isolation fixtures |
| T11 崩溃与取消 | PASS | 独立进程 `os._exit(23)` 在副作用前/后退出；缺结果 unknown；尾部恢复、损坏拒绝、剩余调用 not_executed |
| T12 context | PASS | groups + identical replay、当前轮旧工具压缩、protected overflow、schema/protocol budget、context error 单次重试 fixtures |
| T13 事件与私有数据 | PASS | 写入失败阻止副作用、消费者故障降级、已知密钥跨 chunk 脱敏、私有字段仅本地保存、eval sidecar 与含凭据 patch 拒绝导出 fixture |
| T14 MCP | PASS | 真实本地 stdio server 与故障 server 共存，同一 run_turn 调用 add 得 7；分页、命名、超时、结构化内容、断开不重试 fixtures |
| T15 CLI/TUI/引导 | PASS | help/version/无 TTY/JSON/selector/正文不重复 fixtures；两平台真实终端与引导；实际发现并修复 SIGINT 唤醒问题 |
| T16 eval artifact | PASS（离线边界）；官方 live 见下节 | original-base diff 包含模型 commit 与 untracked，保持原 index；缺报告 unknown、错 patch hash 拒绝 fixture |
| T17 安装隔离 | PASS | 两平台 wheel + uv tool 源码外入口；Windows/Linux 首次配置检查 |

测试文件为 `tests/test_agent.py`、`test_model.py`、`test_tools.py`、`test_boundaries.py`、`test_session_context.py`、`test_mcp_tui.py`、`test_eval.py`。真实 stdio server 位于 `tests/fixtures/mcp_server.py`；不是只 mock MCP 客户端。

## 指定 Provider 与真实编码

所有正式 live 调用使用 `deepseek-v4.1-flash`、`reasoning_effort="high"`、context window 1,000,000、max output tokens 32,768，endpoint 为用户提供的 `https://maas.qianwenaiapi.com/compatible-mode/v1`。没有替换模型或降级 reasoning effort。账户 region 未由用户提供、未独立核验，不根据域名猜测。

S1 `.pytest-tmp/provider-probe.json`：真实 3 次请求，连续两次工具回传再 final；reported total tokens 分别 409、601、606。该 endpoint 在这些真实多轮请求中不要求回传 reasoning_content，适配器未收集、保存或公开它。离线另测必要续接载荷的服务绑定、恢复与公开隔离，不把 synthetic fixture 当成服务实际要求。

| 轨迹 | 实际结果 | 请求 / 工具 | reported tokens | 独立验证 |
| --- | --- | --- | --- | --- |
| Windows A：空输入缺陷 | completed | 6 / 7 | 15,481 | 先失败再修复；2 tests PASS；原测试文件 hash 不变 |
| Windows B：小功能 | completed | 12 / 16 | 58,422 | slugify、测试、README；10 tests PASS；既有 greet 保留 |
| Windows C1：第一轮 | completed | 6 / 7 | 33,006 | subtract_one；4 tests PASS |
| Windows C2：关闭后恢复 | completed | 5 / 6 | 47,541 | 同一 JSONL 恢复后增加 shift；9 tests PASS；已有能力保留 |
| Linux A | completed | 6 / 5 | 10,081 | 先失败再修复；2 tests PASS；原测试不变 |
| Linux TTY 额外 smoke | completed | 8 / 7 | 13,024 | 建立 tiny.py；真实 assert add(2,3)==5；输出 LINUX_TUI_OK |

正式 A/B/C 共有 Windows 3 条任务轨迹（C 含两个 run）、Linux 1 条，均各一次完成；总计 5 个 run，独立测试和额外断言 exit=0。C 的自动验收使用 `list_sessions` 与新 Conversation 恢复；实际上下键/Enter selector 另在两平台 TTY 操作验证，未将自动调用描述成人工点选 C。

主证据在 `.pytest-tmp/windows-live/` 和 `.pytest-tmp/linux-live/`：`results.json`、每个 run 的公开 JSONL、transcript、external-tests、before/after 快照及私有本地 session 文件。执行器为 `.pytest-tmp/product_live.py`。Linux TTY session 位于 `.pytest-tmp/linux-tui-home/.nexus/sessions/`，实际文件在 `.pytest-tmp/linux-tui-work/`。

失败与尝试说明：

- S2 早期 Windows A 探针第一次未在提示中明确 fixture workspace，模型向父仓库做了只读探索，随后主动中断；该次没有完整落盘轨迹，仅本次工具输出留有记录，不能计为成功。增加明确 workspace 环境事实后第二次完成，证据 `.pytest-tmp/live-a-events.json` 与 `live-a-attempt2.jsonl`。正式产品 A/B/C 为后续独立运行，不覆盖这次失败。
- C2 内有一次不合法 patch，工具拒绝后模型修复。Linux TTY 建文件内有 4 次失败 patch 调用（unsupported/conflict），随后成功；没有删掉失败 observation。已据此给现有 schema 补充新增文件 unified diff 示例，没有增加新的编辑工具或修复节点。
- Linux 初次验证镜像缺 Git；另一次临时容器未安装项目，导致子进程 import 失败；均为验证环境错误，修正后最终同一全量集通过。保留 `linux/pytest-attempt1.txt`、`pytest-attempt2-missing-install.txt`。
- 早期 Windows taskkill 受沙箱权限拒绝，以及过早在 PowerShell 启动期间取消的检查不计 PASS；最终测试等到命令实际输出 readiness 后取消，并检查子进程。Windows symlink 权限不足时实际建立 junction 检查，没有 skip 掉边界。

## 终端与首次使用

Windows 与 Linux 实际 TTY 均验证启动、工具摘要/输出、恢复列表、Enter 选择、最近消息展示、继续等待输入与退出。Windows 首次向导在 `.pytest-tmp/windows-wizard-home/` 完成，保存非敏感设置后进入输入提示；Linux 首次向导在临时容器完成，保存后因故意未传 key 环境变量明确报错，没有隐藏等待。另用已配置且带 key 的隔离 Linux 会话完成上表 live TTY smoke。

真实 Linux Ctrl+C 首次暴露：自定义同步 signal handler 只取消 task，没有唤醒事件循环，可能等命令自然退出。`app/cli.py:drive` 改为通过 `loop.call_soon_threadsafe(task.cancel)` 唤醒后，实测中断 60 秒命令，得到 exit=-15、cancelled、cleanup_incomplete=false，并立即返回提示。Windows 实测也提前中断该命令、记录 cancelled 并返回提示。新增独立解释器回归；Linux 使用真实 SIGINT，Windows 测试使用处理器注入（Windows os.kill(SIGINT) 是终止语义），真实 Windows 控制台按键另验。

还修复 `/resume` 取消选择后丢失当前会话引用、短安全 delta 被密钥缓冲延迟、首个短工具输出被刷新间隔滞留的问题。密钥跨 chunk 的隔离仍由测试覆盖。工具输出中的终端控制字符被过滤，窄终端采用滚动换行；这不是全屏 IDE。

## 原十条 AC 与架构检查

| AC | 结果 | 核心证据 |
| --- | --- | --- |
| 1 pip/uv 安装启动 | PASS | 两平台源码外安装检查 |
| 2 项目根运行、无需 DB | PASS | T12/T17、两平台 live |
| 3 提交编码任务 | PASS | A/B、真实 TTY |
| 4 exec 探索 | PASS | A 的真实工具轨迹 |
| 5 patch 改代码 | PASS | A/B/C 磁盘快照及 tool result |
| 6 执行项目测试 | PASS | 两平台独立测试与实际命令 exit code |
| 7 同循环连续工具 | PASS | S1 与全部 live；失败 observation 进入后续请求 |
| 8 流式可读交互 | PASS | 两平台真实 TTY，SIGINT/短输出修复后复测 |
| 9 本地持久化/恢复 | PASS | C、selector、崩溃恢复和配对测试 |
| 10 核心可读 | PASS（源码审查） | 单一 `core/agent.py:run_turn`、core/tools/app 三包 |

源码审查确认：没有 Planner/Validator/Repair/Replan、Graph、Plan authorization 或独立评测裁决循环；只有两个 native tools；MCP 只注册同一工具字典；Provider 协议在 adapter；恢复只重建消息，不重放副作用。TUI、exec、eval 共用 Conversation 和 run_turn。

当前 `src/nexus` 共 19 个 Python 文件、2,410 非空行（规模观察，不作为门禁）。只有四个直接运行依赖；传递依赖按 SDK 正常安装，不能把 MCP 的传递依赖说成 Nexus 引入了独立平台。物理删除 219 个被替代旧跟踪文件，范围包括旧 application/domain/infrastructure/security/interfaces/skills、专用旧 tests、evals、examples、migrations，以及 alembic/compose/day 脚本；复用名称的文件重写为 Next 职责。没有保留第二 runtime 入口。

## SWE-bench 官方对接

建议的 `pvlib__pvlib-python-1707` 不在当前官方 Lite、Verified 或 full test split 中，因此按验收清单换为真实 Lite 样本 `psf__requests-1963`，没有修改题目。准备日志：`.pytest-tmp/swe-prepare*.txt`。

- dataset：`princeton-nlp/SWE-bench_Lite`。
- dataset revision：`6ec7bb89b9342f664a54a6e0a6ea6501d3437cc2`。
- 原始 base commit：`110048f9837f8441ea536804115e80b69f400277`。
- harness：官方 `swebench==5.0.2`。
- inference：独立容器、fresh shallow fetch 原始 requests commit；项目测试 Python 3.9.21 / pytest 8.3.5，Agent 自身 Python 3.12.13；与官方 grader 环境分别记录。
- 网络：允许模型与依赖网络；没有完成可见 Git 对象/所有挂载/网络答案可得性的污染排除，标记为 **unverified / nonformal integration experiment**，不得声称干净 benchmark 能力。
- 只导出任务字段供 predict；没有向模型提供官方 gold patch 或 test patch。Nexus 脚本只有 predict/collect，没有自定义 grader。

实际 inference 运行：一次，completed；32 model requests、36 tool calls、124,495 ms，reported input/output/total tokens 为 511,800 / 12,441 / 524,241。`completed` 不是官方 verdict。

| 外部验收项 | 结果 | 证据 |
| --- | --- | --- |
| 原始 base → 最终文件 prediction | PASS，非空 patch | `.pytest-tmp/swe-predict/predictions.jsonl`、`runs.jsonl` |
| patch SHA-256 | 已记录 | `f19835f4b8a3575020c100310fa86690fb63a9d0c1ae5bfc736acbf774ae0392` |
| 官方 harness 入口接收任务 | PASS（到镜像准备） | 以下实际三次调用与日志 |
| 官方容器实际评分 | NOT RUN，镜像准备阻塞 | `run_instance.log` 停在 `Image not found locally, attempting to pull...` |
| 官方 resolved | UNKNOWN | 没有 `report.json`，不推断 resolved=false 或 true |
| collector 缺报告处理 | PASS，`official_state=unknown`、report hash=null | `.pytest-tmp/swe-official-collected.jsonl` |
| 真实官方对接 smoke | NOT RUN / 未通过验收 | 尚缺真实评分和可关联的官方报告 |
| 干净 benchmark 能力 | NOT ESTABLISHED | 污染排除未完成，不能从本轮 inference 推出能力结论 |

trajectory 为 `.pytest-tmp/swe-predict/psf__requests-1963-e374e3a643dd48bbae46b7d27a8c3e01.jsonl`，Nexus run_id 为 `b79d9a4e2def4a60864a62805a22d241`。任务、环境与准备日志分别为 `swe-inference-case.jsonl`、`swe-inference-environment.json`、`swe-predict.log`。

官方调用尝试完整记录：

1. `nexus-next-v0-20261001-01`：harness 5.0.2 读取旧 `princeton-nlp` 数据后报 `KeyError: image`，未评分。日志 `swe-official.log`。
2. 从新版官方 `SWE-bench/SWE-bench_Lite` 固定 revision `b0dde1093fe417d83b7184254edf8199c1f0dff5` 下载原始 parquet；逐字段核对该 instance 的 base_commit 和 problem_statement，与 inference 完全一致。原样导出该行到 `swe5-grading-task.json`，没有手工补 grader 字段。证明 `swe5-direct-verification.json`，原文件 `swe5-data/data/test-00000-of-00001.parquet`。此前目录遍历下载遇到 RemoteProtocolError，日志保留 `swe5-dataset-*.log`；直接文件下载成功。
3. `nexus-next-v0-20261001-02` 与 `-03` 使用官方 JSON 行执行，均停在官方镜像 `swebench/sweb.eval.x86_64.psf_1776_requests-1963:latest` 准备。Docker 日志记录三个层的写入锁占用超过 37 分钟（`swe-docker-locks.txt`）；取消本任务两个下载客户端和第二次评分容器后，由第三次唯一 harness 客户端重试仍未取得镜像。结束这些阻塞进程，没有重启 Docker 或清理其内部存储。无评分测试实际运行。

最后一次官方调用：

```sh
python -m swebench.harness.run_evaluation \
  --dataset_name /artifacts/swe5-grading-task.json \
  --predictions_path /artifacts/swe-predict/predictions.jsonl \
  --instance_ids psf__requests-1963 --max_workers 1 --timeout 600 \
  --run_id nexus-next-v0-20261001-03 --report_dir /artifacts/swe-official
```

实际 harness 5.0.2 日志根为 `.pytest-tmp/logs/run_evaluation/`，与网页部分示例的路径不同，README 已按安装版本修正。第三次 `run_instance.log` 位于 `nexus-next-v0-20261001-03/deepseek-v4.1-flash/psf__requests-1963/`。后续恢复官方镜像环境后，应复用现有 prediction、使用新的 run_id 评分，再运行 collect；不需要为拿到报告重新付费生成 prediction。

## 可复核产物与限制

wheel：`dist/nexus_coding_agent-0.2.0-py3-none-any.whl`；sdist：`dist/nexus_coding_agent-0.2.0.tar.gz`。最终 hash、体积、锁文件 hash 和归档条目数记录于 `.pytest-tmp/delivery-audit.json`。归档包含实际 19 个源码文件，不包含 `.idea`、测试临时目录、缓存或旧 runtime。

当前验收档案 `artifacts/next-v0-phase2-evidence.zip` 与 `dist/` 由 Git 忽略，本地保留但不会随着源码 diff 自动交付；用户审核前可另行保存。没有承诺 JSONL 耐断电事务或 shell 副作用恰好执行一次，也没有声称 workspace patch 边界是全运行环境沙箱。未知秘密识别、恶意进程逃逸、所有 OpenAI-compatible 服务兼容性均不是本轮证明范围。

产品返回 completed 只说明模型正常结束。项目测试是否通过看命令与独立检查；SWE-bench resolved 只来自官方报告。这三个状态分别记录。
