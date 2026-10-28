# Stage 4 收官与 V0.3 合入检查

日期：2026-10-28。用户明确授权：先检查，确认无误后将 Stage 4 合入 `codex/nexus-v0.3`。

## 范围与分支

- 源分支：`codex/nexus-v0.3-stage4-sandbox`，已验收实现及证据提交 `6853e70f93bf03df2bf835f32f16ce6a56e1d21a`。
- 目标分支：`codex/nexus-v0.3`，检查时为 `b13ee4e13b32bc60bf56cdad9eaf5461934c437e`。
- 已 fetch；源、目标各自本地/远端均一致，目标为源的祖先，无分叉，可 `merge --ff-only`，不需要解决冲突。
- 收尾改动仅为 4 个测试文件的 5 处缺失类型标注，以及回归报告、验收状态和本次检查证据；不修改生产实现。
- `.idea/`、未跟踪旧 SRT PoC 目录及两个脚本保留原状，不纳入提交，不执行 PoC。Stage 4 分支与已有实验报告保留。
- 不合入 main，不发版，不改变 Windows/WSL 的独立安装环境。

## 验收依据

- 用户已确认 Windows、WSL2 Sandbox 和 HITL 功能通过人工验收；当前剩余 UI 细节不阻塞阶段验收，发版前继续完善。
- 最新代码 Local 8-case **6/8**，与近期两轮完整回归总分一致。两项失败为 Matplotlib 格式语义及 Django 子查询补丁不完整；无明显新增 Local Runtime 阻塞。详见 [详细分析](stage4-local-8case-review-20261028.md)。
- 真实 Windows Native、WSL2 Docker 与终端测试复用已交付报告，不在合入检查时重做 ACL 实验、旧 PoC、模型调用或 Docker 重建。
- 已有 Windows partial enforcement、网络不受限、持久 ACL、Python 0700 兼容限制继续保留；WSL2 实测不能替代所有原生 Linux 环境验收。

## 本轮检查

| 检查 | 结果 |
| --- | --- |
| Ruff `check . --no-cache` | PASS |
| Mypy `src tests` | PASS，121 个文件；补齐测试标注后通过 |
| `uv lock --check --offline` | PASS |
| `uv build --offline` | wheel / sdist 构建通过；仅构建，未发布 |
| wheel 资源 | Pi 两份 LICENSE、NOTICE、Native Bridge/Guard、Linux Backend/Patch Worker 均存在 |
| 自有源码/文档差异空白检查 | PASS；原始 evidence、上游 vendor/fixtures 单独保留原文 |
| 独立 HITL/TUI 套件复核 | 28 passed，未调整测试或生产审批期限 |
| 最终全量测试 | **1019 passed、10 skipped，101.74 秒**；10 项为 opt-in Docker 验收 |

`git diff --check` 对整个阶段历史会报告原始 ANSI 录制/测试日志中的空白及上游文件 EOF 空行；不修改这些原始证据和上游归属文件来制造形式通过。

首次直接使用默认 pytest TEMP：**356 passed、10 skipped、663 errors**，抽查首项为既有 `pytest-of-Archer` 目录访问拒绝。未修改其 ACL，改用系统 TEMP 下本轮独占 `PYTEST_DEBUG_TEMPROOT`。

独立 TEMP 的首次全量为 **1018 passed、10 skipped、1 failed**：HITL `allow` 测试返回 `approval_expired`，该测试设置 3 秒期限。随后的独立 TUI 套件 28 项通过；保留这一时序不稳定证据，不能将重测成功写成从未失败。生产审批期限和测试期限均未放宽。

原始日志：

- [首次独立 TEMP 全量](stage4-merge-pytest-20261028.log)
- [HITL/TUI 复核](stage4-merge-hitl-recheck-20261028.log)
- [最终全量](stage4-merge-pytest-final-20261028.log)

当前 GitHub CI 的 push 分支过滤只包含 main、next、stage1，因此直接推送 V0.3 不会自动触发该工作流；本报告的检查为本地实际执行结果，不冒充远端 CI 通过。

## 收尾方式

合入检查通过：最终全量 1019 项通过，未修改生产代码。以明确文件清单提交本次测试标注和验收文档；保留 Stage 4 分支，快进合入 V0.3，并推送两个命名分支。推送后核对远端 SHA 与本地一致，确认 main 未变。未跟踪的旧实验材料保持原状。
