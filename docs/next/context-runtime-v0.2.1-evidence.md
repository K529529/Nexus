# Context Runtime V0.2.1 — Tool Observation Lifecycle

状态：实现与离线验证完成。没有运行云模型、真实单 case 或完整八项 Evaluation suite。本轮仅实现请求时 Tool Observation Lifecycle，不改变单 Agent Loop 拓扑。

## 修改位置与数据流

| 文件 | 职责 |
| --- | --- |
| `src/nexus/core/observations.py` | consumed 推导、HOT/RECENT/COLD 工作集、compact-v1、诊断与策略常量 |
| `src/nexus/core/context.py` | 同源 estimate 接入；HOT 完整工具组保护；snapshot 使用原始逻辑引用，压缩后重新投影估算 |
| `src/nexus/core/agent.py` | 请求边界 build → project → prepare → rebuild → project → check → diagnostics → model；provider fallback 同路；observe 使用实际 model 输入 |
| `src/nexus/app/bootstrap.py` | configuration 事件和 fingerprint 包含 Context 策略与固定参数 |
| `src/nexus/app/profile.py` | 消费 projection 事件，纯数据导出请求数、最后一次诊断及累计工具消息估算，不扩 TUI |
| `src/nexus/evaluation/runner.py` | manifest 和启动版本标签更新，无新评测架构 |
| `tests/test_observation_lifecycle.py` | 30 个离线用例，包含 Unicode/转义字符组合检查和长 run 模拟 |
| `tests/test_safety_compaction.py`、`tests/test_profile.py` | safety 压力改用不可被工具投影缩减的 assistant 历史，原断言保留 |
| `tests/test_evaluation_v0.py` | 验证 configuration / evaluation manifest provenance |

```text
完整 Session.messages / JSONL
  → ContextBuilder.build_active_context（run 隔离 + 逻辑 snapshot）
  → Context.project / observations.project（每次回到原始 FULL 工具结果）
  → Context.tokens / prepare（projected history）
  → 如果生成新 snapshot：原始逻辑 seq 引用 + summary
  → rebuild logical context → re-project → Context.check
  → context_projection 非消息事件
  → model.complete → Context.observe（同一份最终 projected list）
  → 原有协议检查、重复 tool-id 检查、assistant append、执行工具
```

## consumed 与工作集

- `hot_tool_seqs()` 逆序读取同 run 原始 Session.messages。遇到后续普通 assistant 才将此前工具结果视为 consumed。现有 Agent 只在协议/重复 ID 校验通过、message 持久化 emit 成功后 append assistant；没有新增生命周期字段。
- `model_started`、`model_finished`、失败/取消事件、用户 resume 输入、独立的 safety summary 都不属于原始普通 assistant，不推进 consumed。
- `B=context_window-max_output_tokens-1024`，`W=min(16384,floor(B/4))`。每条工具消息 cost 为现有 `estimate([message], [])`，包括同源消息序列化与包装估算；不是 stdout 字节数，也不是固定保留条数。
- HOT 全部 FULL，优先扣除 W；剩余额度从新到旧选择 consumed 的连续后缀，第一条不适合即停止；HOT 超 W 也不裁剪。COLD 只表示 compact candidate，小结果/不可解析结果仍可保持 FULL。

## compact-v1 的确定性实现细节

- 使用 `dataclasses.replace()` 创建新 Message，保持 role、tool_call_id、seq、run_id、protocol_data。任何 preview 都不写回 Session 或 snapshot。
- 总预算以最终 `json_text(...).encode("utf-8")` 为准：成功最多 1024 bytes，失败最多 2048 bytes；不比原文小则返回 FULL。不能可靠解析、元数据本身无法装入或流格式不合法时也保留 FULL。
- 统一元数据：projection、source_seq、tool、ok、error_code、truncated、source_bytes、preview_omitted。适用状态字段保留在 `data` 中，沿用原 ToolResult 的结构。execution truncated 不表示 projection 省略。
- stdout/stderr 中非空流等分可用 preview 配额；成功使用 head/tail 均分，失败每个流优先取最多 512 bytes 的 tail，剩余再给 head。完整短流直接保留；原始非空流若不能保留任何字符，回退 FULL。最终对完整 JSON 做二分配额检查和 byte-size 校验；UTF-8 切片丢弃不完整边界，不制造替代字符。
- apply_patch 保留可装入的状态/文件 facts；不保留 `files[].diff` 正文。文件按原顺序装入，第一项超限即停止，`omitted_files=原 omitted_files + 本次未装入的 files 数`。created_directories 有空间再保留。
- MCP/未知工具不使用领域规则，序列化 data 后产生通用 head/tail preview。不做命令语义分类、检索、artifact store 或逐结果 LLM summary。

以上头尾配额属于 Contract 留出的确定性实现细节，不新增用户配置、调参过程或策略体系。没有发现必须改变冻结边界的冲突，也没有引入未授权的架构决策。

## Safety Compaction 组合

触发判断读取 projected context。摘要请求沿用原顺序，看到 COLD preview，不把 FULL 原文重新展开。HOT 所属完整工具组额外加入保护，与原 system/真实 user/最近两组保护合并；保护内容本身超过 B 时返回 `limited/context_limit`，不调用摘要模型试图丢弃 HOT。

snapshot 仍通过 `kept_seqs` / `replaced_seqs` 引用逻辑历史，只持久化 summary 及 seq 关系。保留的 COLD 工具结果在 snapshot 内仍是原始 FULL；最终请求必须重新投影。provider context-limit 的一次 fallback 同样 rebuild → project → check。现有配对、同 run 重复 tool-id、取消、工具执行和完成判定语义保持不变。

## 非模型诊断与 provenance

`context_projection` 在每次普通 `model.complete` 调用前写入 JSONL，fallback 的重试也单独记录；SDK 内部 transport retry 复用同一份请求投影。事件不进入 Session.messages，也不会成为模型上下文。工具 FULL/投影估算只统计当时逻辑活动上下文仍保留的工具结果，不计已被安全摘要替换或其他 run 的工具结果。

离线长 run 样例：12 次工具执行、13 次脚本模型请求，每条原始工具结果约 31 KiB；0 次云请求，无 safety compaction。最后一轮的实际诊断：

```json
{
  "version": "compact-v1",
  "working_set_target_tokens": 16384,
  "hot_full_count": 1,
  "recent_full_count": 0,
  "cold_count": 11,
  "cold_compacted_count": 11,
  "cold_kept_full_count": 0,
  "observation_full_bytes": 375734,
  "observation_projected_bytes": 42576,
  "observation_full_estimated_tokens": 125726,
  "observation_projected_estimated_tokens": 14747,
  "step": 13
}
```

该样例累计工具消息 FULL 估算 817209 tokens，实际 projected 工具消息估算 151335 tokens；provider usage 保持 unknown。这是确定性样例，不是 provider 账单、真实编码成功率或八项评测结论。本地产物：`artifacts/context-v0.2.1/offline-smoke.json`。

configuration、fingerprint 和 evaluation manifest 记录：

```json
{
  "context_policy": "Context Runtime V0.2.1",
  "observation_projection": {
    "version": "compact-v1",
    "working_set_tokens": 16384,
    "working_set_fraction": 0.25,
    "success_max_bytes": 1024,
    "failure_max_bytes": 2048
  }
}
```

resume 仍使用当前运行时代码/配置，当前 configuration 事件明确记录策略；旧历史不重写，新事件不新增 schema 生命周期状态。比较不同策略必须核对 provenance；旧的存在污染风险的 Evaluation 基线不能作为干净 A/B 对照。

## 验证

- 全量离线 pytest：**227 passed，10 skipped**（现有 opt-in Docker 测试默认跳过）。首次启动时专用 basetemp 父目录未创建，导致 fixture 初始化错误；补建目录后的完整复跑通过。
- Ruff：PASS；mypy：PASS（45 个文件）。
- 新增用例覆盖：多工具 HOT、有效 append 后 consumed、invalid/duplicate/transport/cancel/append failure/resume 不推进、HOT 超 W、连续后缀、原文不可变、字节确定性、不递归、小结果回退、UTF-8/转义边界、exec 双流与失败尾部、patch 成功/partial、通用工具、损坏结果、snapshot FULL 引用、resume 重建、HOT safety protection、provider fallback、最终 check/model/observe 对象一致、投影统计与 billing 分离、长 run 样例。
- 没有运行真实模型、Docker qualification 或完整八项模型 suite。

## 尚存限制

HOT 与必要保护内容依然可能超过 B；COLD 原文过小或无法解析时仍 FULL。assistant 历史、工具参数和必要 continuation 数据没有新增缩减策略。没有检索原始 observation 的新工具、长期记忆或 soft compaction；原始历史仍可供现有 JSONL 调试与轨迹分析。
