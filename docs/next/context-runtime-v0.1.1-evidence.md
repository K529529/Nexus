# Context Runtime V0.1.1：安全压缩恢复

本轮按用户提供的 Restore Safety Compaction After Run Isolation 要求实施。分支 `refactor/lean-agent-core`，起始 HEAD `759ad8d1935d528ea0a50e694be1eaf465bd287b`。保留 V0.1 的 run 隔离，修复已有安全压缩被误移除的回归。

## 改动文件

| 文件 | 改动 |
| --- | --- |
| `src/nexus/core/context.py` | 恢复 85% 安全压缩与约 60% 目标；ContextBuilder 在当前 run 投影上应用快照，校验并恢复压缩引用 |
| `src/nexus/core/agent.py` | 每个请求边界尝试安全压缩；provider 首次报 context_limit 且尚未尝试压缩时，强制压缩并最多重试一次 |
| `src/nexus/core/types.py` | 增加小型 ContextSnapshot（活动消息、through_seq）以及 Session.compactions 的按 run 映射 |
| `src/nexus/app/session.py` | 重放带 scope=run 的压缩事件；恢复尾行损坏日志时保留投影状态 |
| `tests/test_safety_compaction.py` | 新增 14 个参数化后用例，覆盖压缩、历史保留、隔离、配对、重试、恢复与写入失败 |
| `tests/test_context_runtime.py`、`tests/test_boundaries.py` | 修正测试名称：受保护内容过大或无旧组可压缩时，仍然可能明确停止；断言保留 |
| `README.md`、`README.zh-CN.md`、`docs/next/01-development-design.md`、`02-delivery-and-acceptance.md`、`acceptance-evidence.md`、`context-runtime-v0.1-evidence.md`、本文 | 同步 V0.1.1 当前行为，旧 V0.1 记录标为历史 |

## 活动上下文如何表示

`Session.messages` 继续保存原始消息。ContextBuilder 先限定到当前 run，再应用 `Session.compactions[run_id]` 中最新的投影快照，最后追加快照 through_seq 之后该 run 的新消息。当前 system/仓库指令始终来自当前 Session 前缀。

安全压缩成功时，先追加一条 `context_compacted` 事件，再更新内存投影。事件仍用 schema_version=1，run_id 在已有 envelope；data 增加 `scope="run"`，保存 summary、summary_index、kept_seqs、replaced_seqs、before/after estimate 和摘要请求 usage。保留/替代引用只能指向该 run 当前活动上下文。原请求、补充输入、当前指令及最近两个完整组不可替换。

摘要是标注为历史数据的普通 user message，仅存在于活动投影与压缩事件中，不覆盖原始消息。连续压缩可替换上次摘要。resume 按事件顺序重建同样的投影；旧版无 scope 的压缩事件仍只作审计，防止跨 run 旧摘要污染。

**非破坏性确认：** 没有恢复 `model.complete(session.messages)`，没有使用 `session.messages = compacted_messages`；SessionLog.append 格式未改，既有 JSONL 字节和全部原始用户/assistant/tool 消息、run 边界继续保留。工具 protocol_data 跟随所属原消息整体保留或离开活动投影，不被单独截断或文本化。

## 阈值与失败边界

- `B = context_window - max_output_tokens - 1024`；活动输入估算达到 `0.85 * B` 就尝试压缩，目标约 `0.60 * B`。保留 provider input usage 校准，不使用累计 token 数作为窗口占用。
- 一个边界最多一个有界摘要请求，不拆工具组、不做摘要树。摘要只使用可观察事实，禁用 tools，不展示摘要 token 流；摘要 API 调用仍计入 model_calls/usage。
- 压缩成功后必须实际缩小上下文并落到 B 内；受保护/剩余内容使结果超过 60% 目标时警告后继续。
- 普通阈值摘要失败且原上下文仍在 B 内：警告并保留原投影。原上下文已经超 B、强制摘要失败、受保护内容无法容纳或压缩后 provider 仍超限：limited/context_limit。
- provider 报 context_limit 且本边界未尝试压缩：强制压缩一次并最多重试一次。已尝试过压缩则不再次尝试；写入失败停止，不使用未落盘摘要。

## 实际验证

| 检查 | 结果 |
| --- | --- |
| 全量 pytest（Windows） | **PASS：138 passed，24.76 s** |
| `ruff check --no-cache .` | **PASS** |
| `mypy src tests`（覆盖要求的 mypy src） | **PASS：32 source files** |
| `git diff --check` | **PASS** |

使用现有 `.venv`，pytest cacheprovider 关闭，测试和 mypy 缓存放自动清理的系统临时目录。没有运行依赖同步或修改用户配置。

新增测试实际确认：

1. 85% 以下不压缩、恰好 85% 触发；百万级累计 usage 不触发错误判定。
2. 压缩前后 Session 原始消息相等，JSONL 旧字节前缀不变；Run1 内容不进入 Run2 摘要请求或投影。
3. 原始用户请求与最近完整工具组保持可见；protocol_data 与所属消息保持一致；拆散工具组的损坏快照被拒绝。
4. 连续压缩替换旧摘要；新消息继续进入投影；新 run 不携带旧摘要。
5. 正常日志与损坏尾行日志均可恢复重复压缩的投影；未完成工具结果仍补 unknown，不执行旧调用。
6. 预先阈值压缩与 provider 强制压缩两条路径，分别覆盖后续成功和再次超限，均无循环重试。
7. 摘要失败保留原上下文；压缩事件写入失败不继续调用模型；跨 run 的非法引用被拒绝。

本轮只有安全压缩，没有实现 Tool Observation Lifecycle、soft/economic compaction 或新的上下文框架。未重跑云端 pvlib/SWE-bench，未证明长任务成本下降；触发阈值前仍可能重复发送较大上下文。现有工具输出预算、模型适配器、run 隔离语义保持不变。改动交由用户审核，不自行 commit/push/merge。
