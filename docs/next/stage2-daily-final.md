# Stage 2 封版：Daily 与 Context Budget

日期：2026-10-24。分支：`codex/nexus-v0.3-stage2-long-term-memory`。
本轮只增加生命周期 Daily 和修正管理窗口；不合并、不启动 Stage 3。

## 实现与 Pi 对照

编码前读取了 [pi-memory/index.ts](https://github.com/jayzeng/pi-memory/blob/main/index.ts)
（本次读取 Git blob `2f2790186e945fd8b5fd4e6a4dad7bf56d4111ca`）以及
[Pi interactive shutdown](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/src/modes/interactive/interactive-mode.ts)。

| 路径 | Pi 行为 | Nexus 适配 |
| --- | --- | --- |
| 压缩前 | 未完成 scratchpad + 当天 Daily 尾部 15 行；无 LLM | 确认存在可压缩历史后执行；自动、手动、Provider overflow 走同一边界 |
| Handoff 格式 | 时间戳、Session ID、`Session Handoff`、两个来源段 | 同结构；去除来源的 Nexus 条目 ID 包装后取可见 Markdown 的 15 行，避免嵌套 ID 破坏恢复语法 |
| Snapshot | 每次压缩边界刷新，即使没有 Handoff | 强制重新读取磁盘、保存 `memory_snapshot`/`instructions`；重新计算压缩保留量与容量；普通写入仍保持稳定 |
| 退出 | shutdown 总结；短会话、空结果、全 None、失败跳过 | 仅交互 `/exit`、EOF/Ctrl+D；少于 4 条有效当前投影消息跳过；重复调用只执行一次 |
| 退出 UI | 先停止终端 UI，再 shutdown hook | `prompt_async` 结束并恢复输入、停止 Rich Live 后总结；无 assistant delta 或新交互提示符 |
| `/new`、`/resume` | 默认跳过总结 | 普通 `close()` 只清理；切换、Memory 开关边界、exec、eval、Run 结束、Ctrl+C 都不触发退出总结 |
| 模型 | Session model；上游允许单独覆盖 | 只用现有主 Coding Model 配置创建无工具请求；不调用独立 CompactionModel、不加第三个配置 |
| 输入 | 近期字符截断，80,000 字符 | 使用已有 TokenEstimator，整个请求（含指令）不超过 `int(context_window * .20)`；128K 对应 25,600 |
| 输出与超时 | 空结果不落盘；默认 10 秒 | 输出上限 `min(主模型输出上限, 2048)`，模型请求外层 10 秒取消；关闭客户端最多另等 1 秒；API 输出截断也不写入 |
| 存储/索引 | append + QMD update | 沿用 MemoryStore 原子追加、dirty 意图和 MemoryIndex.changed；不中断原 Markdown、不新增检索编排 |

退出总结从当前 ContextBuilder 投影读取已有摘要和近期历史，不重新恢复被替换的历史。
四个固定标题为 Decisions / Lessons Learned / Notes / Follow-ups。Prompt 要求区分
已确认决定、未确认建议和历史状态；500–800 tokens / 每节约三条是软提示，不是额外裁剪算法。
模型仍可能超过该篇幅；最终有效性检查要求四节完整、正文非空且不全为 None。

退出写入后立即通知既有索引任务；关闭时不额外等待整套模型加载和 Embedding。
未完成的更新保留 dirty，下一 Session 按原有增量机制恢复。真实验证覆盖了这一路径。

## Context Budget 审计

- `Context.budget = context_window`，表示 Nexus 管理的输入窗口。
- 自动触发只用 `floor(context_window * .9)`；128000 → **115200**。
- `Context.check()`、keep-recent 可用量、压缩后容量检查统一使用管理窗口。
- Diagnostics 用 `managed_context_window`，不再把它标成 Provider safe input。
- 主模型配置不再强制 `context_window > max_output_tokens + 1024`。
- API 的输出参数和值保持原样；独立压缩模型的输入 + 输出 + 1024 容量检查保留。
- Provider 真实超限的识别、一次强制压缩和一次重试保留，不假定所有模型都是 1M。
- 无新历史裁剪策略；现有最近完整组、Runtime Facts、Checkpoint、Resume 保留。

测试覆盖输出上限 8192/32768 均得到 115200，并用实际 TokenEstimator 构造
115199 不压缩、115200 成功压缩的边界。旧输出预留导致的提前触发和拒绝已去除。

## 真实验收：所有尝试

输入来自真实 FilePilot Rename dogfooding Session `2925ee2af1b441a4af9b5d91298e44eb`。
只复制到隔离 Nexus home；不执行 Coding Run，不改原项目、原 JSONL、日常记忆或用户配置。
Handoff 使用明确标注的验收 Daily 和从原对话取出的 pending TODO。
原日志 SHA-256：`cffef6eb18b6fb0d4e8f4db5a15158477f58030ee7728fc2d2a16e2f62855543`。

主模型 `deepseek-flash`，原配置 reasoning_effort=high；Compaction 为独立的
`Qwen/Qwen3.5-35B-A3B`。首次真实压缩输入 41,734、输出 591 tokens。
原活动投影约 57,855 tokens，真实压缩成功，Snapshot 包含新 Handoff。
此后各次总结诊断使用同一个已压缩 Session 的独立副本，避免重复调用压缩模型。

| 尝试 | Prompt / 超时 | Exit 结果 | Provider 输入 / 输出 | 总结阶段耗时 | 检索 |
| --- | --- | --- | --- | --- | --- |
| `real-default-10s` | 初版 / 10s | 超时，未写入 | 估算输入 25,260；取消后无 reported usage | 到达 10s 限制 | Handoff 实际 hybrid 命中；出现 GPU 分配警告 |
| `real-diagnostic-60s` | 初版 / 60s，仅诊断 | 写入 | 25,260 / 1,709 | 12.281s | 索引 ready；hybrid 因 GPU/CPU 内存分配失败，保留 FAIL |
| `real-concise-default-10s` | 加短篇幅提示 / 10s | 写入 | 25,299 / 1,132 | 9.500s | 新 Session Snapshot 正常、hybrid 命中；仍有 GPU 分配警告 |
| `real-final-10s` | 最终归因 Prompt / 10s | 超时，未写入 | 估算输入 25,339；无 reported usage | 10.032s（含请求准备） | 词法 MCP 查询命中现有 Daily；不能当成 Exit Summary 成功 |
| `real-final-diagnostic-60s` | 最终 Prompt / 60s，仅诊断 | 写入 | 25,339 / 1,571 | 12.202s | 新 Session Snapshot 正常；真实 QMD MCP 词法命中 |

**结论：最终 Prompt 的默认 10 秒长会话总结没有通过成功写入验收；超时降级已通过。**
较早 Prompt 曾在 9.5 秒成功；这不能证明最终版本或所有长会话都可稳定在 10 秒内完成。
产品严格保持用户要求的默认 10 秒；60 秒仅存在于 opt-in 验收脚本参数中。
如果后续希望优先保证长会话 Daily 产出，建议单独决定是否将该上限提高到 20 秒；本轮未擅改。

真实样例与原始报告：

- [Handoff](evidence/stage2-daily-final/real-default-10s/handoff.md)
- [最终 Prompt 真实 Summary（60 秒诊断）](evidence/stage2-daily-final/real-final-diagnostic-60s/exit-summary.md)
- [最终默认 10 秒失败回执](evidence/stage2-daily-final/real-final-10s/report.json)
- [最终成功诊断回执](evidence/stage2-daily-final/real-final-diagnostic-60s/report.json)
- 所有早期尝试均保存在同一 evidence 目录，未用成功尝试替换失败。

### 内容质量核对

原会话 seq 498、506 的工具结果支持 77 passed / 1 skipped 与 211 passed / 7 skipped；
seq 530 支持提交 `c854704`；seq 545 的交付说明与其一致。
最终样例保留未做真实浏览器验证、Nexus patch/history 优化待办、工具问题与产品问题的区别。
初版及短篇幅版对模型的 Daily 作者建议归因不够明确，最终 Prompt 已要求未确认建议放在
Notes 并标明 proposal；最终真实样例符合这一点，也把“Daily 为空”限定为当时的观察。
没有发现上述抽查事实被编造，但这不是全面语义准确率测量。
摘要以当前投影和近期重点为主，未完整复述早期 Rename 实现全过程；这是输入边界的限制。

### QMD 与磁盘

实际复用已有 QMD **2.8.3** 和固定模型缓存，没有安装或重新下载。
五次尝试都核对了 GGUF 名称、大小和 mtime 未变，原始输入 JSONL 字节未变。
样例 Daily 原有内容均保留；新 Session 能把成功的 Exit Summary 纳入 Snapshot。
Hybrid 的显存/内存故障是本次观察到的运行环境边界；没有修改 QMD 配置、模型或增加降级逻辑。
最后使用明确标注的 MCP lex/no-rerank 验证新内容已被索引，**不将词法成功冒充 hybrid 成功**。

## 自动化与交付边界

- 当前工作区：**658 passed, 10 skipped**，46.24s。10 项为未启用的 Docker 验收。
- 当前工作区 Ruff 全量通过；Mypy `src tests`：93 files 通过。
- 独立候选树：**654 passed, 10 skipped**，44.57s；Ruff 通过，Mypy 91 files 通过。
  结果见 [candidate pytest](evidence/stage2-daily-final/pytest-candidate.txt)。
  候选只包含本轮暂存内容，排除了用户未提交的 evaluation/history 改动。
- 同一独立候选树的 `uv build` 成功生成 wheel 与 sdist；未安装到用户环境或发布。
- 定向覆盖：Handoff 空/非空/连续/自动/手动，真实条目标记与恢复语法，Snapshot/Resume，
  Exit 主模型/no tools/无私有推理、完整 Token 预算、近期截断、空/None/length/超时/API/磁盘失败、
  Memory OFF/eval/普通 close/切换、重复退出、新 Snapshot、索引 dirty 通知。
- CLI 顺序测试加真实 PromptSession pipe-input 测试确认 `/exit`、Ctrl+D 在 prompt 停止后总结。
  没有宣称做过 macOS/Linux 真实终端或真人交互验收；本轮真实运行平台为 Windows。
- 开发过程保留的失败：首轮定向 8 项旧预算断言失败，首轮全量 3 项旧契约/fake 缺方法失败；
  更新了测试语义和 fake 生命周期接口。补充精确边界测试初次忘记设置 128K，纠正夹具后通过。
- 独立候选首轮 648 passed / 6 failed / 10 skipped：过长的 Windows 测试临时目录使
  `.jsonl.lock` 创建失败。保持代码不变，改用更短的独占系统临时父目录后全通过；
  [首轮失败日志](evidence/stage2-daily-final/pytest-candidate-long-path-failure.txt) 保留，
  仅去除了四处 pytest 输出行尾空格，失败断言与计数未改。
- Ruff 原先扫描到了旧的原始模型生成 evidence 代码。仅把 `docs/next/evidence` 从 lint 排除，
  未格式化或改写旧证据；src/tests/scripts 继续检查。

修改文件按职责：

- `app/memory_daily.py`：Handoff 格式、退出请求预算和四节验证。
- `app/bootstrap.py`：压缩 hook、强制 Snapshot、限时退出总结和索引通知。
- `app/cli.py`、`app/tui.py`：退出原因与终端停止顺序、简短状态反馈。
- `core/context.py`、`app/config.py`：管理窗口预算解耦。
- `core/memory_snapshot.py`：工具说明如实描述 Daily 自动记录。
- `tests/test_memory_daily.py` 及已有 Context/CLI 测试：新增验收和旧预算断言迁移。
- `scripts/memory_daily_smoke.py`：可复现的 opt-in 真实会话副本验收。
- `pyproject.toml`：打包新验收脚本、排除原始 evidence 的 lint。
- 本报告、两份现有增量文档与本轮 evidence。

限制：MemoryStore 仍保留现有单条 32,000 字符/文件 4 MiB 边界；超大的 Handoff 会明确警告并
跳过，未增加自定义归档/裁剪算法。多进程 Markdown 并发写入保证不在本次扩大。
真实验收的隔离 home 保留原日志副本和派生索引以备检查；路径在各 report.json 中，未加入 Git。
