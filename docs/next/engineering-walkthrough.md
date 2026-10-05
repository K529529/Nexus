# Nexus 工程讲解：代码、取舍与证据

本文是现有实现的讲解入口，不新增架构契约。目标是能从问题讲到代码和验证，
不把功能清单或一次 PASS 当作可靠性证明。

## 一分钟介绍

Nexus 是一个轻量本地 coding agent。单个消息循环让模型选择工具、接收执行结果，
再决定下一步；运行时负责协议、资源限制、工具执行、会话记录和故障恢复。
没有额外 Planner/Judge，也没有数据库或仓库索引。

工程重点是三个问题：如何在缩短上下文时保留工具协议与最近结果；如何在中断后
恢复可信的会话状态；如何把模型生成的补丁变成可检查、可报告失败的文件操作。
模型能否正确修复并及时结束，要用独立评测检验，不能由运行时状态代替。

## 1. 消息循环与执行边界

入口：[run_turn](../../src/nexus/core/agent.py)、[ChatModel](../../src/nexus/core/model.py)、
[工具装配](../../src/nexus/tools/registry.py)。

```text
当前 run 的逻辑消息
  → 构造实际请求、检查容量
  → model.complete
  → 校验并保存 assistant
  → 有 tool_calls：按顺序 dispatch，保存配对的 ToolResult，再请求模型
  → 无 tool_calls 的合法 final：completed
```

这是一种模型驱动的 action/observation 循环。统一的 ToolSpec/ToolResult 让模型适配器、
执行器和终端显示分离；同一 Conversation 装配路径支持 interactive、exec 和 Docker eval。
私有 reasoning continuation 由 adapter 处理，Agent 不读取推理文本来决定工作阶段。

Java 后端类比：ToolSpec 是接口契约，Registry 类似命令处理器注册表，ToolResult 是统一
返回值；模型是会犯错的外部决策服务。因此参数校验、执行边界和异常路径不能委托给模型。

证据入口：`tests/test_agent.py`、`tests/test_model.py`、`tests/test_conversation.py`。
局限：本地 shell/MCP 使用用户权限，workspace 内 patch 边界不是通用执行沙箱。

## 2. 原始历史与模型请求分离

入口：[observations.project](../../src/nexus/core/observations.py)、
[ContextBuilder / Context](../../src/nexus/core/context.py)、
[project_plan](../../src/nexus/core/plan.py)。

Session/JSONL 保存原始消息；模型请求是从原始逻辑上下文派生的视图：

```text
当前 run 历史 / safety snapshot
  → Observation Projection：HOT + 最近工作集保留 FULL，旧结果使用确定性 preview
  → 最新 Plan 快照
  → 本次任务 guidance / 剩余轮次说明
  → 容量检查、必要时摘要和重建
  → model.complete
```

HOT 是尚未被后续有效 assistant 消费的工具结果，不是按英文工具标题猜出的重要性。
工作集目标为 min(16K tokens, 输入预算 / 4)，HOT 不会因超出这个目标而被截掉。
Safety compaction 按完整 assistant/tool 组处理，保护 HOT、system 和用户输入；
不能为压缩而留下半组 call ID 配对。

Plan 和预算说明是 request-only 数据，不能逐轮向历史追加。检查、发送、usage 校准使用
同一份实际请求，避免“估算时装得下，发送时多塞一块”的错误。
动态状态合并到唯一的临时 user 后缀，稳定 system 和历史前缀可以命中服务端缓存；
内部类型把这个后缀与真实用户消息区分开，它不参与历史引用或最近交互组保护。
Usage 保存 provider 报告的 cached_input_tokens（输入的子集）；缺失与测得零必须区分，
成本分析不能把估算 token 或单次命中率冒充账单。
Java 后端类比：原始历史与查询投影分离；投影能重建，不能反向修改权威状态。

证据入口：`tests/test_observation_lifecycle.py`、`tests/test_safety_compaction.py`、
`tests/test_plan_context.py`、`tests/test_execution_budget.py`、`tests/test_request_context.py`、
`tests/test_cache_usage.py`。
局限：preview/摘要会损失信息，token 校准是估计；受保护内容仍超预算时诚实返回 context_limit。

## 3. 追加日志、提交顺序与恢复不确定性

入口：[SessionLog / replay / resume_session](../../src/nexus/app/session.py)、
[create_update_plan_tool](../../src/nexus/tools/plan.py)。

每条事件有 session_id、run_id、seq、timestamp。Plan 更新先校验并构造新值，
再 await 写入/flush `plan_updated`，成功后替换 Session.plan，最后返回工具结果。
完整事件已写入而 ToolResult 尚未写入时，恢复已提交 Plan；缺失工具结果仍按中断处理。
新 run 不继承旧 Plan，同 run 的 resume 保留最新清单。

这里借用了追加日志和提交记录的思想，不能宣称实现了数据库级事务。
日志 flush 不等于断电持久性；shell 副作用与日志也不是一个原子事务。
如果命令已执行、结果尚未保存就崩溃，恢复只能记为 `interrupted_unknown`，不能自动重跑。
这是处理外部副作用时必须承认的不确定窗口。

证据入口：`tests/test_plan.py`、`tests/test_session_context.py`、`tests/test_boundaries.py`。
面试追问重点：为什么不自动重试未知命令；为什么 Plan 状态要在事件提交之后才修改。

## 4. 补丁执行：降低生成负担，严格检查落点

入口：[parse_nexus_patch / resolve_update](../../src/nexus/tools/patch_format.py)、
[apply_patch](../../src/nexus/tools/patch.py)。

模型只提供路径、旧上下文和新文本，不必计算 unified diff 行号/行数。
解析器使用确定性匹配；重复上下文必须能唯一定位，不能悄悄选择第一个相似位置。
先检查整批格式、路径和旧内容，再写入；每次提交前再次核对原内容，检测预检后的变化。
单文件替换原子，但跨文件不是事务；发生部分写入时返回真实 changed_files、partial 和失败位置。

这与数据库迁移前校验、乐观并发检查有相似之处，但不保证其他进程并发写入时的完整隔离。
不能把一次 apply_patch 调用等同于成功修改，也不能把成功修改等同于测试通过。

证据入口：`tests/test_patch_v02.py`、`tests/test_tools.py`。

## 5. 可靠性结论如何成立

| 要回答的问题 | 看什么证据 | 不能用什么代替 |
| --- | --- | --- |
| 是否实际编辑 | 工具实际变更记录和收集到的 diff | 调用了 apply_patch |
| 修复是否符合回归要求 | 独立 validator 的结果、覆盖范围 | Plan completed / final 自述 |
| 是否自主收尾 | Agent outcome 与最后模型响应 | validator PASS |
| 成本和效率如何 | 模型调用、reported usage 覆盖率、耗时 | 仅看模型轮数 |
| 运行时是否遵守契约 | 离线失败注入、配对、恢复、预算测试 | 一次真实 case PASS |

Plan 是模型的进度声明，Detector 和 Execution budget 是提示。模型仍可能忽略它们。
2026-10-05 的 DeepSeek Requests/Sphinx 样本已经表明：提示实现正确，不代表行为目标达成。
见[原始对照记录](execution-budget-evidence.md)。比较不同模型必须保留同一任务、工具、
限制和独立 validator，并分别记录正确性与自主结束；小样本不能推出长期稳定成功率。

这些失效案例也是工程经验：先区分协议缺陷、执行器缺陷与模型决策行为，再决定改哪一层。
不要为单一 benchmark 标题写特判，不把“强制输出一段总结”算成任务已经完成。

## 演示顺序

1. 运行一次小型真实任务，展示工具调用、实际 diff、测试结果和 final。
2. 打开对应 JSONL，说明 assistant/tool 配对、run 隔离与公开/私有字段边界。
3. 用已有离线测试演示一个补丁冲突或中断恢复，说明失败时系统能保证什么。
4. 展示 Requests/Sphinx 的独立 validator 与 Agent outcome，对能力和未解决限制分别作结论。

准备讲解时优先把上述三个核心工程点讲透，无需再添加 RAG、多 Agent 或工作流框架凑技术栈。
