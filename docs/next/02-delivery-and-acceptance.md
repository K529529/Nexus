# Nexus Next V0：实施与验收清单

状态：**APPROVED v0.2 / Phase 2 Implementation** · 2026-10-01

依据：[冻结架构契约](architecture-contract-v0.md)、[开发设计](01-development-design.md)。用户已在本次实施任务明确授权连续推进 S1 → S5。以下表格定义验收标准，实际执行结果另记 acceptance-evidence.md；历史 S0 说明不再表示本次实施未授权。

2026-10-02 Context Runtime V0.1 增量按用户本轮要求替换 S3/T12 中的运行时压缩行为：增加 run 投影与隔离，保留完整 JSONL 和中断恢复，超预算明确停止，暂不压缩。验收覆盖新 run 隔离、未完成 run 恢复、持久化不变，以及两任务模型请求捕获；见 [V0.1 实测记录](context-runtime-v0.1-evidence.md)。其余阶段和工具输出预算不变。

## 1. 实施顺序

采用六个可验证增量，不再套用旧 Day 1–10，不承诺未验证的工期。设计获批后可以连续推进；每步只报告实际证据，出现设计停止线才暂停裁决。

| 阶段 | 交付范围 | 进入下一步的证据 |
| --- | --- | --- |
| S0：开发入口准备（completed / ready） | 用户已准备分支并清理旧 docs；助手已核对 workspace/branch/base、重写根 AGENTS 并修正文档入口 | `Nexus-next` / `refactor/lean-agent-core` / `1a1732e`；docs 仅四份 Next 文档；未提交工作受保护，main 未修改。ready 不代替设计批准或后续功能实现授权 |
| S1：Provider 最小闭环 | `app/config.py`、`core/types.py`、`core/model.py`、两个工具 schema 的测试替身；首个 live 契约探针 | 指定模型可 stream/function call/回传结果/提取 usage，high 确实发出；必要协议续接由 adapter 完成，不进入循环决策 |
| S2：真实编码闭环 | `core/agent.py`、`tools/registry.py`、`tools/execution.py`、`tools/patch.py`；exec 最小入口；删除已替代旧职责 | 临时仓库真实 read/search → patch → test → final；失败可修复；取消/路径边界通过；共享预算经读源码/测试输出检验 |
| S3：会话与窗口 | `app/session.py`、`app/events.py`、`core/context.py`；JSONL 与单一 compaction | 正常/崩溃恢复不重放命令；必要协议字段可恢复但不泄露；写错可见、未知副作用如实记录；无耐断电事务承诺 |
| S4：交互与 MCP | `app/tui.py`/config、resume selector、首次配置引导、`tools/mcp.py` | Windows/Linux 交互可用；新用户能配置；坏 MCP server 不阻断 native/健康 server，同 loop 的真实 stdio smoke 通过 |
| S5：交付与外部评分 | 薄 SWE 脚本、官方 grader、新 CI、wheel/sdist、双语 README、淘汰源码/旧专用测试/配置物理删除 | 必需行为验证与代码/架构检查通过、证据完整，形成交由用户审核和合入的候选；无知识问答门禁 |

每个阶段在同一开发分支、同一 `src/nexus` 中进行。短暂的未完成构建状态可存在于开发过程；不设计双 runtime 入口、迁移路由器或永久新旧目录。新职责替代后直接删除旧实现和对应旧专用测试；最终只能有一个入口链。

**S0 开发入口准备已 completed / ready。** 用户已完成分支准备和旧 docs 清理；助手已核对 `main → next → refactor/lean-agent-core` 均基于 `1a1732e`，并按本轮授权重写根 AGENTS。实际 workspace 和清理记录见审核入口 §4；没有自动创建/切换、stash 或重置分支。

当前有效设计依据仅为冻结 Next 架构契约、开发设计与本验收清单；根 AGENTS 是简短入口，00 只记录审核、迁移与状态。旧产品规格、Day/Addendum 和 ADR 文档已清理；未发现根 AGENTS 以外的 AGENTS.override.md、嵌套 AGENTS.md、AGENT.md、agent.md 或 CLAUDE.md。README 的旧说明已标为历史背景，旧文档链接已替换。旧 CI/脚本、源码与示例仍待后续迁移，不作为 Next 规则；CI 替换属于 S5，不计入本次入口 ready。新版文档、LICENSE、无关文件/未提交工作、用户数据库/缓存/配置和 main 必须保留。

后续开始实现前复核工作区状态，不从旧规则继续找限制；文件清理不等于旧聊天上下文消失。旧代码删除顺序由开发者决定，但候选中淘汰源码、旧专用测试与配置必须物理删除，不能只停用。**不自行 commit、push、merge、改默认分支或发布**；合入 next、再合入 main 均由用户操作或另行授权。

**S1 的意义：** 先验证真实 Provider 契约，防止先做完 TUI/持久化才发现模型不能完成工具回环。探针以临时目录和测试替身控制副作用，不让模型操作生产数据。若无凭据或 endpoint，S1 live 保持 NOT RUN；可完成不依赖它的确定性工作，但不能通过 S1 gate 或宣称该模型已兼容。

## 2. 最小但有效的测试集

T01–T17 是行为清单，不要求建立 17 套测试基础设施，也不按用例数量验收；同一组精简 fixture/测试可覆盖多项。离线回归必须不用数据库、Docker、npm、API key 或真实云请求即可运行。

| 编号 | 行为验证 | 关键断言 |
| --- | --- | --- |
| T01 | 原生 model-tool-model 循环 | 工具调用 assistant 与对应 tool result 完整进入下一请求；正文 + calls 不提前结束 |
| T02 | 同批多工具与失败恢复 | 顺序、每 call 一 result；第一工具失败不会丢第二个；后续模型能修复 |
| T03 | 终止与计数 | 正常 final、max steps、空输出、length、坏/重复 call id、模型故障分别对应正确 outcome；usage unknown 不变成 0 |
| T04 | Provider streaming/续接 | 参数跨 chunk、多个 call index、usage-only/reasoning-only delta、流中断；半截工具不执行；必要协议字段正确回传/恢复，不需要时不保存，不进公开输出 |
| T05 | Provider 配置与重试 | name/base_url/key env/high 注入；SDK 无隐式额外重试；无 delta 的临时错误只重试一次；工具不重试 |
| T06 | Shell 实际行为 | 临时目录、显式 workdir、stdout/stderr、非零退出、Unicode/空格路径、stdin 关闭；Windows 与 Linux 各测 |
| T07 | 共享输出预算与回收 | 单路可用总额度，两路大量输出仍有界；head/tail、截断标记、持续 drain 正确；真实读源码/测试输出检验默认值；timeout/Ctrl+C 回收普通子进程，cleanup 失败可见 |
| T08 | Patch 正常行为 | 新增/修改/删除、多文件、CRLF、无末尾换行、权限保留；实际 diff/hash 与磁盘一致 |
| T09 | Patch 错误行为 | 越界、绝对路径、symlink/junction、歧义/零匹配、并发变动、unsupported；预检失败不改文件，I/O partial 如实报告 |
| T10 | JSONL 与隔离 | workspace 分隔、seq/version、单 writer 追加；正常 resume 恢复有效消息及最低必要协议数据；不依赖数据库或逐边界 fsync |
| T11 | 崩溃与取消 | 在 side effect 前/后、结果写入前注入进程故障；日志缺项按 unknown，已知未派发的取消项为 not_executed；不重放；损坏尾行/中间行按设计处理 |
| T12 | Context | AGENTS 唯一自动 repo 文件；无需扫描；schema 计入预算；完整组压缩、不孤立 tool；摘要重放；超限明确停止 |
| T13 | 事件与保存边界 | 追加顺序正确、写入/flush 错误可见且停止新副作用；TUI 故障降级；公开消费者/JSON/导出无 protocol_data、密钥或私有 reasoning；必要本地保存与公开日志分开验证 |
| T14 | MCP 降级 | 用户配置、分页/命名、text/JSON/is_error/timeout；一服务器连接/发现失败只停用它，native/健康 server 仍可用，用户与模型都收到不可用事实；依赖任务不伪报，副作用不重试 |
| T15 | CLI/TUI/引导 | help/version 离线可用；首次交互收集并保存非敏感设置，取消不写、不保存 key；one-shot/无 TTY 缺配置立即报错；exec JSON/退出码、selector、正文不重复 |
| T16 | Eval artifact | 原始 base_commit 与结束文件内容比较，覆盖模型 commit 后仍收集修复、shell 修改/新增文件；官方 prediction/report 关联；缺报告为 unknown；记录外部环境/网络与污染标识 |
| T17 | 安装隔离 | 非源码 cwd 的干净 venv 安装 wheel，无旧数据库变量；首次启动可引导配置，非交互不等待；入口/import/resume 可用；无淘汰依赖 |

多数循环测试使用 scripted fake model + 临时文件系统；Provider 使用原始流 fixture 验证协议；executor/patch/session 使用真实 OS 操作。不能仅 mock subprocess 再声称取消已验证。小型本地 MCP server 使用已安装 Python SDK 启动，避免为 native 测试引入 npx 下载。

## 3. 指定模型与双平台 live 验收

Windows 是主要真实产品验收平台；Linux 必须完成真实跨平台 smoke。两者使用以下指定模型，不能以自动化替身代替 live。

首测固定 **阿里云百炼直供 `deepseek-v4.1-flash` / Chat Completions / `reasoning_effort=high`**。API key 保持环境变量；只记录变量名，不记录值。Base URL 取用户账户所在 region/workspace 的真实配置，不使用 DeepSeek 自营地址或其他供应商版本。

| 首测检查 | PASS 证据 |
| --- | --- |
| Chat Completions + streaming | 同一请求收到可展示增量与完整终止信号；不是先等待完整答复再模拟逐字输出 |
| `exec_command` Function Calling | 模型返回真实 call id/name/JSON，执行实际只读命令并回传正确 tool_call_id |
| `apply_patch` Function Calling | 模型返回实际 patch，临时项目的文件确实按 patch 改变 |
| 连续回环 | 模型读取上述工具结果后继续测试/修复或最终答复，至少两次带工具的 model round |
| usage | provider 返回 input/output/total，保留 reported 来源；缺失时此指定模型项目不能靠 estimate 判 PASS |
| high 与 reasoning | 请求包含 high；reasoning-only chunk 不误判；S1 查明必要续接字段并由 adapter 正确往返，S3 验证必要本地恢复及公开输出隔离；不得悄悄关闭思考 |
| 平台 | Windows PowerShell 与 Linux 均完成真实调用和本地工具操作；记录 shell/Python/包版本 |

文档核查不能代替 live 证据。百炼要求的必要续接字段属于已约定的 adapter 工作：按开发设计 §4 实现最小收集/回传，确需 resume 时才作本地保存，并验证不进入 TUI、公开 JSON 或遥测；不因发现字段而暂停整个项目重审架构。保持先通过真实工具回环再推进依赖工作的顺序；真正需要新增运行时能力时才提范围变更。

### Primary product live acceptance — Windows

Windows PowerShell 上使用三个固定小型临时 Python 仓库用例，初始快照与最终断言在 live 开始前确定，A/B/C 都必须完成真实 end-to-end 运行：

| 用例 | 用户任务与预期结果 | 外部验收 |
| --- | --- | --- |
| A：修复小缺陷 | 找出边界输入导致的失败，修改实现并运行已有测试 | 修复前至少一个失败；修复后原测试全通过；不得删除/跳过断言 |
| B：添加小功能 | 在已有模块增加一个有明确输入/输出的功能，新增测试及简短说明 | 至少修改实现并新增测试文件；新旧测试都通过；diff 无无关改动 |
| C：连续会话 | 完成第一轮修改，退出，再从 selector 恢复追加需求 | 可利用前轮上下文继续；新验证通过；旧工具不重复执行，无数据库 |

### Cross-platform live smoke — Linux

Linux 至少完成 1 个真实 end-to-end coding task，保留一条完整成功轨迹，证明安装/启动、model → tool → model 回环、`exec_command`、`apply_patch`、streaming/TUI 基本交互及项目命令/测试执行均可用。可复用 A 或 B 的任务定义，不要求在 Linux 重复完整 A/B/C 产品验收。

最低成功证据为 Windows A/B/C 三条完整轨迹，加 Linux 至少一条完整 coding smoke 轨迹，共至少四条。两个平台的失败尝试也必须保留，分别报告 attempts 与成功数，不能只选成功轨迹隐藏失败。

其余 Windows/Linux 平台行为由确定性自动化测试覆盖：T06–T09 在两平台验证 executor、patch、输出预算、timeout/cancel 与清理；T02/T07/T11/T12 稳定覆盖“失败→修复”、取消、崩溃及压缩路径，避免依赖模型随机出错触发测试。

这验证简单任务可交付，不代表广泛任务成功率。任一必需平台或模型项未测，整体交付状态保留 NOT RUN/未完成，不以另一个平台结果替代。

## 4. 原架构合同十条验收映射

| 原合同 §15 | V0 证据入口 |
| --- | --- |
| 1. 正常 Python 安装启动 | T17；干净 venv 中 pip 安装构建的 wheel，及 uv tool 安装同一 wheel |
| 2. 仓库内运行且无数据库 | T12/T17 + 两平台 live；删除 DB/LangGraph 的依赖与启动组装 |
| 3. 用户可提交简单编码任务 | CLI/TUI T15 + A/B |
| 4. exec 探索仓库 | T01/T06 + A 轨迹 |
| 5. patch 修改代码 | T08/T09 + A/B 实际磁盘 diff |
| 6. exec 执行项目测试 | T06 + A/B/C 的命令、退出码和完整必要输出 |
| 7. 同一循环连续使用工具直到结束 | T01/T02/T03 + 指定模型连续回环 |
| 8. 流式、可读、顺畅 | T04/T07/T15 + 两平台人工终端检查 |
| 9. 本地持久化与友好恢复 | T10/T11/T15 + C |
| 10. 核心快速读懂 | 单一主循环、三个小 package、淘汰源码已删除；规模观察与代码/架构检查，非行数或知识问答门禁 |

合同正文额外必需项也必须验收：MCP=T14 + 同 loop 的真实本地 stdio smoke；Context=T12；Runtime Events=T13；SWE-bench=下节官方对接。不因它们未逐字列入这十条就从 V0 删除。

## 5. SWE-bench 最小验收

只做一次明确选定 instance 的正式对接 smoke，不制定没有依据的 resolved 率指标。建议先用此前项目涉及的 `pvlib__pvlib-python-1707`，但在执行前核对它确实存在于所选官方 dataset split；否则明确记录实际替代样本及原因，不修改题目。

流程：准备隔离 workspace/base commit/dependencies → 脚本 predict → 得到 predictions.jsonl + runs.jsonl + trajectory → 官方 harness → collect 原始官方报告与指标。脚本内只保留 `predict` 和 `collect` 两个开发子命令；harness 由文档里的官方入口运行，不包装成新评测平台。

prediction 始终比较任务原始 base_commit 与结束时真实文件内容，不比较结束时 HEAD；模型已经 commit 的修复也必须保留，T16 覆盖，不增加 Git denylist。外部准备流程除检查初始 HEAD/干净状态，还负责排除可见未来官方修复/答案（含可访问 Git 历史/对象、文件与挂载资料），并记录网络条件。不能满足时只记受污染/非正式实验，不作为干净 benchmark 能力证据；不在 Core 建反作弊平台。

官方调用形式如下，实际执行时记录 dataset revision、harness 版本和 instance_id：

```bash
python -m swebench.harness.run_evaluation \
  --dataset_name SWE-bench/SWE-bench_Lite \
  --predictions_path artifacts/predictions.jsonl \
  --instance_ids <verified-instance-id> \
  --max_workers 1 \
  --run_id <unique-run-id>
```

同一 patch hash/instance/run 关联原始报告，重跑使用新 run_id，避免缓存旧结果。官方 grader 的环境准备和测试执行由其 harness 负责；Nexus inference workspace 的准备记录单独保留。详见 [SWE-bench 官方 Evaluation Guide](https://www.swebench.com/SWE-bench/guides/evaluation/)。

实施时验证的 harness 5.0.2 需要新版官方 dataset 的 `image`/evaluation 字段；旧 `princeton-nlp` 数据副本会报 `KeyError: image`。这是上游调用格式兼容修正，不改变评测职责。固定 revision 的官方行也可原样导出为 JSON 传给该入口，必须核对题目和原始 base_commit 一致，不手工补造 grader 数据。

对接 PASS：有效 prediction 被官方 harness 接收、实际评分执行、报告可回收且与 trajectory 对应。resolved=false 可以是一次真实有效的评分；基础设施错误或只有 fake report 不满足真实对接。接入 smoke、样本 resolved 与环境是否无污染分别记录；即使非正式实验打通接入，也不能声称取得干净 benchmark 能力证据。缺 Docker/镜像/资源就报告 NOT RUN，不用自定义 pytest 冒充官方 verdict。

## 6. 发布候选检查与证据

S5 的 CI 运行 Windows 与 Linux、Python 3.12；不使用 PostgreSQL service，不要求云凭据。真实百炼验收独立保存，不放入每次 PR 的付费 CI。

必需质量检查：`ruff check .`、`mypy src tests`、`pytest`、`uv lock --check`、`uv build`、`git diff --check`。测试执行命令使用锁定依赖；缓存与临时目录写在允许位置。源码路径测试通过后，还必须从源码目录外验证 wheel，避免被本地 import 路径掩盖打包错误。

新基线不沿用已被删除模块的覆盖率分区门槛，也不为 V0 新建复杂覆盖率平台；T01–T17 全部有效执行及失败分支证据是门禁。若报告覆盖率，只作辅助，不能用高百分比替代实际行为。测试、格式、类型任一失败均需解释并修复，不能通过缩小扫描路径隐藏。

最终交付目录只新增一份真实 `acceptance-evidence.md`（执行后再写，当前不伪造报告），包含：

- 设计版本/批准记录、开发分支/commit/未提交 diff 摘要、Python/OS/shell/依赖锁信息。
- T01–T17 与原十条 AC 的 PASS/FAIL/NOT RUN；每项对应实际命令和 artifact 路径。
- 指定百炼模型、region、脱敏 endpoint、reasoning_effort、输出预算、reported usage；Windows A/B/C 三条完整成功轨迹、Linux 至少一条真实 end-to-end coding smoke 成功轨迹（合计至少四条），以及两平台失败尝试与 attempts/成功数。
- TUI 双平台与首次配置观察结果、MCP 正常/部分故障 smoke、官方 SWE prediction/run/report/原始 base_commit/patch hash、外部环境和网络/污染说明。
- 源文件数/非空行数（仅观察）、直接依赖清单、淘汰源码/旧专用测试/配置物理删除的范围，以及有效开发入口核对结果。
- wheel/sdist 文件、干净安装结果、未解决限制；不把 build 成功写成 PyPI 已发布。

## 7. 产品验收与非阻塞学习提示

交付前的代码/架构检查只回答具体问题：是否只有一个循环；是否隐藏 Planner/Validation；是否只有两个 native tools；失败结果能否进入下一轮；是否超出批准范围；淘汰文件是否物理删除；Provider 细节是否隔离在 adapter；恢复是否重放副作用。通过后交由用户审核，不能据此自行合并。

下面五个问题仅作为从 Java 后端转 Agent 开发的学习提示，**不要求答题或“知识审核通过”，不影响开发、产品验收或合入条件**：

1. `messages` 如何驱动控制流，tool_call_id 如何把调用和结果配对？
2. 为什么测试失败属于 observation，而模型流中断属于 runtime failure？
3. 什么证明 patch 真写入、测试真运行，为什么 final 文字不能替代这些事实？
4. 为什么 JSONL 可以恢复对话，却不能保证一次外部命令恰好执行一次？
5. Context 压缩为何必须保留完整工具消息组，为什么不需要预先建立仓库索引？

停止线：设计未获用户批准不实现；S0 ready 不代表本轮授权功能编码，后续工作区与交接不符时先报告；不覆盖用户未提交工作；无真实 Provider 多轮证据不声称兼容；必需产品验收缺失不声称 V0 完成。常量、helper、等价 SDK 适配和必要协议续接无需新增 ADR 或逐项审批；超出冻结范围才提出变更。不自行 commit/push/merge、改默认分支或发布，不靠增加未来架构掩盖当前失败。
