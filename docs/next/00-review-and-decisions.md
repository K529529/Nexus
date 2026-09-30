# Nexus Next V0：审核入口与迁移说明

状态：**S0 开发入口准备 completed / ready；本轮未授权功能实现**

版本：0.2 · 状态同步：2026-10-01

## 1. 本轮交付与阅读顺序

目标：一个能安装、能连续完成简单仓库任务、代码容易读懂的 Coding Agent。以「极简 + 可交付」约束实现，不以架构能力数量衡量完成度。

| 文档 | 用途 |
| --- | --- |
| 本文 | 审核范围、迁移说明、准备责任及批准记录；不另设契约层 |
| [01-development-design.md](01-development-design.md) | 开发唯一细节基线：接口、执行语义、数据、CLI/TUI |
| [02-delivery-and-acceptance.md](02-delivery-and-acceptance.md) | 实施顺序、必需测试、交付证据与停止线 |
| [architecture-contract-v0.md](architecture-contract-v0.md) | 用户提供的架构说明原文快照，不改写 |

本轮完成 V0 开发基线收尾：重写根 `AGENTS.md`，同步当前工作区和准备状态，将 live 验收调整为 Windows 主要产品验收与 Linux 跨平台 smoke，并修正双语 README 的入口和历史定位。开发设计 v0.2 仍是当前设计基线，架构原文快照保持不变；没有修改源码、测试、依赖或 CI，没有 commit/push/merge/发布，也未开始功能实现。

原文来源：`D:/download/Chrome/Nexus_Next_Architecture_Contract_V0.md`。SHA-256：`67d529fa01f9f3e94ef230c1be0c1b8d9dc4783e6c4a7dbb8b58ccb3f1106b76`。

原文中的「Architecture Frozen for V0 Design」表示设计输入已确定。S0 ready 只表示开发入口已准备就绪，不代签整体设计批准，也不构成 Agent Runtime、Provider、Tool 或 TUI 的实现授权。

## 2. Next 的生效依据与旧新差异

Next 只以 [冻结架构契约](architecture-contract-v0.md) 和用户批准的 [开发设计](01-development-design.md) / [验收清单](02-delivery-and-acceptance.md) 为施工依据；根 `AGENTS.md` 仅作简短入口。本审核说明及本次修订意见不构成额外的长期 ADR/Contract 审批链。

Next 不继承旧规格、旧 ADR、旧 Day/Addendum 或旧 AGENTS 的约束，不需要逐项向旧规则申请豁免。下表仅记录 v0.1 设计调查时的迁移背景；旧文件被清理后，也不能将这些历史引用当成有效规则或待补回文件。

| 主题 | 旧实现的历史背景 | Next V0 设计 |
| --- | --- | --- |
| 执行控制 | 旧规格 §3、§5；`Day4LangGraphRuntime` 的 explore/build_context/plan/approval/validate/repair 节点 | 一个手写消息循环；规划、验证、修复由模型通过工具完成 |
| 持久化 | 旧规格 §6；ADR 003；PostgreSQL + graph checkpoint | 用户目录中的 append-only JSONL；只恢复对话 |
| 命令权限 | 旧规格 §8；SAFE/WRITE/DANGEROUS、精确 Plan scope、Git 操作禁令 | 可信本地 shell；删除上述产品级策略和强制审批，不宣传为沙箱 |
| 上下文 | 旧规格 §7；`context/`、pgvector、启动探索 | 只自动读根 AGENTS；由模型主动探索；仅预算与阈值压缩 |
| 模型协议 | `OpenAICompatibleModelGateway` 经 LangChain 返回阶段性 JSON 决策 | 薄 OpenAI-compatible adapter，使用原生 function tool calls |
| 扩展与可观测性 | `skills/`、LangSmith、复杂 tracing/evaluation | MCP 动态工具、轻量事件、本地轨迹、官方 SWE-bench 对接 |
| 代码组织 | 旧规格 §4.1–4.3；固定 Composition Root、多层 domain/ports | `core/`、`tools/`、`app/` 三个小 package，`app/bootstrap.py` 组装；仅分组，不增加架构层 |
| 发布验收 | 旧 Day 1–10、旧覆盖率区域、LangSmith/数据库验收 | 使用本文档集定义的 V0 行为与交付门禁 |

新基线自身包含：Python async-first、CLI-first、模型协议隔离、工具顺序执行、可观测事实与真实验证证据。必要的 Provider 协议续接数据由 adapter 处理，不展示为推理过程、不导出普通日志或遥测，具体保存边界见开发设计 §4、§7。

**准备状态：** 分支准备和旧 docs 清理由用户完成；助手已核对当前分支、起点和文档目录，并按本轮授权重写根 `AGENTS.md`、清理文档入口。S0 的开发入口准备已 completed / ready；实现仍须依据用户后续授权，本轮停在文档收尾。

## 3. 需要审定的具体选择

用户已明确迁移分支、首测模型/API 与双平台要求，下面 D1–D3 据此更新。其余细节仍为**提案**；本开发设计整体尚未获批，不会据此提前实现。

| ID | 推荐选择 | 取舍与边界 |
| --- | --- | --- |
| D1 | 用户已完成 `main → next → refactor/lean-agent-core` 分支准备，创建时均基于 `1a1732e`；当前在 `Nexus-next` worktree | main 保留旧稳定版，未被此次清理修改；助手不创建/切换分支。开发分支直接替换旧职责，不建独立 src、永久双架构目录或旧版兼容层 |
| D2 | 用户确定：Windows PowerShell 和 Linux 都可运行；建议沿用 Python 3.12+ | macOS 不承诺已验证；普通任务不要求 Git、数据库或 Docker |
| D3 | 用户确定：百炼直供 `deepseek-v4.1-flash`，Chat Completions，streaming/function calling/usage，`reasoning_effort` 可配且首测 high | 不要求 Responses；name/base_url/key env 注入，Core 不含百炼分支。endpoint 地域与实际限额在 live 前确认 |
| D4 | `argparse` + `prompt_toolkit` + `rich`，保留终端滚动记录 | 不是全屏 IDE；支持流式文字、工具输出、diff、会话选择与取消 |
| D5 | `exec_command` 可信本地执行；`apply_patch` 限 workspace | shell/MCP 具有当前用户可用权限，可能访问外部路径/网络；patch 边界不构成整体隔离。无强制审批、命令分类或 Git denylist |
| D6 | MCP V0 只接用户配置的 stdio server，文本/JSON 工具结果 | 不实现 HTTP/OAuth、resources/prompts、sampling/elicitation、热更新与多模态。需要这些能力时修改范围，不暗中补平台 |
| D7 | 延续 distribution `nexus-coding-agent`、命令 `nexus` | 候选版本建议 `0.2.0`；“V0”是产品范围，不回退包版本。包构建/本地安装属于交付，公开发布需另行指示 |
| D8 | 4 个直接运行依赖：`openai`、`mcp`、`prompt-toolkit`、`rich` | 用标准库承担配置、CLI、数据和文件；无 LangChain/LangGraph/数据库/OTel 运行依赖 |

设计整体获批后，常量调整、helper 拆分、等价 SDK 适配、已约定的 Provider 续接字段处理由开发者完成并验证，不另建 ADR 或逐项审批。真正新增运行时能力、改变公开语义或超出冻结范围时，才报告设计变更。

## 4. 当前工作区与后续替换范围

当前状态（2026-10-01 核对）：

```text
workspace: D:\WorkSpace\releasePro\github\Nexus\Nexus-next
branch: refactor/lean-agent-core
HEAD / base commit: 1a1732eb9a4ca5830a3f7ef99bbe74314568ad80

main
  └─ next
      └─ refactor/lean-agent-core
```

上述分支关系由用户准备，三个分支创建时均基于 `1a1732e`，本次核对也均指向该 commit。旧 docs 已从当前开发分支工作树删除，删除与新版文档新增已由用户暂存；main 未被此次清理修改。未操作另一个 worktree `D:\WorkSpace\releasePro\github\Nexus\Nexus`。

当前 `docs/` 仅保留：

```text
docs/next/
  architecture-contract-v0.md
  00-review-and-decisions.md
  01-development-design.md
  02-delivery-and-acceptance.md
```

v0.1 调查快照：分支 `feature/eval-readiness-performance-ux`，HEAD `0872d9f`；`src/nexus` 下 128 个 Python 文件、19,049 非空行（含注释/docstring）。这是历史规模观察，不代表未来开发分支状态，也不是质量评价。

以上 feature 信息仅为历史调查背景，不是当前开发位置。后续继续在已准备的 `refactor/lean-agent-core` 开发，不迁入整条 feature 历史，不自动 stash/reset/clean。若实际状态与交接不符，先报告。

合入计划仍为 V0 验收后 `refactor/lean-agent-core → next`，稳定可用后再 `next → main`；这是计划，不是自动合并授权。**不自行 commit、push、merge、改默认分支或发布**，由用户操作或另行授权。main 保留旧稳定版本。

S0 开发入口准备结果：

- **completed / ready**：用户已完成分支准备及旧产品规格、Day/Addendum、旧 ADR 文档清理；根 `AGENTS.md` 已重写为简短 Next 入口，不复制契约，不保留旧权威链。
- 仓库指令文件核查仅发现根 `AGENTS.md`，未发现其他 `AGENTS.override.md`、嵌套 `AGENTS.md`、`AGENT.md`、`agent.md` 或 `CLAUDE.md`。
- 双语 README 的旧运行说明已明确标为历史背景，文档导航只指向现存 Next 文档；没有新增 ADR、Contract 或旧契约归档。
- 旧源码、内置 Skill、Day 示例及 CI 仍是待迁移材料，不是 Next 开发约束。旧 CI 仍有 PostgreSQL 服务与旧 runtime/security/validation 覆盖率门槛；本轮不改 CI，其替换按 S5 处理，不宣称 Next CI 已就绪。
- 新版文档、LICENSE、未提交工作、用户环境及 main 保持受保护；文件清理不代表旧聊天上下文消失，执行者不得继续继承旧规则。

下表是后续实现迁移范围，不是本轮已完成的修改：

| 处置 | 文件／模块 | 说明 |
| --- | --- | --- |
| 替换并删除旧文件 | `application/`、`domain/`、`infrastructure/graph/`、`infrastructure/bootstrap/` | 直接实现 `core/agent.py`、`app/bootstrap.py` 等；物理删除旧 Planner/Validation/Repair/Replan/LangGraph runtime |
| 替换 | `infrastructure/model_gateway/`、`interfaces/cli/`、`config/` | 保留用户需求，不保留旧 phase/schema/CLI 兼容层 |
| 删除淘汰源码 | `context/`、`security/`、`skills/`、database/persistence/checkpoint/semantic/embedding/observability adapters | 候选版本中不留淘汰文件；必要的最小 context 在 `core/context.py`；不删除用户数据 |
| 替换 | `tools/native.py`、`tools/editing.py`、MCP manager/adapter | 只复核、抽取纯函数；不沿用 PolicyDecision、RiskLevel、Plan 等依赖 |
| 可复用行为 | `tools/editing.py:_apply_unified_patch`、`_atomic_replace`；`test_patch_relocation.py` 中相关用例 | 保留严格匹配、唯一位置修正、换行处理与原子替换思路；新增/删除/多文件语义按新设计补齐 |
| 可复用行为 | `infrastructure/sandbox/local_process.py` 的异步读取经验 | 新 executor 独立实现生命周期，不整体搬入策略/可信 argv 系统 |
| 重写 | `tests/` 中与旧 graph/policy/persistence 绑定的测试、CI、README、安装配置 | 用 V0 测试替换；不能靠大面积 skip 把旧测试伪装成通过 |
| 删除淘汰文件 | 旧 Alembic、Compose、旧 evaluation 框架、旧 Day 专用示例与配置 | 物理删除，不仅停止 import；历史由 main/Git 保留，仓库入口仅指向新指南 |

淘汰代码可先删再写，也可在新职责实现后删除，由开发者决定；V0 候选必须物理删除淘汰源码、旧专用测试和配置，不能只做到不 import。保留的纯函数剥离旧 Plan/policy/graph 依赖；不得建立 `legacy/`、`v1/`、`v2/`、`old_runtime/new_runtime` 等永久双架构目录。代码重构不迁移数据库、不清理用户环境、不升级已发布包。

本轮开始时的未提交内容为旧 docs 的暂存删除、四份 Next 文档的暂存新增，以及未跟踪的 `.idea/`。本轮文档编辑保留为未暂存变更，不改动用户已有暂存区；`.idea/` 不纳入本轮交付范围。旧 worktree 的其他未提交文件不代表当前 `Nexus-next` 状态。后续实现前重新核对，不以重构为由覆盖无关工作。

## 5. “小且高质量”的执行约束

- 首先跑通 `用户 → 模型 → exec → patch → test → 模型结束` 的纵向链路，再加入持久化、TUI、MCP 和 eval 对接。
- 按 `core/`、`tools/`、`app/` 分组，共 14 个功能模块、2 个根入口文件和 3 个 package 初始化文件（初始布局共 19 个 Python 文件）。记录实际文件数与行数作规模观察，不设置配额、编码目标或行数门禁；初始化文件不代表新增能力。
- 一个主循环、一个模型 adapter、一个工具注册表、一个日志格式、一个事件分发函数。没有通用工作流、插件平台、仓储层、DI 容器或兼容性路由器。
- 对非必要抽象优先删减；跨平台、取消、消息协议和 patch 正确性所需代码以行为验证为准，不为追求行数删掉可靠性。
- 模型自主决定何时完成；测试是否通过由实际证据判断；benchmark 是否 resolved 由官方 grader 判断。

## 6. 审核记录

| 项目 | 当前状态 |
| --- | --- |
| 架构原文纳入设计 | 已完成，快照与原文件 SHA-256 已核验一致 |
| 开发设计 v0.2、验收清单 | 当前设计基线；本轮仅按用户要求调整状态与 live 验收范围，不代签整体批准，不新增 ADR 审批层 |
| 分支和旧 docs 清理 | 用户已完成；当前 workspace、branch、HEAD/base 与 docs 目录已核对 |
| S0 开发入口准备 | **completed / ready**；根 AGENTS 已重写，README 入口已修正，无其他旧指令文件 |
| 实现授权 | **未获得，本轮停在文档** |
| 目标服务／模型／平台／分支 | 用户已指定，按 D1–D3 执行；用户专属 Base URL/region/key 在 live 前由本地配置提供 |
| 文档检查 | 本轮核对有效入口、本地链接、live 数量与冻结文件完整性；只改文档，源码/依赖/CI 无本轮变更 |
| 实现测试与 live 验收 | NOT RUN；文档检查不算实现验收 |

S0 开发入口准备已经完成；本轮至此停止。后续功能实现仍按用户明确任务与设计批准推进，不以本次 ready 状态替代授权，不以知识问答通过作为交付条件。

## 7. v0.2 修订摘要

| 审核项 | 已修订位置 |
| --- | --- |
| 旧 docs 清理和分支准备由用户完成，S0 开发入口 ready | 本文 §2、§4、§6；验收清单 S0 |
| Windows A/B/C 主验收 + Linux 至少一次 coding smoke | 验收清单 §3、§6；跨平台自动化和官方 SWE-bench 要求保留 |
| 三个小 package，文件数仅观察 | 本文 §5；开发设计 §1 |
| 去除知识门禁与逐边界 fsync 承诺 | 开发设计 §7；验收清单 §7 |
| Provider 不透明续接与必要本地保存 | 开发设计 §2、§4、§7；S1/T04/T13 |
| MCP 按服务器降级 | 开发设计 §8；T14 |
| 可配置共享输出预算 | 开发设计 §5、§6、§9；T07 |
| 首次交互配置引导 | 开发设计 §9；T15/T17 |
| 原始 base_commit diff 与评测环境责任 | 开发设计 §10；T16、SWE-bench 验收 |
