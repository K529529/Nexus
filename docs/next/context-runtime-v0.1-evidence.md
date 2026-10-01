# Context Runtime V0.1：实施与验证记录

日期：2026-10-02（Asia/Shanghai）。工作区 `Nexus-next`，分支 `refactor/lean-agent-core`，起始 HEAD `e67c7e4870314e0f9ef129c442d9f3877097eb32`。本文件记录本轮结果，不建立额外架构契约。

## 改动文件与决策

| 文件 | 改动 |
| --- | --- |
| `src/nexus/core/context.py` | 新增小型 ContextBuilder，只返回 system + 指定 run 的消息；现有预算检查改用活动上下文，移除自动摘要/压缩路径 |
| `src/nexus/core/agent.py` | 每次模型请求前投影；新任务创建 run_id，显式恢复消费原 run_id；完整消息仍追加保存；调用 ID 检查与取消补结果限定在当前 run |
| `src/nexus/core/types.py` | Message 内存保留事件已有 run_id；Session 增加一次性的 resume_run_id；public()/模型消息不增加字段 |
| `src/nexus/app/session.py` | 从已有 run_started/run_finished/message envelope 恢复 run 归属；未完成 run 保留历史，缺失工具结果仍补 unknown；损坏尾行复制全部有效事件到新文件，保持边界与 seq，原文件不变 |
| `src/nexus/app/bootstrap.py` | 当前环境事实写入当前 run；恢复提示区分未完成任务与新任务；模型/MCP 连接复用不变 |
| `src/nexus/app/cli.py` | `/help` 说明普通输入隔离、显式 resume 续接 |
| `tests/test_context_runtime.py` | 新增新 run 隔离、持久化不变、预算边界和参数化中断恢复回归 |
| `tests/test_session_context.py` | 旧压缩测试改为旧日志兼容：恢复原始消息，不把跨 run 旧摘要送进新 run |
| `tests/test_conversation.py`、`tests/test_boundaries.py` | 更新跨任务语义；增加完整 Conversation 恢复、指令/环境刷新；服务端 context_limit 不触发压缩重试 |
| 双语 README、开发设计、实施清单、验收记录 | 同步当前行为及限制 |

持久化仍是 schema_version=1 的 append-only JSONL；SessionLog.append 的格式、message data、protocol_data 保存方式均不变。ContextBuilder 不决定是否保存消息，也不写日志：持久化层保存事实，projection 只决定模型可见范围。

普通输入每次是新 run，包括在同一 Conversation 中；显式 `/resume` / `nexus resume` 选择最近 run 未完成的会话，下一次输入使用原 run_id。未完成包括未落 run_finished、aborted、failed、limited；completed 后再输入创建新 run。恢复仍等待用户输入，不重放旧工具、不恢复进程/工作流指针。每次恢复执行段重新计算 max_steps/usage，通过相同 run_id 关联日志。

按本轮“暂不做 compaction”范围移除原自动压缩路径，超预算返回 limited/context_limit。历史 context_compacted 事件仍留在 JSONL；恢复以原消息为准，避免旧摘要混入其他 run 的事实。未引入新配置、依赖、数据库、检索、记忆或 Agent 节点。

## 自动化验证

| 检查 | 实际结果 |
| --- | --- |
| 全量 pytest | **PASS：124 passed，24.07 s，Windows** |
| `ruff check --no-cache .` | **PASS** |
| `ruff format --check --no-cache src tests` | **PASS：31 files already formatted** |
| `mypy src tests`（缓存放临时目录） | **PASS：31 source files** |
| `git diff --check` | **PASS** |

测试使用 `.venv/Scripts/python -B`，关闭 pytest cacheprovider，basetemp 放自动清理的系统临时目录。完整 Windows 测试以同一用户权限执行进程树取消测试，没有创建新的项目根临时缓存目录。

核心断言：

- Task B 首次请求只带 system 和 Task B；其后仅追加 B 自己的 assistant/tool；Task A 日志逐字保留，投影前后 JSONL 字节不变。
- 同一 run 的 tool call/result 完整成组；不同 run 可以使用相同调用 ID。
- 未完成 run 的原用户请求、assistant、必要 protocol_data 和已有结果可恢复；缺失结果为 interrupted_unknown，已知未执行结果保持 not_executed，恢复不执行旧调用。
- 崩溃/aborted/failed/limited × 完整文件/损坏尾行均覆盖；恢复后再关闭、重新打开也不丢 run 归属。尾行恢复保留原文件，复制有效事件的字段与 seq 不变，仅 session_id/header 来源变化。
- 旧 run 再大也不占新 run 预算；当前 run 超预算明确停止，输出仍在完整历史中，没有摘要调用。
- 显式恢复刷新根指令和当前 MCP 不可用事实；恢复完成后再次普通输入恢复为独立 run，连接生命周期回归仍通过。

## 两任务手工请求捕获

使用真实 `Conversation → run_turn → ChatModel → OpenAI SDK` 路径，以本地 `httpx.MockTransport` 返回固定 SSE；模型的第一条工具调用实际运行 PowerShell，读取 README 前 14 行并列出 `src/nexus` 子目录。此验证检查真实序列化请求与持久化，不是云端模型能力测试，没有访问真实云端或使用用户密钥。

| 任务 | 请求与结果 |
| --- | --- |
| A：`Explain repository structure` | completed；2 次模型请求，消息数分别为 2、4；真实 exec_command 成功，返回 README 与 app/core/tools 目录 |
| B：`What is 7 multiplied by 8?` | completed；1 次模型请求，消息恰为 system、B user；system 与 A 相同，没有 A 请求、工具调用、工具输出或最终回答 |
| 持久化 | 两个不同 run_id，JSONL 共保存 6 条 message，另有 instructions 与执行事件；A 工具结果仍在原日志 |

本地原始证据（`artifacts/` 被 Git 忽略，不随源码提交）：

- [report.json](../../artifacts/context-runtime-v0.1/report.json)：SHA-256 `82337b2e66e2f2333e0316a854c4de8721dd19401807fe1f48f8f1ba3de2937b`
- [requests.json](../../artifacts/context-runtime-v0.1/requests.json)：SHA-256 `ee7f3650a9f2303093448b81f1a827a7a7254b1b983f1ae5c2df62194f3dbef2`
- [session JSONL](../../artifacts/context-runtime-v0.1/sessions/5682d566a731097be7a88359/23ba073a2c164822b6da1eb8a2de3c8d.jsonl)：A run `a9dbd4291c44405c90120b94493209cd`；B run `4fc815ad0d464205bb7a463a1eeadd68`。

## 剩余限制

- **同一长 run 内仍回传完整历史，这是已知的当前限制，不是永久策略。** V0.1 聚焦活动上下文投影与 run 隔离；这次没有证明 pvlib 单任务的 37 次调用或约 100 万累计 input tokens 会下降。
- 后续 Context Runtime 版本将引入工具 observation 生命周期管理、软压缩（soft compaction）与历史缩减策略。本轮不实现这些能力，也不新增历史选择 UI 或按 run_id 的 CLI 命令；现有选择器只续接所选会话最近未完成的 run。普通输入“继续”也属于新 run，须显式 resume 才续接。
- 恢复出的原始 run 可能已经超预算，此时明确 limited；不丢弃结果或偷偷摘要。旧版本曾将损坏日志恢复成无 run 边界的扁平消息文件时，原始归属无法凭空还原，默认不自动混入新任务；可查原始日志。
- JSONL 和 Session 内存仍随历史增长；没有文件轮换或历史索引，投影按列表筛选。
- 本轮未重跑云端 pvlib/SWE-bench，也没有新的 provider token 成本或能力结论。未改用户模型配置，未 commit/push/merge。
