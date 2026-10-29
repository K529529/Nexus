# Stage 6 · Tool-level Durable Execution V1

2026-10-29 用户授权的 V0.3 增量。基线为 `codex/nexus-v0.3` 的
`10b473ef0da38125b64abf20e3c5947b6d7f2c78`；开发分支为
`codex/nexus-v0.3-stage6-durable-execution`。

本增量仅替代原设计中“所有未完成调用都不恢复执行”的限制：有明确 PREPARED
证据的调用允许继续。仍只有一个手写 Agent Loop、原 Tool Registry、Security Gateway
和 HITL，不引入数据库、任务调度器或通用 Workflow 引擎。

## 持久化与恢复边界

Session JSONL 保持 schema_version=1，通过新增 `tool_checkpoint` 事件扩展；
关联键为 `(run_id, call_id)`，允许不同 Run 使用相同 call_id。

| 阶段 | 持久化内容和屏障 | Resume 行为 |
| --- | --- | --- |
| PREPARED | 完整调用、原始参数 JSON 字符串及摘要；flush + fsync | 校验关联和摘要，排队等待用户提交续接输入；在新模型请求前通过当前 Gateway 执行 |
| DISPATCHING | Gateway 完成当前授权后，记录分发意图；flush + fsync 完成后才调用执行器 | 无完整结果则 UNKNOWN，不重放；Patch 另核对磁盘哈希 |
| COMPLETED | 完整 ToolResult，包括 data、error_code、耗时和截断标志；flush + fsync | 恢复保存的结果，不执行工具 |
| UNKNOWN | 缺少可信结果的保守恢复判断；以 interrupted_unknown 工具消息/恢复审计持久化 | 保留中断风险，不自动重放 |

完整结果指经过原有输出预算和脱敏规则的完整 ToolResult，不是无限量原始 stdout。
Gateway 在返回结果、记录 finished 摘要之前就保存 COMPLETED；Loop 随后提交对应的
tool message，并再做一次 fsync。因此“结果 checkpoint 已保存、tool message 尚未提交”
可以恢复；只有 finished/进程退出等摘要而没有完整结果时，不能伪造成功结果。

普通业务失败仍是 ToolResult，模型可以据此修复。日志写入、flush 或 fsync 失败使
SessionLog 进入 failed 状态，停止后续副作用分发。Checkpoint 不经过终端异步发布，
避免持久化成功与调用方收到确认之间引入 UI 取消窗口。

恢复先配齐原 assistant 批次，再追加新的用户输入。只有 PREPARED 调用会进入待执行队列；
同批已保存结果仍按原顺序恢复。内部 update_plan/load_skill 沿用原事件和校验规则。
摘要与脱敏后参数不匹配时记录 not_executed，不用被替换的参数重跑。

## JSONL 损坏尾部

沿用单写者的 `.jsonl.lock`。损坏尾部的原文件保持不变，仅复制完整且序号连续的记录。
新文件的 `recovered_from` 指向原会话，复制完成后 fsync `tail_recovery_completed`。
创建副本期间保持原写锁；再次选择原文件时定位既有后继，避免复制出第二个可执行的
PREPARED 队列。多个副本或未完成复制会 fail closed，需要人工检查。

旧日志没有 checkpoint 时保留原保守策略：有明确授权前中断证据的记 not_executed，
否则记 interrupted_unknown；不会推断旧调用具备 PREPARED 安全性。

## Patch 持久化顺序

Local 和当前受限 Worker 路径采用同一顺序：

1. 解析整个补丁并完成 preflight，保存所有待修改文件的 `patch_manifest`，含路径、
   before_hash/after_hash；宿主 JSONL flush + fsync。
2. 每个文件保存 `patch_diff phase=planned`；宿主 fsync 后才 ACK。
3. ACK 后重新核对实际文件和 before_hash，防止等待期间的编辑被覆盖。
4. 写同目录临时文件并 flush + fsync，然后原子 replace/link 或 unlink。
   POSIX 同步父目录；新建父目录也同步目录项。
5. 保存 `patch_committed` 并 fsync，然后 ACK，之后才允许下一个文件。
6. 全部执行结束后，依次持久化完整结果 checkpoint 与 tool message。

Worker ACK 绑定 request id、file_index、event_kind；同一文件的 planned ACK
不能被当作 committed ACK。Windows Native 复用的 Bridge 和 Linux Docker 的直连
Worker 均传递 manifest、planned、committed 事件。旧 Worker 显式关闭 prewrite 的
兼容路径保留，但当前产品调用启用 prewrite。

Resume 对未完成 Patch 只读核对：实际 hash 等于 after_hash 为 applied，等于
before_hash 为 not_applied，其余为 conflict；多文件可能 partial。不存在文件使用
null hash，仍拒绝越界、symlink/junction 和超限非普通文件。缺 manifest 时明确标注
覆盖不完整。即使所有已知文件均为 applied，没有完整结果仍为 UNKNOWN：核对结果
不是补造工具结果，也不会自动重做补丁。目录同步在文件修改后失败会终止执行，
保持未知状态，不能当成无副作用的普通失败继续。

## 未知 Shell 副作用与 HITL

UNKNOWN 和 cleanup_incomplete 风险由工具记录重建，保留在 Session 中；跨 Run、
再次 resume 和上下文压缩都不会消失。新 call_id、模型的“已解决”文字或一次性批准
均不能清除旧风险。

存在风险时，新 Shell/Patch/外部工具及其他可能写入的能力通过原 HITL 请求单次确认。
确认内容绑定新调用及其参数，并包含旧风险的 run_id/call_id。已有 Deny 不会升级为
Allow；无人值守或无 approver 时返回 approval_unavailable，不执行。Internal、
skill-read 和已声明的 memory-read/search 保持原策略。

V1 不分析 Shell 文本来证明只读，因此同一受影响会话中的查看命令也可能需要确认。
V1 没有批量解除风险命令，每次确认只批准当前新操作；用户应先核实实际副作用。

## 验证与限制

执行记录见 [Stage 6 验证报告](../reports/stage6-durable-execution-acceptance.md)。

- 这是工具级崩溃恢复，不提供外部 Shell/MCP 的 exactly-once、事务回滚或远端去重。
- 不重新连接旧 PID、回收无法证明归属的进程或宣称旧进程已停止；文件哈希只是检查时的快照。
- 文件操作和 JSONL 并非同一个事务。Windows 使用文件 fsync，但 Python 不提供本实现
  所用的目录 fsync；不能据此承诺 Windows 目录项的断电原子性或存储设备级保证。
- 完整结果额外保存一份会增加日志空间和 fsync 开销；未做持久化吞吐性能结论。
- 正常产品恢复入口是 resume_session + 原 run_turn。只调用 replay 是历史消息解析，
  不执行工具恢复或副作用核对。
- 本轮验证真实进程退出与协议故障，不等同于断电测试或真实平台 Sandbox 隔离验收。
