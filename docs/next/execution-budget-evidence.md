# Execution budget 与验证收尾：实现及验收记录

基线：`dcc1959f98d99350f1a89ef319fe5f0faf2b918d`。本文只记录本轮实现和观测，不建立新的架构契约。

## 小范围实现

- `core/agent.py`：沿用现有 step，在每次任务请求前设置本次执行窗口的预算说明；fallback 使用同一轮数。
- `core/context.py`：在原 system、项目/环境、Plan、一次性 guidance 后追加 request-only 预算；复制 Message，保留 seq/run_id/protocol_data，不写 Session 或 safety snapshot。摘要请求不携带任务轮次预算。
- 容量检查、重建和 observe 使用同一份实际请求。估算包括预算文字；校准缓存键只排除确切的运行时预算后缀，避免计数变化使校准每轮失效。
- `core/stagnation.py`：只补充提示文字，阈值、计数、一次性发送和 mutation 判定不变。
- 不新增 Planner、Judge、finish tool、后置 Detector、强制 final 或自动完成 Plan。最后一轮工具执行、limited/max_steps、合法 final 的语义保持不变。

预算说明示例（每轮从逻辑上下文重建）：

```text
Execution budget (current execution window):
Current model turn: 36 / 40
Remaining model turns, including this one: 5
Tool calls need a subsequent model turn to inspect their results and respond.
This is a limit, not a target; finish earlier when the task is done.
```

## 实际 Prompt 增量

原 SYSTEM 全部原句与八段结构保留，只在验证段加入三句：

```text
After relevant checks pass, repeat or expand validation only for subsequent edits, new failures,
an unmet requirement, or a concrete regression risk.
Preserve test exit status when filtering output, and distinguish code failures from missing test
infrastructure.
If required checks are blocked by unavailable infrastructure, use a relevant local check if
possible, then report the limitation instead of repeatedly investigating the environment.
```

原 Detector guidance 追加两句：

```text
Treat a proposed cause as a hypothesis; test it against the observed behavior with the
smallest distinguishing local check.
If external references or the exact test environment are unavailable, use a local check
of the same behavior rather than repeating failed lookups without a new lead.
```

## 离线验证

- 定向 Agent / Context / Plan / Detector / Session 测试：101 passed。
- 全量 `pytest`：424 passed，10 skipped（既有 opt-in Docker 测试）。
- Ruff、mypy（54 files）、format check、`uv lock --check --offline`、`uv build --offline` 通过。
- wheel 脱离源码的导入、`--version` / `--help` 通过；显式检查 Nexus 模块均来自 wheel。离线缓存缺少完整依赖 wheel，复用了当前锁定环境依赖，未验证全新机器安装。
- 覆盖实际 mock API 请求、预算容量、跨轮校准、摘要/重试重建、resume、新 run、原始 JSONL 不受投影修改，以及 assistant/tool 配对和 reasoning continuation。

## 两次真实评测（用户明确授权，各一次）

运行时代码指纹均为 `73e586e137323e241661b1809bd2f0a56960d0dc8838ea0d90eaf342d99f7d33`。模型 `deepseek-v4.1-flash`、endpoint、reasoning、context_window、max_output_tokens、40 步限制、工具执行语义、任务/镜像/validator 均未调整。两次之间没有修改运行时代码，没有运行八项 suite。

本地证据根目录：`C:\Users\Archer\.nexus\evaluation\results`。PASS/FAIL 指既有 Next Dev Set 本地 validator，不是官方 SWE-bench resolved。

| 样本 | run 目录 | validator | Agent outcome | 首次实际 apply_patch 修改 | 工具调用 | Agent 用时 |
| --- | --- | --- | --- | --- | --- | --- |
| 修改前，用户最新 Requests | `20261005T143101Z-827bc211` | PASS | limited/max_steps，40 步 | 26 | 61 | 339.2 s |
| 本轮 Requests | `20261005T145750Z-43466c07` | FAIL | limited/max_steps，40 步 | 无 | 40 | 352.0 s |
| 本轮 Sphinx | `20261005T150442Z-29a11a66` | PASS | limited/max_steps，40 步 | 30 | 51 | 328.9 s |

Requests 的公开事件证据：

- 40 次全部是 exec_command，0 次 update_plan，0 次 apply_patch；patch.diff 为 0 bytes。
- 第 24 步记录 pre_mutation nudge；第 25 步 model_started 明确标记 `stagnation_nudge_included=true`。
- 多轮寻找外部版本、缓存、标准库及 urllib3 实现。第 35 步才用本地调用直接观察到 `prepend_scheme_if_needed` 丢失认证信息；第 36–40 步仍在读取实现，没有编辑。
- validator：2 failed、201 passed、11 skipped；两个失败用例都是认证信息保留回归。
- 41 次实际模型请求包含一次重试；reported usage 覆盖 40/41，已知 input ≥1,138,426、output ≥34,131 tokens。缺失重试用量不按零计算。

Sphinx 的公开事件证据：

- 46 次 exec_command、5 次 apply_patch，全部 patch 调用实际修改文件；0 次 update_plan，最终 patch.diff 为 4,211 bytes。
- 第 30 步修改 filter 顺序，第 32 步补测试，第 35 步补文档与 changelog。
- 第 36 步 `tests/test_directive_code.py` 41 passed；第 39 步扩大 HTML 相关验证，20 passed，同时发现 flake8 不可用。
- 第 40 步又增加 auto-dedent 测试，之后没有 Agent 内重跑或 final。该追加测试可能有覆盖价值；日志不足以将末尾全部行动一概认定为无意义验证，但它没有留下完成验证和回复的预算。
- 仍出现测试/检查命令经 `tail` / `head` 管道丢失原命令退出状态的写法，新增 Prompt 没有保证模型遵守。
- 41 次实际模型请求包含一次重试；reported usage 覆盖 40/41，已知 input ≥1,082,624、output ≥27,151 tokens。

两项均没有 safety compaction；真实 provider context-limit fallback 未触发，该路径由离线 mock 测试覆盖。Requests 和 Sphinx 的执行预算说明来自已验证的 request-only 路径，不作为历史 Message 保存；不能将日志中的进度声明当作独立完成证明。

## 本轮结论与限制

实施与离线契约通过，真实行为目标未通过：两个样本均未主动结束，Requests 也未编辑。不能把本轮交付描述为已经解决 edit/finish 收敛。

修改前 `20261005T142110Z-b3dd2a22` 的 Requests 已出现过 40 步、0 patch、FAIL；修改后的失败不能单独证明本次变更导致回归，也没有形成改善证据。既有 Sphinx 最近四次均 PASS，但 `20261005T081903Z-5279bb7f` 实际是 completed/39，之后三次才是 limited/40；这些样本早于 Apply Patch V0.2，不是只改变本轮提示的严格对照。

本轮没有为追求 PASS 调整模型、工具、评测或终止判定，也没有追加云采样。若后续要把提示升级成运行时决策或完成门控，需要先与用户讨论控制语义。本记录及全部实现/测试文件随交付暂存，由用户决定 commit/push。
