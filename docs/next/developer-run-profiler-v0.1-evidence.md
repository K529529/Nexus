# Developer Run Profiler V0.1 — implementation evidence

## 范围与入口

仅本地、可选的开发诊断；无模型/工具额外调用，不修改 Context Runtime V0.1.1、
安全压缩、run 隔离、resume 或会话消息正文。

```powershell
.\.venv\Scripts\nexus.exe --profile
.\.venv\Scripts\nexus.exe exec "解释当前项目结构" --profile
.\.venv\Scripts\nexus.exe resume --profile
```

`--profile` 是一个完整参数；与 exec 的 `--json` 互斥。不启用时终端行为保持原样。
交互模式每次任务结束后报告，再等待下一次输入。

## 实现与指标口径

| 文件 | 变更 |
| --- | --- |
| `src/nexus/app/profile.py` | 小型 run/model/tool 数据结构、事件聚合、失败隔离和 Rich 报告 |
| `src/nexus/app/cli.py` | 参数解析、消费者包装、drive 返回后输出、实际 SessionLog 路径 |
| `src/nexus/core/agent.py` | 四类模型/工具边界事件增加 step；tool_finished 增加字节数、exit_code、truncated |
| `tests/test_profile.py` | 离线聚合、等价性、CLI、多轮隔离、诊断失败与边界测试 |
| 两份 README、`01-development-design.md`、本文 | 启用方式、事件字段和验收证据 |

- `step` 由 Agent Loop 给出，仅为 RuntimeEvent 元数据，保留 attempt 和 compaction purpose。
- `result_bytes` 是完整模型可见 ToolResult 序列化 message.content 的 UTF-8 字节数，
  含 JSON 包装；衡量已有输出预算处理后的 observation，不是原始进程无限输出或 token 数。
  `tool_finished` 不重复正文。
- Model calls 包含每次重试和摘要请求；Compaction calls 单列摘要请求。
  Compactions 统计成功的 `context_compacted` 事件。累计 tokens 使用所有请求 reported usage；
  任一请求该字段缺失时，对应累计值为 unknown，不假设失败请求免费。
- Active context 仅取成功普通请求的输入。若其中有未知输入，峰值也显示 unknown。
  预算为 `context_window - max_output_tokens - 1024`，并非已消耗总 tokens。
- 逐步表取该步成功普通请求的 input/output/latency；没有成功请求则展示最近尝试的已知事实。
  不超过 10 步全显，否则前 3 + 后 7，中间标明省略。
- Tool calls 统计 tool_started；Failed results 包括未派发的 synthetic not_executed 等失败结果，
  因此取消时失败结果数可能大于实际派发数。字节数统计全部结果，slowest 取已观察到耗时的
  已派发工具；失败按 error_code 分组，exec_command 保留 exit_code 可见性。
- 每次 run_started 重置统计，即使 resume 保留同一 run_id，也只报告本次执行段。
  原始 JSONL 仍是完整轨迹；报告只使用真实 writer.path，缺失时显示 unknown。
- ProfileConsumer 隔离采集/渲染异常，继续传递原事件及运行结果；不收集或展示工具正文和参数。

## 验证

2026-10-02，Windows 当前 `.venv`：

| 检查 | 结果 |
| --- | --- |
| pytest 全量 | PASS：163 passed；其中 profiler 新增 24 个测试用例 |
| ruff check . | PASS |
| ruff format --check src tests | PASS：34 files |
| mypy src tests | PASS：34 source files |
| git diff --check | PASS |
| 本地 nexus.exe --help / exec --help | PASS：暴露 profile 参数和 json 互斥用法 |
| 真实云模型任务 | NOT RUN：本轮明确不执行 |

等价性测试对照模型请求、会话消息、工具派发、RunResult 和公开事件；额外用真实
Context.prepare 安全压缩路径对比开关前后的压缩投影和请求，确认没有改变执行行为。
CLI 测试覆盖普通模式不输出报告、报告在任务结束后输出、真实日志路径、两轮独立统计、
参数互斥和采集/渲染故障降级。全部采用离线脚本模型和工具，不调用付费/云模型。

V0.1 只提供当前执行段的事件派生统计，不做历史 JSONL 汇总或计费金额计算。
提供方缺失 usage、未完成请求、缺失耗时保留 unknown；不新增后台采样或 Context Runtime V0.2。

## 离线报告示例

以下来自 `tests/test_profile.py::fixture_profiler` 的合成事件，经实际 renderer 生成，
不是云模型运行数据。Fixture 没有 SessionLog，所以路径诚实显示 unknown；另有真实
临时 SessionLog 的 CLI 测试验证报告使用 writer.path。

```text
─────────────────────────────────────────── Run Profile ────────────────────────────────────────────
Outcome                              completed
Failure reason                       -
Duration                             10.0s
Steps                                2
Model calls                          4
Compaction calls                     1
Tool calls                           2
Tokens (cumulative)
  Input total                        1,200
  Output total                       60
  Total                              1,260
Active context (normal completions)
  First input                        100
  Peak input                         200
  Final input                        200
  Context budget                     5,000
  Peak / budget                      4.0%
  Compactions                        1
Tools
  Result bytes                       3.0 KiB
  Failed results                     1
  Slowest observed                   exec_command · 4.2s
 Failure breakdown     Count  exec exit codes
 command_exit_nonzero  1      7 ×1
 Step  Input  Output  Model latency  Tools  Tool bytes
 1     100    10      1.9s           2      3.0 KiB
 2     200    20      2.3s           0      0 B
Trajectory: unknown
```
