# Compaction Fidelity Hardening：修复与负面结果

日期：2026-10-22。分支 `codex/nexus-v0.3-stage1`。不进入用户验收，不合并 main 或 V0.3 主分支。

后续实现与 24 次双模型减法实验见 [Compaction Simplification](compaction-simplification.md)。本文保留为 V2.2 的历史证据，不代表当前 V4 输出契约。

**结论：确定性来源校验、可更新摘要和兼容机制已实现；真实实验没有证明整体保真改善。**
最终 Prompt 三例只有一例通过结构校验，且该例严重遗漏信息；另两例结构被拒绝。
不能以“拒绝了坏摘要”或离线测试通过宣称保真成功。此实现应作为待独立审查的阶段候选，
不作为已通过真实模型验收的版本。所有失败、原始响应与人工判定一起交付。

## 1. pi 源码研究与实际取舍

参考固定提交 `bd3c9b823762b7383954aa49e13ee350471015fe`，并非浮动 main：

| 参考位置 | 参考机制与 Nexus 取舍 |
| --- | --- |
| [utils.ts:120 serializeConversation](https://github.com/earendil-works/pi/blob/bd3c9b823762b7383954aa49e13ee350471015fe/packages/coding-agent/src/core/compaction/utils.ts#L120) | 按角色表达历史、去掉 thinking；Nexus 保留 JSON 但显式区分任务信息、模型陈述、工具回执，不续写原对话 |
| [utils.ts:161 System Prompt](https://github.com/earendil-works/pi/blob/bd3c9b823762b7383954aa49e13ee350471015fe/packages/coding-agent/src/core/compaction/utils.ts#L161) | 独立摘要职责；不带 Coding Agent 指令/工具，不把历史问题当当前任务 |
| [compaction.ts:507 First / 540 Update](https://github.com/earendil-works/pi/blob/bd3c9b823762b7383954aa49e13ee350471015fe/packages/coding-agent/src/core/compaction/compaction.ts#L507) | 首次整理与增量更新分开；状态可修订、旧步骤可失效，非永久追加文本 |
| [compaction.ts:60 文件操作提取](https://github.com/earendil-works/pi/blob/bd3c9b823762b7383954aa49e13ee350471015fe/packages/coding-agent/src/core/compaction/compaction.ts#L60) | 利用结构化工具信息；Nexus 仅提取原生 apply_patch 回执中的路径/状态/hash，不从任意 shell 命令猜文件最终状态 |
| [compaction.ts:696 generateSummaryWithUsage / 859 prepareCompaction](https://github.com/earendil-works/pi/blob/bd3c9b823762b7383954aa49e13ee350471015fe/packages/coding-agent/src/core/compaction/compaction.ts#L696) | previous summary + 新历史，独立 Usage；只处理被选中的旧前缀 |
| [session-manager.ts:476 buildContextEntries](https://github.com/earendil-works/pi/blob/bd3c9b823762b7383954aa49e13ee350471015fe/packages/coding-agent/src/core/session-manager.ts#L476) | 最新 checkpoint + 保留尾部 + 后续消息；Nexus 继续复用现有唯一 Session 投影 |

没有复制 pi 的固定 2000 字符裁剪，也没有移植其 session tree、分支摘要或第二摘要器。
工具正文保留执行层保存的全部内容，32 KiB 上限不变；不引入 HOT/COLD。

## 2. 输入与事实分层

新增 `core/compaction_input.py`，替代直接序列化 `Message.public()`：

- `USER_TASK_INFORMATION`：文本、seq、run_id，作为历史需求信息。
- `ASSISTANT_CLAIM`：可见文本与 tool_calls，陈述不等于独立验证。
- `TOOL_EXECUTION_EVIDENCE`：seq、run_id、call_seq、call_id、tool_name，和结构化
  `ok/error_code/truncated/exit_code`。完整 stdout/stderr/diff 等放在 `untrusted_payload`。
- `reported_file_operations`：仅提取原生 patch 的已报告路径、操作类型、前后 hash；
  不等于正确代码，不推断最终文件状态。partial/omitted_files/错误详情仍保留在完整 payload。
- `RUNTIME_LIFECYCLE_ONLY`：按出现顺序给出 Run outcome/reason 与关联 source seqs。
  说明 outcome 可能涵盖摘要区间以外的消息，completed 不升级为任务成功。

保留消息顺序、Run 边界和调用/结果对应关系。无 reasoning_content、Tool Schema、主 System
或请求级 Plan/Skills/handoff 重复注入。不识别“命令含 pytest 就算验证成功”，而是保留命令
和实际退出码/输出供核对。有限前缀没有测试命令，也不证明整个项目从未运行测试。

## 3. First / Update 与最小来源追溯

`core/compaction.py` 保留原有 Goal、Constraints、Progress、Decisions、Files、Validation、
Unresolved、Next Steps 字段。两个 Prompt 共用字段操作合同，区别是首次建立状态或基于
previous_summary 更新状态。语义规则明确区分状态/约束、模型声称/执行回执、操作成功/验证。

最终 `fidelity-v2.2` 让模型输出扁平证据记录，例如：

```json
{"field":"validation_evidence","index":0,"source_seqs":[205]}
```

每条摘要字符串对应一条记录，index 明确指向字段内条目；不靠并行数组猜归属。
Runtime 确定性转换成 V2 checkpoint 的 `field -> 每条摘要的 sources` 字典格式。
既有 V2 字典格式仍接受；不通过自动补齐缺失引用或忽略悬空记录来制造成功。

`validate_summary / validate_references` 检查：

1. 字段、类型、条目索引、来源整数合法且无重复，所有条目都有对应来源。
2. seq 必须来自当前被摘要前缀或上一个 checkpoint，不能引用未提供的近期尾部/其他记录。
3. 目标/约束须引用 user，或原样保留的上一摘要条目；工具/assistant 不能单独创建用户约束。
4. 旧目标、约束、决策若被改写或移除，必须提供更新凭据：field、旧 index、新 source_seqs、
   reason。目标/约束变更须有新 user 来源；不能仅拿旧 checkpoint 自证撤销。
5. 没变化的条目无需凭据；状态、验证、Next Steps 可以依据证据更新，不强迫永久保留旧状态。

这里废弃的是“无论新证据如何都强制逐字永久继承”。尚无新证据时，仍不允许无依据删除
关键用户信息；合法引用也可能与陈述不相干，**程序没有证明自然语言蕴含关系**。
旧摘要中的错误如没有新信息澄清，仍可能继承。没有关键词扫描器、LLM Judge 或事实数据库。

## 4. 持久化与兼容

`core/context.py` 写 `scope=session, version=2, prompt_version`。`core/history.py`
恢复 V1 时使用旧 Schema；恢复 V2 时重新检查来源、更新凭据和原有消息配对/替换引用。
V1 不伪造来源，下一次摘要可引用其 checkpoint seq；旧 Run checkpoint 折入策略不变。
checkpoint 的先追加/flush、后更新内存顺序不变，拒绝响应不更改旧投影或原始 JSONL。

没有改变连续 Session、Run 执行隔离、90% 阈值、20K 完整近期消息、模型组合、独立密钥、
Step Budget、32 KiB 工具上限或单 Agent Loop。
新增复杂度是一个纯输入序列化模块、引用/更新凭据验证和 V1/V2 兼容分支。
真实实验表明引用契约会增加当前模型输出负担；该兼容/可用性风险不能隐藏。

## 5. 冻结输入、实验方法与全部结果

[冻结用例](../../tests/fixtures/compaction-fidelity.json) 包含合成消息、旧摘要、Run outcomes；
[旧 Prompt](../../tests/fixtures/compaction-baseline-prompt.txt) 提取自 `0880514`。
ABF 是上一轮实际摘要的 28 条消息，seq 3–42，未把保留的近期尾部混进实验。
另外两例在调用前冻结，分别覆盖声称完成/测试失败，以及限制撤销/修复/测试通过。
DE 的旧摘要是固定合成前提，不取上一案例的随机输出，确保 old/new 输入一致。

| Input ID | SHA-256 |
| --- | --- |
| ABF-original | `4492b53ade3095a2682ad3a1402072df38a9c31a6194d6670db2a1026a5ffb20` |
| CDE-initial | `fe14d394e351f4c9c7dda7c4f4116b6ce8cffc049b3fcf0f300785c37c9cf8a4` |
| DE-update | `c6e3fc636c8e975408ebcdd9a601637be63cb637d8aece7e78ea37f80965ff37` |

固定 Qwen/Qwen3.5-35B-A3B、硅基流动、enable_thinking=false、4096 输出上限、120s 超时，
temperature 始终省略（provider 默认值），不修改主模型。只比较 Prompt/Input/引用合同这个
整体改动，**没有分别隔离 Prompt 与 Serializer 的因果贡献**，没有主模型 Coding Benchmark。

执行 [复现脚本](../../scripts/compaction_fidelity.py) 默认只构造请求；只有 `--run` 发送真实请求。
必须给新输出目录，不覆盖旧尝试。每次保存输入/请求 SHA、Prompt 版本、Git SHA/diff hash、
参数、原始响应、reported Usage、尝试事件与耗时。手工评审写在单独发布的结果副本中，
原运行工件不改写；没有让模型给自身打分。

| 尝试 / Prompt | 结构与引用 | 事实审阅 | input/output | 耗时 |
| --- | --- | --- | ---: | ---: |
| ABF 原失败记录 / 0880514（上轮） | PASS | NOT_RUN 被当约束；捏造禁止测试的决策，次数错误 | 7103/642 | 17.5s |
| ABF current-1 / v2 | FAIL | 状态仍是约束；遗漏生命周期 | 7662/393 | 130.8s，含一次 120s 超时 |
| CDE baseline / 0880514 | PASS | 正确区分模型声称，但实际失败后仍标 NOT RUN | 825/404 | 16.9s |
| CDE current / v2 | FAIL | 从声称完成推断文件操作，来源数组错误 | 1022/345 | 9.4s |
| DE baseline / 0880514 | FAIL：旧逐字继承 | 保留撤销约束；无证据声称配置未变；过早宣称完成 | 1080/307 | 8.1s |
| ABF current-2 / v2.1 | FAIL | 未再把 NOT_RUN 放约束，但遗漏验证和生命周期、进度错分 | 7738/344 | 14.9s |
| DE current / v2.1 | FAIL | 撤销和 PASS 有体现；遗漏失败/修复历史、过度完成推断 | 1555/676 | 40.9s |
| ABF final / v2.2 | PASS | 严重遗漏：整句用户输入塞进 goal，其余接续状态几乎全空 | 7710/156 | 6.6s |
| CDE final / v2.2 | FAIL | NOT_RUN 再次进约束；FAILED 配了早期未运行陈述；类型/覆盖错误 | 1070/427 | 11.6s |
| DE final / v2.2 | FAIL | 撤销和 focused PASS 表达正确；遗漏早期失败；引用指向空字段条目 | 1568/502 | 16.8s |

本轮九次逻辑调用、十次物理尝试：ABF v2 一次 transport timeout 后适配器重试。
已报告用量合计 33784 tokens；超时尝试无 Usage，所以是已知下界，未核算金额。
后两次 Prompt 修订只针对通用引用格式：v2.1 加形状示例，v2.2 改显式索引记录。
没有增加样本语义关键词规则，没有改 Thinking/Temperature，没有丢弃失败继续试到 PASS。

最终三例没有一例完整事实保真 PASS；**不能声称本轮已经改善总体摘要质量**。
原“禁止事项意味着已检查未发生”的具体错误在这几份新输出中未再观察到，但样本少且有
严重遗漏，不能据此宣称该错误根治。约束撤销与失败→通过的表达有局部改善，但未形成
可提交的完整合格摘要。新增严格合同导致的格式拒绝是本轮明确的可用性退化风险。

公开证据仅为合成数据：[逐次 ledger](evidence/compaction-fidelity/ledger.json)。同目录保留每次
原始 `.response.txt` 和参数/Usage/错误/人工评审 `.json`，包括失败与通过结构但失败语义的样本。
未发布用户私人轨迹或密钥。固定输出不是随机模型未来行为的保证。

## 6. 质量门禁、交付与待审问题

本地最终：517 passed / 10 skipped（Docker 按需验收未运行）；Ruff、Mypy（71 个文件）、
`uv lock --check`、sdist/wheel build 全部通过。CI 状态与最终 SHA 见交付消息。
[代码提交 3781b5d 的 Windows/Linux CI](https://github.com/K529529/Nexus/actions/runs/39642329281)
已通过；最终证据文档提交的 CI 会另行确认。
离线测试涵盖来源不存在/类型/角色、撤销凭据、不得静默遗失、V1→V2、重复/缺失/越界来源、
原有 checkpoint 原子性、取消、恢复、工具配对，以及冻结输入 SHA/消息顺序。
专门保留“合法引用但错误自然语言仍可通过”的反例测试，明确检查器能力边界。

分模块提交：`f00bfca` 输入分层，`beee5ec` 更新与来源合同，`978b99f` 通用形状澄清，
`3781b5d` 显式条目引用。文档和原始结果另作收尾提交；不合并、不替换全局安装。

待独立审查的具体问题：是否保留逐项来源要求、怎样在固定 Qwen 上降低合同负担，以及是否
应在下一轮单独评估模型参数。本轮不继续付费调参，不把采样失败包装成稳定能力。
此前发现的 **context_window 默认值、真实容量边界与 Overflow 识别** 保留为明确后续问题，
本轮没有混改，也没有宣称已修复。
