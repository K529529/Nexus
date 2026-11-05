# 0.3.0 发布准备与验证记录

状态：准备中，**未发布**。本轮允许分阶段提交、推送及 Stage8 → V0.3 → main 合入；
创建标签、发布 GitHub Release（会触发 PyPI）、上传 PyPI 均等待用户检查后另行确认。

## 冻结评测与基线

- ABK 单/多 Agent 对照固定 Nexus `0fd7fa4ba1f0bc8e313fca04cf1771c2a33720dd`。
  本轮不修改、重启或构建 ABK 的代码、配置、镜像、数据、日志。
- 2026-11-05 远端：Stage8 `0fd7fa4`、V0.3 `c6f1b2c`、main `b5b25b5`。
  V0.3 是 Stage8 的祖先（落后 13 提交），main 也是其祖先（落后 87 提交）。
- 原有 `.idea/`、Stage4 SRT PoC 证据及脚本为未跟踪用户内容，保留不动。
- 发布修复使用后续提交，不能把冻结评测结果当作最终候选的直接评测。

## 增量一：平台类型与 CI

`BridgeClient.start` 的条件表达式改为显式 `if sys.platform == "win32"`，
使 mypy 在 Linux 排除 Windows 专属 `subprocess.CREATE_NEW_PROCESS_GROUP`。
Windows 仍建立进程组，POSIX 仍创建新 session；启动屏蔽取消、关闭和清理路径未变。
CI push 覆盖 main、next、V0.3 及其阶段分支，PR 继续全部覆盖。

真实失败记录：

- 修复前 Linux 目标 mypy：1 个 `attr-defined` 错误，见
  [原始输出](evidence/release-0.3.0/stage8-linux-mypy-before.txt)。
- 首次本机并发质量检查：Ruff 内存分配失败，Windows mypy internal error；
  Linux 目标 mypy 随后通过。该次组合不记 PASS。
- 首次 Windows pytest：收集期间 `MemoryError`，没有完成测试，见
  [原始输出](evidence/release-0.3.0/stage8-windows-pytest.txt)。
  改为串行复验，不改变测试断言，不操作正在评测的 ABK。

- 串行 Ruff、Windows mypy、Linux 目标 mypy（各 150 文件）、锁文件与 diff 检查 PASS。
- Windows 全量复验：1243 passed / 1 failed / 10 skipped，见
  [完整输出](evidence/release-0.3.0/stage8-windows-pytest-retry.txt)。
  失败是缩放测试固定等待 100ms 后仍读取前一帧（9 行与新的 8 行限制比较）。
  改为等待 prompt_toolkit `after_render` 屏障，保留高度、光标和草稿全部断言；
  不改产品渲染逻辑，不增加固定 sleep 或削弱断言。
- 屏障修正后界面/Bridge 专项 **20 passed**，见
  [专项输出](evidence/release-0.3.0/stage8-targeted.txt)。Ruff 随后指出循环闭包 B023，
  将当前 Event 显式绑定到回调默认参数，避免下一轮覆盖；再做质量及全量复验。

最终本机 Windows 全量 **1244 passed / 10 skipped / 16 warnings**（170.50s），见
[完整输出](evidence/release-0.3.0/stage8-windows-pytest-final.txt)。警告来自 LiteLLM/Pydantic，
跳过项为 opt-in Docker 验收。

提交 `315832d284c6808909253fb7417caaba6106b000` 已推送，
[双平台 CI 41939344088](https://github.com/K529529/Nexus/actions/runs/41939344088) **PASS**：
Windows 1244 passed / 10 skipped；Linux 1224 passed / 30 skipped。
两边 Ruff、mypy、锁、构建、diff 和源码外 wheel/uv tool 安装均通过。
[CI 摘要](evidence/release-0.3.0/stage8-ci-summary.txt)。

重新 fetch 后 Stage8 → V0.3 于 2026-11-05 **fast-forward**，远端 V0.3 和 Stage8
均核对为 `315832d284c6808909253fb7417caaba6106b000`；Stage8 保留。

## 增量二：0.3.0 版本、文档与包验证

在 V0.3 更新 pyproject、运行时版本及 uv.lock；锁文件只改变本项目版本，依赖不升级。
双语 README/使用手册、代码导读、八项评测入口、Stage8 当前使用、开发/发布步骤、
Release Notes 已对照实际代码更新；历史设计/阶段报告仅增加历史入口，不改写结果。
新增 package_smoke.py 对比 wheel/sdist 全部包文件，源码外实际加载资源、检查版本与依赖；
CI 保留候选分发文件，release workflow 校验精确 v 标签并复用资源检查。

进行中的 ABK 仍固定 `0fd7fa4`。旧 16 题 qwen3.8-flash/low 的 1/16 与新
deepseek-v4-pro/high 批次分开描述；不写新批次分数或多 Agent 性能结论。

最终候选检查与 main 合入结果将在完成后追加。
