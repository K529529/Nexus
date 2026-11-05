# Nexus 0.3.0 Release Notes

已于 2026-11-05 发布：[GitHub Release](https://github.com/K529529/Nexus/releases/tag/v0.3.0) ·
[PyPI 0.3.0](https://pypi.org/project/nexus-coding-agent/0.3.0/)。
发布标签固定于 `ddf06420eed65b0f5986faeae50aa2243694aab2`；
[正式发布流水线](https://github.com/K529529/Nexus/actions/runs/41984455825)已通过。
[验证、CI 与合入记录](release-0.3.0-validation.md) ·
[中文使用手册](usage-guide.zh-CN.md) · [English guide](usage-guide.md)

## 变更

- 连续 Session 上下文：独立压缩模型、V4 语义摘要与确定性 Runtime Facts。
  原始消息/工具回执仍保存于 JSONL，工具调用组不拆开；不再使用旧 HOT/COLD 投影。
- 长期记忆：Markdown authority、QMD 检索、稳定会话快照、恢复删除及 Daily。
  默认开启，首次 Local 使用可能下载用户级依赖/模型；可明确关闭。
- 终端交互：持久输入框、运行中 Enter 指导/Tab 排队、多行粘贴与审批草稿保留；
  多 Agent 独立会话、任务名/短 ID 选择器、阅读历史时不抢回底部。
- 执行权限：Local、Windows Native 部分强制隔离、Linux/WSL Docker；
  精确 Patch 一次性审批、进程取消与清理，不自动回退 Local。
- Durable Execution：工具 PREPARED/DISPATCHING/COMPLETED checkpoint、
  Patch manifest/ACK、损坏尾部保留恢复与未知 Shell 副作用防重放。
- 工具与模型：stdio MCP 工具/嵌入文本资源、可选 DeepSeek Web、LiteLLM 接入、
  模型别名菜单和仅元数据的 OTLP tracing。
- 可选多 Agent：scout/worker/reviewer 复用同一 Loop，Fresh 上下文、共享工作区、
  全树额度、父子通信、取消、恢复、结果交付去重与实际请求用量统计。
  **默认关闭**；嵌套默认关闭，活跃后代默认最多 3 个。
- 发布收尾：修复 Linux mypy 对 Windows 专属进程标志的类型判断；
  CI 覆盖 V0.3/Stage8/main，缩放回归用真实渲染屏障，增加包资源/独立安装检查。

## 升级与配置

在独立 Python 3.12+ 环境运行 `python -m pip install --upgrade nexus-coding-agent==0.3.0`，
或使用 `uv tool install nexus-coding-agent==0.3.0`。
备份用户 config、sessions、memory，核对实际 CLI 路径和 `python -m nexus --version`。
独立压缩模型必须显式配置；其 key、超时和输出预算不从主模型偷偷继承。

默认值与边界：Local 使用用户权限；Memory on；Multi-Agent/Web/Tracing off。
模型参数由用户所选服务决定，不因版本升级更换模型/推理强度或读取项目 `.env`。
详细迁移步骤在 [使用手册](usage-guide.zh-CN.md#安装与升级)。

## 已知限制与尚未证明的结论

- Windows Native 是 partial enforcement，网络不受限，现有读权限不隔离敏感文件；
  目录 ACL 授权可持久保留。Python/0700 等平台限制见 [平台说明](stage4-platform-backends.md)。
- macOS 受限执行、Linux/macOS/ARM 上完整真实 QMD bootstrap 不是本轮离线 CI 的证明范围。
- PyCharm PowerShell 的中文 IME、粘贴、缩放与长期使用手感仍需人工检查；
  真输入管道测试和离线帧不能替代宿主体验。
- 多 Agent 共享工作区，没有文件锁或冲突合并服务；角色“只读”不构成 Shell 安全边界。
  全树取消/恢复保守处理未知副作用，不承诺强杀后自动消除所有外部进程影响。
- 摘要 Schema 有效不等于保真；历史实验未证明整体语义保真或接续质量明显改善。
  [保真失败](compaction-fidelity-hardening.md)与[简化实验](compaction-simplification.md)保持原始结果。
- 工具退出码、Agent completed、官方 verifier PASS 是不同层次，不能互相替代。
- 缺失用量保持 unknown；缓存输入不重复累加；并行子代理时长不相加为整题耗时。
- 外部 tracing 仅元数据，但本地 JSONL 仍含对话、代码和工具内容；共享前需审查。
  远端 trace 补发不保证 exactly-once，未完成 span 不补造结束时间。
- 本轮不运行新的付费模型、QMD 下载或 opt-in Docker/真实沙箱专项，也不重跑本地八项评测。
  已有验收按原提交/环境保留，具体 PASS/FAIL/NOT RUN 见验证记录。

## 评测版本与发布版本

ABK 单/多 Agent 对照固定于 **`0fd7fa4ba1f0bc8e313fca04cf1771c2a33720dd`**。
它的包版本字符串仍可能为 0.2.0，以 commit、源码包 SHA256、实际镜像 ID 为身份依据。
本轮发布不改变其镜像、配置、数据或运行状态；0.3.0 包含后续类型修复、CI/测试改进、
0.3.0 元数据和文档/包检查，不重新标记或覆盖旧评测证据。

| 实验 | 条件与状态（2026-11-05 发布准备时记录） |
| --- | --- |
| 已结束 FeatureBench 固定 16 题 | `0fd7fa4`；Local/单 Agent；qwen3.8-flash/low；1/16 官方 PASS；主 Agent 阶段上限 900s、50 steps；不是 DeepSeek Pro/high 结果 |
| 用户启动的新固定 16 题 | 同提交/题集；deepseek-v4-pro/high；压缩 Qwen/Qwen3.5-35B-A3B、关闭思考；Local/单 Agent；50 steps、900s；**进行中，尚无本页最终成绩** |
| 配套 Multi-Agent 对照 | 同冻结提交与题集，仅启用 multi_agent；本轮未启动，不预填分数 |

新批次 Run ID `20261104T231415Z-9831f7bd`。本页的进行中状态是发布准备时快照，
不是后台实时查询；结束后应由 ABK 自身不可变结果报告给出结论。
Local 八项开发评测、历史污染数据和 ABK FeatureBench 分开，不合并成一个成绩。
不能从功能实现或一次样本宣称更快、更省或更准确。

## 证据与检查入口

- [本轮验证和分阶段合入](release-0.3.0-validation.md)
- [Stage8 输入交接与收尾修复](stage8-review-fixes.md)
- [Stage8 完整交互改版与初始失败](stage8-interaction.md)
- [Stage8 实施进度与离线边界](stage8-progress.md)
- [Stage6 Durable Execution 验收](../reports/stage6-durable-execution-acceptance.md)
- [Stage7 模型与元数据遥测](stage7-observability-models.md)
- [开发/构建/正式发布步骤](development-and-release.md)

使用反馈重点：Windows 实际终端体验；默认能力/权限；历史会话升级；多 Agent 任务选择、
指导/排队、取消和已完成只读。摘要限制和评测条件仍按上文与原始证据记录。
