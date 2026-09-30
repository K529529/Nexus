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
