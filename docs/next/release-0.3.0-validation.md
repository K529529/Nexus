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

版本/文档增量 `d658eff80932afd6fe1b88ff0fd4e095963fce32` 已独立提交并推送。
109 个当前文档本地链接、2 份完整 TOML 示例经真实 Config loader 校验通过，
Ruff、mypy、锁与 diff PASS；新增包脚本已通过 Ruff 格式检查。

## 候选构建、独立安装与后续回归

从 `d658eff` 的 git archive 构建，不包含原工作区未跟踪内容：

- wheel/sdist **PASS**，两者的 100 个包文件逐字节核对通过。
  [构建输出](evidence/release-0.3.0/candidate-build.txt)、
  [SHA256/完整性](evidence/release-0.3.0/candidate-artifacts.json)。
- Windows 源码外新环境实际安装 wheel；`python -I -m nexus --version/--help`、
  runtime/metadata=0.3.0、16 项必要资源、两个 tokenizer/模板实际加载、
  `uv pip check` **PASS**。安装使用 wheel 声明的依赖范围，未改仓库锁；
  [实际依赖版本](evidence/release-0.3.0/candidate-install.txt)、
  [安装检查](evidence/release-0.3.0/candidate-installed-smoke.txt)。
- 工作流静态核验 PASS：CI 两平台/相关分支/PR；Release 仅 published；
  标签须精确 `v<package version>`；PyPI pypi environment/OIDC 入口保留。
  未创建标签、Release 或上传 PyPI；真实发布及公开索引安装 **NOT RUN**。

首轮候选 [CI 41941059482](https://github.com/K529529/Nexus/actions/runs/41941059482)
的 Linux pytest **FAIL：1223 passed / 1 failed / 30 skipped**。
`test_persistent_composer_navigation_and_child_guidance[True]` 观察到回调入口计数即断言
Inbox，而回调还在异步接收。修正测试为等待 `finish_submission()` 后再检查原消息，
保持内容、目标、队列和恰好一次断言；不修改产品代码。
[原始失败](evidence/release-0.3.0/candidate-linux-ci-failure.txt)。
修正后 Windows 界面专项 **6 passed**，Ruff/mypy PASS，见
[复验输出](evidence/release-0.3.0/candidate-screen-recheck.txt)。
该次失败不得作为最终候选 PASS，后续必须完整双平台复验。

## 最终功能候选检查与交付门禁

回归同步修正提交 `9358de1162cf3d5fff8dc614c94c5cc91e65190b` 已推送。
[完整 CI 41941751122](https://github.com/K529529/Nexus/actions/runs/41941751122) **PASS**：

| 项目 | Windows | Linux |
| --- | --- | --- |
| 完整 pytest | 1244 passed / 10 skipped | 1224 passed / 30 skipped |
| Ruff / mypy / 锁 / diff | PASS | PASS |
| wheel + sdist，100 个包文件核对 | PASS | PASS |
| 源码外新环境 wheel 安装、version/help、metadata 一致 | PASS | PASS |
| 16 项必要资源、两个 tokenizer/模板、依赖检查 | PASS | PASS |
| uv tool 独立安装 | PASS | PASS |

[CI 摘要、跳过原因、分发 SHA256](evidence/release-0.3.0/candidate-ci-summary.txt)。
每个平台的精确分发文件保存在该 CI 的 candidate-* artifacts。Windows 的 10 个 skip 是
opt-in Docker；Linux 另外 20 个为 Windows 专用能力。16 个警告来自已锁定 LiteLLM/Pydantic。
本轮不在运行 ABK 的宿主另外开启真实 Docker/ACL 专项、QMD 下载或真实模型评测，
这些记 **NOT RUN**，既有平台人工证据保留原提交条件。

没有剩余已知的自动化发布阻断项。PyCharm/中文 IME 手感、真实多 Agent 体验及尚未完成的
ABK 对照仍需用户检查，不宣称它们已由离线套件证明。
发布工作流实际发布、GitHub Release、PyPI 上传/公开索引安装均 **NOT RUN（等待批准）**。

相对 ABK `0fd7fa4`，产品源码只改变 `src/nexus/__init__.py` 的版本号和
`src/nexus/tools/sandbox.py` 的平台类型分支；其余是测试同步、CI/打包检查和文档。
评测仍用原 commit/archive hash/image ID，不把发布候选伪装成被测镜像。

本次专属独立安装/构建目录已归档证据及分发文件后清理；
[清理记录](evidence/release-0.3.0/temporary-cleanup.txt)。原有 `.idea`、Stage4 PoC 与
系统 pytest 默认保留失败目录未清理；没有改代理配置、电源配置或全局 Nexus 安装。

本验收归档之后只更新提交记录，不再改变功能。合入 main 仍需重新 fetch，确认保留
main 独立变更、采用可快进/保留历史合并，并检查合入后的完整 CI。
最终远端 main SHA 与 CI 作为交付报告中的待发布对象；若之后变化，发布前必须重新确认。
Stage8 与 V0.3 分支保留；不创建 v0.3.0 标签、不发布 Release、不上传 PyPI。
