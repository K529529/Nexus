# 模型对照与交付记录（2026-10-05/06）

基线 `dcc1959f98d99350f1a89ef319fe5f0faf2b918d`，分支 `refactor/lean-agent-core`。
本文记录证据，不新增规划工作流，不把小样本通过率当作稳定性保证。

## 本轮决策与实验边界

用户允许把模型/服务对照与替换纳入交付，要求实际切换、费用先说明。
先复用现有服务 `https://maas.qianwenaiapi.com/compatible-mode/v1` 和统一 Conversation；
只临时替换传给 evaluate 的 Config，不修改 `~/.nexus/config.toml`。
评测发送公开 Requests/Sphinx 任务与隔离源码快照；Docker 不挂载 Nexus 工作区或宿主密钥。
Agent 容器继续关闭网络，任务、镜像、validator、工具和 40 步限制均不调整。

所有样本保留已有 Plan/Detector/Execution budget。Kimi context_window 设置为 262144，
其余沿用当前配置：max_output_tokens=8192、reasoning_effort 未设置。
不同模型的服务端默认思考行为可能不同，这是一组可部署配置的对照，不是严格能力归因实验。

实现取舍参考了两种成熟项目的公开机制：
[mini-SWE-agent](https://mini-swe-agent.com/latest/advanced/control_flow/) 保持模型动作/观察循环，
通过提交或预算退出；[OpenCode](https://opencode.ai/docs/agents/) 在达到 steps 上限后要求
只输出总结。后者能保证有界收尾，不能证明代码正确。本轮先验证现有循环的可用配置，
没有引入强制总结、第二模型 Judge 或固定 Explore/Edit/Test 状态机。

## Requests 样本

本地证据根目录：`C:\Users\Archer\.nexus\evaluation\results`。
PASS/FAIL 来自既有 Next Dev Set 本地 validator，不是官方 SWE-bench resolved。

| 配置 | run 目录 | validator | Agent | patch | input / output tokens |
| --- | --- | --- | --- | --- | --- |
| DeepSeek，已有执行预算 | `20261005T145750Z-43466c07` | FAIL | limited/max_steps，40 步 | 0 bytes | ≥1,138,426 / ≥34,131 |
| Kimi，首次 | `20261005T153921Z-d0266117` | FAIL | failed/empty_or_invalid_final，19 步 | 0 bytes | 270,529 / 9,998 |
| Kimi，空响应重试小修后 | `20261005T154837Z-72a2d0ad` | FAIL | limited/max_steps，40 步 | 0 bytes | 590,284 / 10,824 |
| Qwen，原 120 秒请求期限 | `20261005T155259Z-ad1a75db` | FAIL | failed/model_transport，4 步 | 0 bytes | ≥16,926 / ≥3,557 |
| Qwen，300 秒请求期限 | `20261005T155923Z-d1fe6386` | PASS | completed，34 步 | 4,359 bytes | ≥1,036,256 / ≥28,464 |

首次 Kimi：19 次模型请求、21 次 exec_command，没有 Plan 或 patch。
第 19 轮完整 stop 但没有正文或工具调用，Agent 拒绝把它当成完成。
第二次：40 次请求、40 次 exec_command，第 24 步产生 pre-mutation nudge，仍未编辑。
两次 Kimi usage 覆盖完整；第二次没有触发空响应重试，因此不能将其失败归因于重试修复。
两次 validator 都是 2 failed、201 passed、11 skipped。

对首次失败边界另外做一次只读 API 探测，没有执行返回的工具，也没有续写原会话：
复原相同的第 19 轮投影，输入仍为 18,729 tokens；服务返回标准 tool_calls，output 85 tokens。
只保存响应字段名称、长度、finish_reason 与 usage，未输出私有 reasoning。
证据在首次 run 的 `psf__requests-6028/protocol-probe.json`。
这支持“该边界可能恢复”，不能证明首次空响应的服务端根因。

## 小范围实现：完整空响应重试

只修改 `core/model.py` 现有两次 attempt 循环：完整流已 stop、无工具调用且正文为空白时，
同一请求最多重试一次。与连接失败/429/5xx 共用两次上限，不叠加新的重试层。

失败的空响应不写 assistant 历史、不执行工具、不将 reasoning 转成 final。
两次空响应仍失败；部分流、拒绝、格式错误、已产生工具调用的响应均不走此例外。
每次尝试独立记录 model_started/model_finished，失败响应的 reported usage 仍计入成本。
step 语义、completed/limited 判定、工具执行和 Plan 状态均不变。
这个修复处理协议韧性，未声称解决编辑或结束不收敛。

离线验证：64 项定向通过；全量 428 passed、10 skipped（原有 opt-in Docker 项）；
Ruff、mypy（54 files）通过。受限 Windows 环境首次取消测试因 cleanup_incomplete 失败，
在允许正常清理子进程的环境重跑通过。源码检查和请求/结果断言包括不重复工具、
不泄露 reasoning、累计全部用量和重试上限。

## 费用口径

[平台 Kimi 标价](https://www.qianwenai.com/models/kimi-k2.7-code)：
输入 ¥6.5/M、输出 ¥27/M，缓存命中输入 ¥1.3/M。
按 reported usage、全部输入按非缓存价格估算，两例分别约 ¥2.03、¥4.13；探测约 ¥0.12。
这些是公开价格估算，不是账户账单；实际折扣、缓存和结算以平台为准。

## 如何复现临时配置对照

使用已有环境和 evaluator；不需要编辑默认配置文件。以下操作会产生真实费用。

```python
import asyncio
from dataclasses import replace
from nexus.app.config import load_config
from nexus.evaluation.runner import evaluate

config = load_config()
config = replace(
    config,
    model=replace(config.model, name="kimi-k2.7-code", context_window=262144),
    limits=replace(config.limits, context_window=262144),
)
asyncio.run(evaluate(["psf__requests-6028"], config))
```

更换模型使用全新会话；不得复用绑定旧 service/model 的私有 reasoning continuation。
对照结果目前不支持采用 Kimi 为默认配置。

## 小范围实现：可配置完整响应期限

Qwen 首次样本第 1 轮用时 57,031 ms，output 3,099 tokens；第 4 轮在 120,000 ms
报 model_transport，usage 缺失。它尚未编辑，因此该样本不能用于判断自主完成能力。
时间吻合既有硬编码整流 deadline；日志不足以进一步断定模型计算慢还是网络停顿。

将原先两个硬编码 120 改为 `[model].request_timeout_seconds`，默认 120，允许整数 1..600。
SDK timeout 和外层整流 deadline 使用同一值；保持工具超时、max_steps、输出上限、重试政策
及完成判定。对照将这个参数临时设为 300，而不是给旧默认配置静默延长所有请求。

最终离线验证：timeout/模型/Conversation 定向 48 passed；全量 439 passed、10 skipped；
Ruff、mypy（54 files）、格式检查、离线 wheel/sdist 构建通过。
wheel 内容检查确认包含最终 timeout 配置和空响应处理。没有重跑完整 Windows A/B/C 或
全新机器安装；没有执行八项 suite，也没有官方 SWE-bench harness 评分。

Qwen 同平台公开标价为输入 ¥12/M、输出 ¥36/M，缓存命中输入 ¥1.5/M，见
[模型页面](https://www.qianwenai.com/models/qwen3.8-max)。中断请求缺失的 usage 不能计作零，
初次样本不能据已报告小计推断完整费用。

## Qwen Requests：修复与自主结束均有证据

运行时指纹 `8b20b5def28d4b62525de61c27b2febfdfdc3a5a6f69341605f05f909608ef59`。
模型 `qwen3.8-max`，context_window=1000000，max_output_tokens=8192，
request_timeout_seconds=300，reasoning_effort 未设置，max_steps=40。

- 第 4 步创建 Plan 并运行本地复现；第 10 步首次实际修改 `requests/utils.py`。
- 第 15–16 步补回归测试；第 17 步针对性检查得到通过结果，同时发现缺失 httpbin fixture。
- 第 18–19 步临时还原实现，确认新增测试能暴露原缺陷，再恢复补丁。
- 第 20–30 步扩大验证并多次对照原始状态；遇到网络测试失败、缺失 fixture 以及
  `/tmp/test_utils.py` 缓存引起的不一致。这一段仍有较高时间成本，不能说已消除过度验证。
- 第 31 步本地代理复现返回 200；第 32 步将 Plan 更新为 completed；第 34 步正常 final，
  如实报告环境限制，没有强制终止、额外 Judge 或伪造成功总结。
- 最终独立 validator：203 passed、11 skipped，状态 PASS；Agent outcome=completed。
- 41 次 exec_command、5 次 apply_patch、4 次 update_plan；50 次工具调用、35 次模型请求。
  第 31 步一次传输失败后按原有策略重试成功；usage 覆盖 34/35。
- Agent 用时 1003.515 秒，约 16.7 分钟。已报告 tokens 按未缓存标价折算约 ¥13.46，
  缺失的失败请求用量另计；并非账户实付账单。

运行中曾另取只读 HEAD diff 验证，保存于 `checkpoint-before-final/`。抓取时正好位于
第 29 步的 `git stash` 原始状态对照期间，因此 patch 为 0 bytes，validator FAIL。
该快照不代表最终候选，也不能当成最终修复的失败证据；未反馈给 Agent。

这说明现有轻量循环能够在这组配置下完成 Requests 的 edit → checks → final，
并不证明模型替换是唯一原因，更不证明所有任务稳定。Sphinx 继续使用相同配置核验。

## Qwen Sphinx：修复通过，收尾被账户欠费中断

run：`20261005T161743Z-0a32666e`，case：`sphinx-doc__sphinx-10323`。
运行时指纹、模型及全部参数与成功的 Requests 相同。临时控制台消费者只额外打印公开的
model_finished step/attempt/耗时，不改变请求、状态或工具执行。

- 第 10 步创建 Plan，第 14 步首次成功修改代码；此前两次 patch 定位失败均返回错误，未写文件。
- 第 22 步完成文档/changelog 修改，第 24 步补测试，第 25 步 dedent 定向测试 4 passed。
- 第 26 步请求 HTTP 403，Agent outcome=failed、reason=model_http_403，没有 final。
- 收集到 4,264 bytes patch，最终独立 validator：41 passed，状态 PASS。
- 24 次 exec_command、7 次 apply_patch（3 次成功实际变更、4 次失败）、1 次 update_plan。
  26 次模型请求，usage 覆盖 25/26；已知 input ≥637,201、output ≥20,691。
- 用时 491.545 秒，约 8.2 分钟。已报告 tokens 按未缓存标价折算约 ¥8.39；
  失败请求缺失用量另计。没有把 PASS 改写成 Agent completed。

重建失败边界后，以 max_tokens=1 做一次只读诊断，不执行工具、不修改原记录。
仍返回 HTTP 403，错误代码与类型均为 `AccessDenied.Unpurchased`；记录保存在该 case 的
`provider-error-probe.json`，含可供平台排查的 request_id，不含请求正文或私有推理。
[平台错误码文档](https://platform.qianwenai.com/docs/api-reference/preparation/error-messages)
将此类错误归为服务开通/模型访问资格；欠费另有 Arrearage 等代码。
当时不能仅凭该错误码断言余额耗尽；用户随后确认这次拒绝由百炼账户欠费导致。

浏览器工具两次初始化失败（failed to write kernel assets / os error 3），无法代查控制台。
该阶段暂停了付费采样，保留默认模型。2026-10-06 用户充值并授权自主 hardening 后，
已恢复低价 Flash 基线；浏览器恢复可用，控制台显示余额 ¥100.43、欠费 ¥0.00。
该余额是读取时的账户状态，账单可能延迟，不代表本轮可无上限消费。

## 模型对照阶段裁决（后续已进入自主 hardening）

- 保留单循环、Plan、Detector、补丁工具和现有完成判定，不继续叠加控制机制。
- 空响应重试与可配置期限是可离线验证的小修；后续按新的自主任务授权分别提交，未推送。
- Qwen 是值得继续验证的候选：Requests 已 PASS + completed/34；Sphinx 已 PASS，
  但正常收尾尚未完成验收。Kimi 两例不支持替换默认模型。
- 当前默认配置仍为 DeepSeek；[Qwen 候选配置](qwen-candidate.toml) 可在权限恢复后复用。
  配置文件不是模型路由器，CLI 不会自动发现或切换它。
- 该阶段的账户阻塞已解除。后续优先验证低价 Flash、输入缓存和固定八项 suite；
  Max 暂不作为默认模型，不据此引入新的运行时规划节点。
- 本轮共 5 个真实 case：Kimi Requests 两次、Qwen Requests 两次、Qwen Sphinx 一次。
  另有两次 API 探测、一次独立只读中间快照验证。已报告用量按未缓存价格合计折算约
  ¥28.46；缺失 usage、缓存折扣与账单结算未包含，不能当作实际扣费金额。
- 样本仍少，Requests 也仍有重复验证和较高延迟；没有声称长期稳定性已被证明。
