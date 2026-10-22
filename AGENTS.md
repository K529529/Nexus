# Nexus Next V0

本仓库当前开发 Nexus Next V0。本文件只作开发入口，不复制项目契约。

## 有效设计依据

- [冻结架构原文](docs/next/architecture-contract-v0.md)
- [开发设计 v0.2](docs/next/01-development-design.md)
- [实施与验收清单](docs/next/02-delivery-and-acceptance.md)

`docs/next/00-review-and-decisions.md` 只记录审核、迁移和状态，不建立新的长期 Contract / ADR 权威层。

## 旧规则失效

旧 Nexus 的 Day1–Day10、旧 Product Specification、旧 ADR、spec-addenda、旧 AGENTS 指令全部失效，不得从历史文档、代码或 Memory 继续继承为默认规则。这也包括 LangGraph 工作流、Planner / Validator / Validation / Repair / Replan 节点、SAFE / WRITE / DANGEROUS 与 Plan-based authorization，以及旧 PostgreSQL / pgvector / RAG / Skills / LangSmith 架构要求。遗留源码、测试、配置和示例是待迁移材料，不是 Next 的设计依据。

## 开发原则

- Minimal scaffold, evidence-driven complexity。
- 一个 hand-written message-driven Agent Loop；不擅自加入冻结范围外的新架构。
- 实现细节优先采用最简单合理方案；只有真正改变冻结架构边界的问题，才停止并报告证据与选项。
- 按当前任务范围推进并验证；不恢复旧 Product Owner / knowledge review / Day gate 审批体系。
- 不自行 commit / push / merge / publish。

## 测试临时文件

- 普通 pytest 运行不指定 `--basetemp`，使用系统临时目录；禁止在仓库根目录新增 `.pytest-tmp*` / `.pytest-temp*`。项目配置会清理通过用例的 `tmp_path`，仅短期保留失败用例目录。
- 如需隔离并发测试，优先在系统临时目录创建本次运行专用父目录并设置进程级 `PYTEST_DEBUG_TEMPROOT`；不要复用其他用户创建的目录。确需 `--basetemp` 时也只能指向系统临时目录下的专用子目录，结束后另行清理，因为自定义 basetemp 不提供默认的会话保留清理。
- 开发收尾及获准推送前检查本次产生的临时文件；清理已确认无用的文件。需要交付的测试报告、实验原始输出和失败证据先保存到约定的证据目录，不能仅因名称带 tmp 就删除。
- 清理只针对已核实的未跟踪临时路径，不跟随符号链接或 junction；遇到访问拒绝、占用或用途不明时保留并报告，不擅自修改权限。
