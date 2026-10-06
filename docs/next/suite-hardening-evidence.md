# Nexus 自主加固：结果与工程证据

**状态：本阶段已完成交付；全部已启动评测已结束，不再追加付费试验。用户同意保留原判分。尚未达到或证明稳定 8/8。**

本报告仅使用原 Next Dev Set V0 的判分结果。PASS 是冻结的本地 validator 结果，不是官方 SWE-bench resolved，也不是模型自己的完成声明。所有 case 评测使用原任务、原镜像和原 validator；另做的合成接口探针单独记录。

## 1. 八例对比

单元格为 `验证结果 · Agent outcome/步数`。基线是同一冻结配置在 Docker 中断恢复后分段执行，不能称为一次无中断运行；后续每列是一种统一配置，不能跨列挑最好成绩。旧 Flash medium 对照因连续长耗时而在 click 的第 39 步工具执行期间停止，已完成五例保留；click 不判分，pytest/rich 未运行。该中断 case 已报告 39 次调用，另有约 ¥0.887 已知消耗，未计入下面“已完成 case”成本表。

| Case | 冻结 Flash 基线 | Flash medium / 8K | DeepSeek high / 8K | DeepSeek high / 16K | Flash low / 16K total | DeepSeek high / 16K total |
| --- | --- | --- | --- | --- | --- | --- |
| pvlib | PASS（恢复后独立验证） · limited/40 | PASS · completed/37 | PASS · completed/22 | PASS · completed/28 | PASS · completed/33 | PASS · completed/28 |
| matplotlib | FAIL · completed/38 | FAIL · completed/24 | FAIL · completed/18 | FAIL · completed/34 | FAIL · completed/19 | FAIL · completed/15 |
| Django | FAIL · limited/40 | FAIL · limited/40 | FAIL · completed/40 | PASS · limited/40 | FAIL · completed/40 | FAIL · completed/40 |
| Requests | PASS · completed/24 | PASS · completed/23 | FAIL · failed/17 | PASS · completed/38 | PASS · completed/21 | PASS · completed/35 |
| Sphinx | FAIL · limited/40 | PASS · limited/40 | PASS · completed/37 | PASS · completed/21 | PASS · completed/22 | PASS · completed/34 |
| click | PASS · limited/40 | 中断（未判分） | PASS · completed/30 | PASS · completed/36 | PASS · completed/26 | PASS · completed/38 |
| pytest | PASS · limited/40 | NOT RUN | PASS · completed/40 | PASS · completed/32 | PASS · completed/37 | PASS · completed/39 |
| rich | PASS · limited/40 | NOT RUN | PASS · completed/20 | PASS · completed/30 | PASS · completed/23 | PASS · completed/35 |

基线汇总：**5/8 PASS，1/8 同时 PASS + completed，6/8 达到 max_steps**。matplotlib 虽 completed，验证仍失败。pvlib 原始结果保留 ERROR，表中 PASS 来自已保留补丁的独立验证恢复，不抹去 Docker 故障。

| 统一配置 | 已落盘 case | PASS | completed | PASS + completed | 已完成 case 的 Agent 总耗时 | 用量标价估算 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Flash medium / 8K | 5/8 | 3 | 3 | 2 | 75.1 分钟 | ¥3.270 |
| DeepSeek high / 8K | 8/8 | 5 | 7 | 5 | 34.5 分钟 | ¥3.395 |
| DeepSeek high / 16K | 8/8 | 7 | 7 | 6 | 40.1 分钟 | ¥4.727† |
| Flash low / 16K total | 8/8 | 6 | 8 | 6 | 39.6 分钟 | ¥1.536 |
| DeepSeek high / 16K total | 8/8 | 6 | 8 | 6 | 36.7 分钟 | ¥4.587† |

成本是 provider 已报告 usage 的估算，不是发票。† 表示用量覆盖不完整，数字不能当作完整总费用或严格上下界。缓存属于输入子集，缺失用量不当作零。DeepSeek 采用试验所在中国时间 22:00–08:00 的离峰价，白天成本不同。未完成 case 的实时消耗不包含在表中。

## 2. 主要失败族与最小改进

| 失败族 | 通用处理 | 证据与边界 |
| --- | --- | --- |
| 动态 Plan/预算改变 system 前缀，重复输入成本不可见 | 保持 system 与历史前缀稳定，动态状态用唯一 request-only 后缀；保存缓存用量及覆盖率 | Requests 缓存样本输入 1,164,513，缓存 701,696；该次已知成本 ¥0.512，相同用量不计缓存折扣为 ¥1.004。不能据此声称不同运行质量相等。 |
| 空响应、长推理请求超时 | 有界空响应重试、可配置请求期限；重试不重放工具 | adapter 回归测试；所有尝试计入 usage。原始 120 秒与 300 秒 Max Requests 试验已另行记录。 |
| 无法纠正的工具错误消耗轮次 | @@ 空 chunk 错误说明边界；缺少 patch 外层标记时明确正确格式；shell 参数错误列出所需 command/workdir/timeout_ms | Flash pvlib 重复空 chunk，Pro Django 重复缺少标记。三种真实 patch 错误均有“拒绝无写入→纠正重试成功”测试；Requests 另有 5 次 cmd/command 混淆，本地/容器共用更明确的错误说明，并验证非法调用不启动进程。后续 Django 仍出现 5 次该参数错误，提示未消除模型习惯。不放宽参数、解析或匹配。 |
| 执行环境限制未告知模型 | 受限容器的工具描述明确无互联网 | 真实 Conversation→模型工具列表测试；原 native ToolSpec 不变。后续轨迹仍有联网尝试，不能宣称提示已消除该行为。 |
| 过度探索/过度验证、到预算还未 final | 保留 Plan、Detector、执行预算与有限验证提示；对照合理模型配置 | 基线 6 例 max_steps，DeepSeek high/8K 0 例 max_steps；模型和提示同时变化，不能归因于单条 prompt。 |
| 自写测试只证明自己的实现、复杂语义修复不完整 | 两句通用提示：从任务/现有契约推导预期；优先先失败后通过的行为断言 | Flash Django 开始加入结果断言，仍未覆盖独立检查的相关语义。提示尚未证明提升总体 PASS 率，completed 和测试通过都不能代替独立验证。 |
| 输出被截断 | 拒绝执行不完整回复；统一 16K 输出配置试验同步调整 Context 保留预算 | DeepSeek Requests 17 步 finish_reason=length、输出恰为 8192，零补丁。没有把截断改为成功或自动重放。 |

这些处理不包含 case ID、仓库名、issue 特判、gold patch、阶段路由或额外模型 Judge。补丁与工具提示以真实失败类型为依据，适用于普通本地 coding task。

## 3. 语义失败与模型对照

matplotlib：公开问题是 BoundaryNorm cursor 崩溃；冻结检查还要求区间精度与边界值的具体格式。多数候选采用固定精度 fallback，另一些用边界邻居；能消除崩溃但未通过全部格式检查。公开问题提到 try/except 等候选办法，但没有逐一给出这些精度预期，存在验收充分性疑问。GLM 的独立验证也显示 26 项既有检查通过、1 项新增格式检查失败。用户同意阶段性交付：这里仍保留 FAIL，不改判分、不把隐藏预期塞进提示，也不声称已经证明 validator 错误。

Django：多个模型的 OuterRef 包装修复能消除报告中的异常，却未通过跨模型相关查询的结果断言。DeepSeek high/16K 后续通过独立验证，但仍 limited/40；该次包含 11,551-token 单次输出。一次成功尚不能证明稳定；最终代码的 total16K 重复对照中 Django 又 FAIL，因此不能将历史失败全部解释为 finish 问题。通过的 high16K/max32K 轨迹都在末段修复新出现的回归，第 40 步才更新完成 Plan；这不同于任务早已完成后的纯重复验证。

| 对照配置 | Run ID | matplotlib | Django |
| --- | --- | --- | --- |
| Max medium | `20261005T184346Z-d011ec65` | FAIL · completed/13 | NOT RUN |
| Max xhigh | `20261005T184937Z-a2671f30` | FAIL · completed/19 | NOT RUN |
| DeepSeek V4 Pro high | `20261005T191638Z-1c0a1423` | FAIL · completed/19 | FAIL · limited/40 |
| Qwen3.7-Plus 默认 | `20261005T193730Z-dfb55550` | FAIL · completed/29 | FAIL · failed/27 |
| DeepSeek V4.1 Flash max/32K | `20261005T194636Z-93ac3621` | FAIL · completed/25 | PASS · limited/40 |
| GLM-5.3-Flash high/16K | `20261005T202629Z-842b876f` | FAIL · completed/21 | FAIL · failed/34 |

GLM-5.3-Flash high/16K：两例均 FAIL，仅 matplotlib 正常 completed；Django 第 34 次模型请求达到 300 秒期限后以 model_transport 结束，冻结验证也失败；落盘补丁仅含测试和临时调试文件，没有生产代码修复，edit 收敛仍不能保证。Agent 总耗时 32.1 分钟，已报告部分估算 ¥0.707†，usage 覆盖 54/56。未运行剩余六例，不能当作完整八例结果，也不能把服务超时单独归因于模型能力。


昂贵模型尚未证明更可靠：Max medium matplotlib ¥0.445、Max xhigh ¥2.298，均 FAIL；Pro Django 已知消耗约 ¥6.378，仍 FAIL + max_steps，其中一次 transport retry 没有 usage。不切换默认到最贵模型，不根据单个成功样本建立自动路由。

两项被证据否定的疑点也保留：合成 Qwen 请求省略 preserve_thinking 时计入传回的 reasoning；合成 DeepSeek 请求从最后 tool 变为追加 user 后缀，输入为 7818→7827，清空合成 reasoning 才降为 325。这只是输入计费证据，不证明推理质量；未据猜测添加厂商开关或改变角色。探针只保存合成用量，不导出真实私有 reasoning。

## 4. 代码与验证

新增可选 `model.output_token_parameter`：默认 `max_tokens` 保持兼容；服务支持时选择 `max_completion_tokens`，使推理与回答共用总上限。只发送选定的一个参数，与 Context 使用相同 `max_output_tokens` 数值；无模型名分支或失败后自动换参。真实 Qwen Flash 合成探针在上限 32 下，旧参数输出 1756、新参数输出 32 并返回 length。详细证据见 [cost-hardening.md](cost-hardening.md)。

- 单循环与完成边界：`src/nexus/core/agent.py`。没有 Planner/Judge、自动完成 Todo 或按 PASS 结束 Agent。
- 请求投影与预算：`src/nexus/core/request_context.py`、`context.py`、`plan.py`。同一请求用于估算、容量检查、摘要、fallback 与 usage 校准；不污染 Session 历史。
- 服务适配与缓存计量：`src/nexus/core/model.py`、`core/types.py`、`app/profile.py`、`evaluation/report.py`。保留私有 reasoning 绑定与协议配对。
- 可纠正错误：`src/nexus/tools/patch.py`、`patch_format.py`、`execution.py`；受限环境说明：`src/nexus/evaluation/environment.py`。工具权限、schema、原子写入与路径边界未变。

GLM 直供接口的另一个合成探针：上限 32 时，max_tokens 输出 32/length，而 max_completion_tokens 被接受却输出 3552/stop。该服务明确使用前者，不能把 HTTP 接受参数当作上限生效。用户开通后工具调用探针通过，两例原始任务对照已结束。

当前离线验证：463 passed / 10 Docker opt-in skipped（30.23s）；定向模型/Context 测试 86 passed、工具测试 100 passed、环境接入 27 passed；Ruff `check --no-cache .`、mypy `src tests`、`git diff --check` 均 PASS。10 项 Docker 隔离/超时/中断/污染检查在本次 mission 前段全部 PASS（240.99s）；工具文案与可选输出参数改动后未重复运行。

`uv lock --check --offline` 与 `uv build --offline` PASS；wheel 在独立环境安装，从仓库外 `-I` 导入，确认包含当前 prompt、可选输出参数、补丁和命令错误诊断。既有 Windows 子进程退出探针曾偶发失败，原测试与完整套件重跑通过；未掩盖测试或修改无证据的清理逻辑。

Prompt 实际新增三行（两个句子），其余措辞和段落保留：

```text
Derive expected results from the task and existing contracts, not from the new implementation.
For a bug fix, prefer a focused regression test that fails before the fix and passes after it;
assert the intended behavior and preserve behavior outside the requested change.
```

## 5. 复现与审计

- 分支 `refactor/lean-agent-core`，mission 起点 `dcc1959f98d99350f1a89ef319fe5f0faf2b918d`；冻结 Flash 行为基线 `8b5cd07`。
- 本次恢复时 HEAD 为 `31e8672`。此前 capability 提交：`36989db`（预算/验证）、`8b5cd07`（重试/期限）、`f46e5c3`（缓存计量）、`7717e01`（稳定前缀）、`c07fa3f`（文档）、`31e8672`（汇总计量）。本恢复阶段按最新 AGENTS 只 stage，不自动 commit/push；保留 `.idea/` 与 `i_love_you.txt`。
- 所有报告/JSONL/patch/validator 在 `~/.nexus/evaluation/results/<run-id>/`；候选运行与独立 validator 隔离，当前用户默认已选为 Flash low/16K total，详见下面的配置与回退记录。
- 基线分段：`20261005T171354Z-1be8b311`（pvlib 恢复验证）、`20261005T175244Z-444cb820`（matplotlib、Django）、`20261005T184402Z-9aaeea84`（其余五例）。中断 Requests 的 PASS 仅记在 `interrupted-validation/`，未计为完整完成。冻结归档与原 manifest 的五个 raw hash 差异只来自 CRLF/LF，规范化内容相同。
- 全套对照：Flash medium / 8K: `20261005T184923Z-5493f208`, DeepSeek high / 8K: `20261005T190106Z-ca4d4dcd`, DeepSeek high / 16K: `20261005T193317Z-1fc159ff`, Flash low / 16K total: `20261005T200740Z-94df05e2`, DeepSeek high / 16K total: `20261005T201022Z-c712b795`。
- 早期缓存样本 `20261005T180013Z-fc51ba8a`：Requests PASS/completed36、pvlib PASS/completed33；早期中档对照 `20261005T181726Z-2e6054de`：Flash Requests PASS/completed19，估算 ¥0.110；`20261005T181841Z-985338dd`：DeepSeek Requests PASS/completed30，估算 ¥0.457。均保留，不当作当前 profile 的重复稳定性证明。
- 2026-10-06 05:05 中国时间，账单只读显示余额 ¥60.04、欠费 0；结算有延迟。没有充值、购买订阅或更改账户设置。

## 6. 成熟方案参考与面试重点

- [百炼 Context Cache](https://platform.qianwenai.com/docs/developer-guides/run-and-scale/context-cache)：稳定内容在前、动态内容在后；以实际命中用量验收，而不是只降低字符串长度。
- [Qwen Code prompt](https://github.com/QwenLM/qwen-code/blob/main/packages/core/src/core/prompts.ts)：运行时信息可放 user/tool 提示中，保持核心循环简单。
- [mini-SWE-agent 控制流](https://mini-swe-agent.com/latest/advanced/control_flow/)：小型 query/execute 循环、有限资源边界、明确的退出状态；没有引入其外部评测控制逻辑。
- 价格/参数：[Flash](https://www.qianwenai.com/models/qwen3.8-flash)、[DeepSeek Flash](https://www.qianwenai.com/models/deepseek-v4.1-flash)、[Max](https://www.qianwenai.com/models/qwen3.8-max)、[Pro](https://www.qianwenai.com/models/deepseek-v4-pro)、[Plus](https://www.qianwenai.com/models/qwen3.7-plus)、[GLM Flash](https://www.qianwenai.com/models/ZHIPU/GLM-5.3-Flash)、[Chat API](https://platform.qianwenai.com/docs/api-reference/chat/openai-chat)。

面试最值得讲的五点：

1. 模型是不可靠外部决策服务，单循环把协议校验、工具执行、资源边界留在确定性代码中。
2. 原始 Session 与请求投影分离：工具配对、HOT 保护、预算校准与 prefix cache 可以同时成立。
3. Plan 的持久化提交先于内存替换，以及 resume/run 隔离；类比 Java 后端的事务提交与读模型。
4. 文本补丁的严格解析、路径边界、逐文件原子写入，以及能够指导重试的错误反馈。
5. 验证结果、Agent outcome、token 覆盖率与价格分开记录，用负面实验约束复杂度。

详细代码讲解见 [engineering-walkthrough.md](engineering-walkthrough.md)。不要把这些工程能力包装成已实现稳定 8/8；单次成功也不足以证明长期稳定。

## 7. 日常配置与回退

两组最终代码的完整对照均为 6/8 PASS、8/8 completed、0 max_steps。选择 Qwen3.8-Flash low 作为本阶段成本优先的日常默认：整轮约 ¥1.536；DeepSeek high 同轮已报告部分约 ¥4.587。不能据此宣称两模型能力相同或已验证长期稳定。此前 DeepSeek 的 7/8 单轮仍保留，Django 不稳定也保留。

用户级配置已备份并更新，endpoint、key 环境变量、context_window=1000000、max_steps=40、execution 和 MCP 均保留。变更字段如下：

```toml
[model]
name = "qwen3.8-flash"
reasoning_effort = "low"
max_output_tokens = 16384
output_token_parameter = "max_completion_tokens"
request_timeout_seconds = 300
```

位置：`C:/Users/Archer/.nexus/config.toml`；原文件备份：`config.toml.before-hardening-20261005T210008Z`（同目录）。实际 load_config 验证与 model/limits 输出预算一致。该用户文件在仓库外，不进入 Git；公开选择依据与字段记录在本报告。

模型切换后开始新会话。需要恢复旧 DeepSeek 会话时，恢复原模型/服务设置；私有 reasoning 的绑定校验保持生效。此次没有引入自动模型切换。CLI 本地配置回读已验证；不额外启动付费测试。

## 8. 本阶段提交安排

实现与测试文件已经逐项 git add；不自动 commit/push。建议按以下能力拆分提交，已存在的六个 mission 提交保持原样。

| Commit message | 文件组 |
| --- | --- |
| `feat(model): configure provider output token limit semantics` | app/config.py、core/model.py、test_model.py、两份 README、cost-hardening.md |
| `fix(tools): provide actionable validation and environment feedback` | tools/execution.py、patch.py、patch_format.py、evaluation/environment.py、test_evaluation_v0.py、test_patch_v02.py |
| `feat(agent): guide behavior-based regression checks` | core/context.py、test_session_context.py、本证据报告 |
