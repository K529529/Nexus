# Nexus V0.3 工程讲解：代码、取舍与证据

这是当前实现的代码导读，不新增架构契约。[使用手册](usage-guide.zh-CN.md) ·
[Release Notes](release-0.3.0.md) · [验证记录](release-0.3.0-validation.md)。
原始 V0 设计和早期实验保留，当前连续上下文已替代 HOT/COLD 与 Run 级历史过滤。

## 1. 一个手写消息循环

入口：[CLI](../../src/nexus/app/cli.py) →
[Conversation 装配](../../src/nexus/app/bootstrap.py) →
[run_turn](../../src/nexus/core/agent.py)。

```text
用户任务 / 待处理指导
  → ContextBuilder 组装模型请求
  → ChatModel / LiteTransport
  → 保存 assistant
  → 有 tool_calls：逐项经过 Registry / Gateway，保存配对结果，再请求模型
  → 无 tool_calls 的合法 final：结束核心执行
```

模型选择探索、编辑、测试和修复动作；运行时负责协议、预算、授权、持久化和清理。
没有 Planner/Validator/Repair 图节点，不以输出关键词替代独立验证。
Java 后端类比：ToolSpec 是调用契约、Registry 是分发注册表，ToolResult 是明确回执；
模型是可失败的外部决策服务，不能承担权限校验或持久化事务职责。

同一个 Loop 支持 interactive、exec、受限后端和子代理。工具批次内顺序执行并保持
assistant/tool ID 配对；多个代理可异步运行，不等于同一工具批次无序并发。

## 2. Session 与 Run、压缩与执行事实

[Session](../../src/nexus/app/session.py) 保存跨 Run 连续历史；Run 重置执行预算、
Plan 和自动 Skill 加载状态。`/new` 换 Session；普通下一次输入不清空历史。

```text
原始 Session JSONL + 唯一压缩 checkpoint
  → 历史摘要 + 近期完整原文 + checkpoint 后的消息
  → 当前 Plan / Skills / memory snapshot / guidance 等请求上下文
  → tokenizer / provider usage 校准 / 容量检查
  → 主模型调用
```

[Context](../../src/nexus/core/context.py) 在主输入窗口约 90% 时调用独立压缩模型；
[history](../../src/nexus/core/history.py) 按完整消息与工具组切分，目标保留约 20K 近期 token。
[compaction](../../src/nexus/core/compaction.py) 的 V4 摘要保存意图、约束、决策、进度和下一步；
[Runtime Facts](../../src/nexus/core/runtime_facts.py) 独立保留执行回执、退出码、Patch 路径/hash、
未知副作用与 Run outcome。原始 JSONL 不被摘要改写。

主模型输入窗口与 API 输出上限分别管理，不能从模型名字猜容量。
结构校验不证明摘要语义真实；命令 exit=0 不证明项目测试充分或用户目标已达成。
保真失败和权衡见 [实验原文](compaction-simplification.md)。

## 3. 持久化、恢复和取消

[durable](../../src/nexus/core/durable.py) / [tool_recovery](../../src/nexus/app/tool_recovery.py)
区分 PREPARED（未分发）、DISPATCHING（可能有副作用）、COMPLETED（完整结果已保存）。
重要 checkpoint 和 Patch ACK 前执行 flush/fsync；普通 JSONL 追加不能等同完整数据库事务。

恢复只续跑有明确未分发证据的调用，通过当前 Gateway；已完成结果复用，未知调用不重放。
Patch manifest 与 before/after 哈希支持只读核对，不虚构缺失的完成回执。
损坏尾部保留原文件并建立新副本；单写者锁避免同一日志被两个会话同时续写。
取消等待工具树清理；cleanup_incomplete 保留未知状态，不把取消异常当作干净成功。
这是外部副作用的保守恢复协议，不是跨任意 Shell/MCP 的 exactly-once 事务。

## 4. 工具、权限与平台后端

[Registry](../../src/nexus/tools/registry.py) 与
[SecurityGateway](../../src/nexus/core/security.py) 分离工具实现和授权。
Local 使用宿主用户权限。Windows Native 是 token/ACL/Job 的部分强制隔离；
Linux/WSL Docker 使用非 root 容器和明确挂载。不可用时 fail closed，不回退 Local。
精确 Patch 一次性批准绑定 request/call/参数摘要/范围，不转化为任意 Shell 权限。

Patch 提前解析并检查范围、非普通文件、链接、上下文和写入顺序；
它降低模型生成文本格式的负担，不证明补丁业务逻辑正确。
MCP 是显式 stdio 工具来源，Skills 是受预算约束的说明文本，Web 是 Local 的显式可选能力。
这些都复用工具事件/权限路径，不建立第二套编排框架。
[平台限制](stage4-platform-backends.md)明确网络、ACL、读权限和清理的真实边界。

## 5. 长期记忆与模型接入

[MemoryStore](../../src/nexus/app/memory.py) 以用户级 Markdown 为权威；QMD 仅索引检索。
Session 启动保存稳定快照，后续写入与跨会话失效有独立控制；关闭 Memory 换新会话，
不会把旧快照默默携带进“关闭记忆”的下一次请求。记忆默认开启，首次可触发依赖/模型下载。
恢复删除、Daily 和退出摘要仍可能失败，状态必须可见。

[LiteTransport](../../src/nexus/core/transport.py) 支持 openai/deepseek/dashscope，
独立管理模型客户端、重试和关闭。`/model` 切换主模型，压缩模型与已创建子代理快照独立。
不引入 Router、自动 fallback 或外部 tracing callback 驱动业务流程。

## 6. 多 Agent 与终端输入

[Subagents](../../src/nexus/app/subagents.py) 是同一 Conversation/Loop 的生命周期管理：
Fresh 上下文、原实例/历史续接、全树活跃额度、MCP 权限交集和父子结果交付。
默认关闭；角色是协作约定，不是文件锁或 Shell 只读安全隔离。共享工作区要求划分编辑范围。
已确认接收的收尾指导由原实例处理后才越过完成边界；取消优先，不复活取消任务。

[Inbox](../../src/nexus/core/inbox.py) 保存接收/投递边界，
[TaskInput](../../src/nexus/app/input.py) 区分草稿、已提交未交付和已交付消息。
审批接管输入时保存草稿并收拢已提交消息，不通过取消监听丢弃它们。
[ConversationScreen](../../src/nexus/app/screen.py) 持有终端渲染区域与代理会话视图，
避免 Rich 和输入组件争用终端。普通 Enter 指导、Tab 排队，用户无需命令发指导。

## 7. 证据是运行时之外的独立结论

本地会话 JSONL、八项评测 history/report 和外部 metadata-only OTLP 是不同查看路径。
[agent_history](../../src/nexus/app/agent_history.py) 按实际模型请求汇总全树用量：
缺失保留 unknown，缓存不重复加，父结果快照不重复累计。并行时长不相加成墙钟耗时。

确定性测试覆盖边界和协议，真实模型 smoke 证明指定服务链路，官方 verifier 验证候选。
三者不能互相替代。历史失败、环境错误、跳过项与未验证平台见各自原报告；
发布 CI 不证明真实模型成功率提高。ABK 固定 `0fd7fa4`，最终发布候选使用后续提交。

## 开发者阅读顺序

先读 types → agent → Conversation → Gateway/执行器 → session/durable → context/compaction，
最后读 Inbox/Subagents/Screen。对 Java 后端转 Agent 开发，优先掌握异步取消、
外部调用失败、工具协议配对和持久化提交顺序，再考虑增加能力。
对应测试：test_agent、test_conversation、test_durable_execution、test_session_compaction、
test_stage8_handoff、test_stage8_completion、test_stage8_screen。
