# Tokenizer Performance Instrumentation

2026-10-22，分支 `codex/nexus-v0.3-stage1`，基于 Final P1 `101a4ae`。
本轮只增加本地计时及已有事件的聚合字段；Tokenizer 算法、模型、容量、
Context 策略、Compaction Prompt/Schema、Checkpoint 与恢复逻辑保持不变。
不新增缓存，不减少或合并原有 Token 计算调用。

## 计时口径

`TokenEstimator.raw()` 用 `time.perf_counter()` 的 try/finally 包围原有完整计数：
包括消息/工具协议渲染、分词、首次资源加载；失败调用也计数。
`count()` 和 Usage 校准最终调用同一个 raw 入口，因此不会嵌套重复计时。
网络等待、模型 API、工具执行、事件写盘均不在此计时区域内。
它不是整个 Context prepare 的 wall time；scope hash、校准比例算术、
Runtime Facts 旧 byte-budget 估算等外围工作不在 tokenizer 时间中。

每个 Agent Step 开始重置两套模型的**观测计数器**，不重置校准或 Context 状态。
已有事件新增以下字段，无逐次分词事件：

| 字段 | 语义 |
| --- | --- |
| `token_count_ms` / `token_count_calls` | 当前 Step 两模型计数累计总量 |
| `main_token_count_ms` / `main_token_count_calls` | 主模型分量 |
| `compaction_token_count_ms` / `compaction_token_count_calls` | 压缩模型分量 |
| `history_split_ms` | 当前 Step 的 History Split wall time；**包含内部计数** |
| `performance_scope` | 标明当前累计快照的边界 |

`context_projection` 保持请求前写入，scope 为 `step_before_request_cumulative`。
正常请求此处含 4 次计数：prepare 1、check 1、diagnostics 2。
响应后既有 diagnostics 2、observe 1 继续累计，完整 7 次计数写入已有
`context_usage`，scope 为 `step_after_usage_cumulative`，同时记录 step。
没有为了得到一个“完整 Step”指标移动事件或改变请求顺序。

同 Step Overflow 重试时，第二个 projection 是累计快照。
**取该 Step 最后的 context_usage；没有 usage 时取最后的 projection，并视为不完整边界。
不要把同 Step 的 projection/usage 相加。** 失败压缩的后续工作另有 compaction_failed
阶段记录，不能把缺少最终 usage 的 Step 宣称为完整成功测量。

Compaction 的 started/usage/failed 事件附加本次 `history_split_ms` 和压缩模型
累计计数（scope=`compaction_attempt_cumulative`）。正常路径 started 时计数 1 次，
usage 时累计 3 次。阶段累计值与 Step 总量也有重叠，不能再次相加。
`history_split_ms` 已包含内部主模型计数时间，不能与 token_count_ms 简单相加。
`context_compacted` 数据和 checkpoint 中不写性能字段；原有 API duration 字段保持原义。

## 离线实验

命令：`uv run python scripts/token_performance.py --output artifacts/token-performance-new`。
输出目录必须不存在，防止覆盖旧证据。脚本只使用本地 Tokenizer 和固定 OfflineModel，
不创建 API Client，工具执行函数会直接报错。**付费 API 调用数为 0。**

平台 Windows 10 / Python 3.12.10。固定英文、中文、Python、JSON 混合合成历史，
含工具定义、原有空 Plan 快照、执行预算后缀；约每 3000 字符一个 assistant 历史片段。
按实际主模型完整请求计数冻结为准确的 8000/64000/110000 Tokens，
对应请求消息数 19/131/221。每档 warm 运行 3 次，保留所有样本，以中位数汇报。
输入哈希 `1c537370073c3afee40439fda4d01bb7ec83a15fa8b8a23add1314dc7be8f8af`。
三档都低于既有自动压缩阈值，因此普通 Agent Step 不实际触发 Compaction；
History Split 和压缩模型计数作为独立离线操作测量，不执行摘要模型。

| 主模型完整输入 | 单次完整计数 | 普通 Step 累计（7 次） | History Split | 压缩模型单次计数 |
| --- | ---: | ---: | ---: | ---: |
| 8000 Tokens | 10.26 ms | 69.37 ms | 67.62 ms | 9.92 ms |
| 64000 Tokens | 96.43 ms | 636.78 ms | 558.06 ms | 62.23 ms |
| 110000 Tokens | 161.45 ms | 1141.82 ms | 614.24 ms | 121.46 ms |

压缩模型输入采用切分得到的 prefix 和未改动的 summary_input，实际为
8728/46972/95687 Qwen Tokens。8K 历史小于 20K 保留目标，split 无可压缩 prefix；
8K 行的压缩模型计数使用全历史构造离线输入，仅测计数，不冒充可执行压缩。
Split 内部计数次数分别 15/43/43；64K/110K 均保留 42 条消息，前缀为 86/176 条。

另测一次 8K 冷资源加载：DeepSeek **205.94 ms**，Qwen **387.06 ms**。
这是清空既有资源加载缓存后的首次计数，包含本地加载；不是 warm 延迟，也不是 API 时间。

原始三次数据、冷启动结果、机器环境和冻结输入：
[`evidence/tokenizer-performance/report.json`](evidence/tokenizer-performance/report.json)、
[`measurements.jsonl`](evidence/tokenizer-performance/measurements.jsonl)、
[`inputs.json.gz`](evidence/tokenizer-performance/inputs.json.gz)。

实验偏差完整保留：最初因目录权限未启动；首次输入构造漏了现有空 Plan 快照，
断言拦截，未形成有效测量。修正后 v2 后半段与全量测试负载重叠，完整数据存为
`overlap-attempt.*`，不用于正式表格。正式 v3 在本任务测试结束后独立执行；
与 v2 输入 SHA 完全一致，没有按耗时换样本或筛选重复次数。
这些是本机有限合成输入，不代表所有历史形态或生产机器性能。

## 热点与验证

110K 的单次计数约 161 ms，但普通 Step 原有路径计数 7 次，累计约 1.14 秒；
History Split 通过反复计数逐渐扩展的尾部选择边界，64K/110K 各调用 43 次，
切分约 0.56/0.61 秒，绝大部分时间位于内部 Token 计数。
这是可见的本地成本，不归因于网络。本轮只报告热点，不引入缓存、增量分词或切分重构。

回归覆盖：正常 Step 的 4/7 次累计、跨 Step 重置、异常计数、模拟 30/50 秒 API 等待排除、
压缩 1/3 次累计、预检失败、Overflow 同 Step 重试、JSONL replay 和 checkpoint 等价。
已有 profiler 事件等价测试固定新增 perf_counter 时钟，继续逐字段比较业务与事件结果。
全量测试、Ruff、Mypy、lock check、build 及阶段提交的 Windows/Linux CI 结果见交付回复。
