# Evaluation V0 实施与验收记录

状态：Stage 1–5 已按批准范围完成；V0.1.1 八项真实基线已完成。V0.2.1 尚未实现，After 对照待其提供。

## Stage 1：八个实际环境与 validator

全部已执行 base 负对照与 reference fix 正对照。原始证据：`artifacts/evaluation-stage1/qualified-44269ae4/`；Rich 的固定 rootdir 修正后证据：`artifacts/evaluation-stage1/qualified-26bd5ad7/`。机器可读摘要保存在 [qualification.json](../../evaluation/next-dev-v0/qualification.json)。

| Case | base | reference fix | 必需 F2P / P2P |
| --- | --- | --- | --- |
| pvlib | 1 failed / 30 passed | 31 passed | 1 / 30 |
| matplotlib | 1 failed / 26 passed / 2 skipped | 27 passed / 2 skipped | 1 / 26 |
| django | 目标缺陷复现，保留检查通过 | queries 370 tests，9 skipped、2 expected failures | 1 / 275 |
| requests | 2 failed / 201 passed / 11 skipped | 203 passed / 11 skipped | 2 / 193 |
| sphinx | 1 failed / 40 passed | 41 passed | 1 / 40 |
| click | runtime 1 failed / 8 passed；typing 失败 | runtime 9 passed；typing 成功 | 1 / 8 + typing |
| pytest | 10 failed / 149 passed / 1 skipped / 1 xfailed | 159 passed / 1 skipped / 1 xfailed | 10 / 149 |
| rich | 13 failed / 113 passed | 126 passed | 13 / 113 |

表中 skip/expected failure 不属于必需目标；所有必需目标均实际执行，正对照全部通过。Requests/Matplotlib 原始参数化标签缩写展开为完整 node ID，映射保存在 targets.json。Django 使用真实 unittest ID 与 shortDescription，避免将缺失目标当作通过。

已逐项检查原始源树文件集合与 Git blob；Matplotlib/pytest 的 `.git_archival.txt` 由 archive export-subst 改写，使用准确 base 的原始 blob 恢复。Git archive 的普通文件权限与 Git tree 有差异，缓存以 Git tree 的 mode 为准；运行时再次检查每个 blob/mode，并要求合成初始 tree 与源 tree 相等。

### 环境修正与失败尝试

- 不可用/停滞的官方 instance 镜像拉取保留日志；只为对应固定 case 构建干净依赖环境，未引入网络网关。
- pvlib 原镜像 NumPy 2 与旧源码 `np.Inf` 不兼容；固定 NumPy 1.26.4、pandas 1.5.3、SciPy 1.13.1。移除原 testbed、setup 脚本和缓存。
- Matplotlib 在准确 base 编译扩展，仅保留必需生成文件；安装 Ghostscript 使原任务必需 PDF 检查可运行。首次并行编译发生资源错误，改用单线程编译后通过。
- Django 使用 Python 3.6.13；其他环境依赖版本由各 Dockerfile 固定。
- Rich 的 validator 使用已有公开 Text.wrap 行为，不依赖参考修复才引入的新 helper；固定 pytest rootdir 消除测试 ID 相对路径差异。
- pytest 的嵌套 pytester 会在终端打印内层测试状态，因此使用外层 pytest hook 结果文件，不通过终端字符串判分。

## Stage 2：真实 Docker 边界

`tests/test_evaluation_docker.py` 实际执行，2 passed：

- 准确 blob、执行位和 Linux symlink；一个合成 commit、无 remote；初始 Git 状态干净。
- 只挂载 Agent workspace，无 Docker socket/validator/控制面；无模型密钥和代理注入；外部 TCP 连接失败。
- 复用 apply_patch 修改普通文件，symlink 修改被拒绝。
- Agent commit 后的修改、未提交新增、删除都进入候选 patch。
- 真正终止 timeout/cancel 对应容器内子进程，后续命令仍可运行。
- 输出超限仍按原字节缓冲截断。
- 全新 Rich 验证环境对空候选补丁重现 13 个缺陷。

初次实测发现 setsid 包装进程可能提前退出 0；改为 `setsid --wait` 后重新通过全部边界检查。失败尝试与修复后的测试输出分别留在本轮记录，未把失败尝试算为通过。

## Stage 3–5

真实单项、八项顺序基线及最终检查均已执行；不以 validator 正对照代替模型基线，不把单项 smoke 与正式 --all 拼成 best-of-N。


### 大仓库准备修复

首次 --all 尝试 `20261002T191817Z-559a1756` 在 Matplotlib 准备阶段超过 120 秒，未调用模型；随后停止整套尝试以修复准备路径。该次保留为基础设施失败，不能作为八项基线，也不用于 best-of-N。此前单项 pvlib smoke `20261002T191253Z-e22df80c` 为 FAIL，原因 `invalid_finish: length`，独立验证未通过。

原因是每个源文件单独启动 `git hash-object`，Matplotlib 4,440、Django 6,135 个文件在 Windows bind mount 上耗时过长。改为一次流式 `git fast-import`，仍保留同一准确源 tree、文件 mode、无上游历史及单一合成 commit。新 Docker 边界验收 2 passed / 19.03 秒；生产 validator 资格复查另存 `artifacts/evaluation-runtime-qualification/`。

Rich 追加了宽度 1 的保留检查：`中A文` 的预期为 `[" ", "A", " "]`；宽度小于双宽字形时保持原来的空格占位策略，不错误要求字符完整性。新负对照 13 failed / 113 passed，正对照 126 passed。


## 最终确定性检查

- pytest：194 passed，2 个 opt-in Docker 测试在普通测试中跳过；这 2 项已另行实际执行并通过。
- Ruff：PASS；mypy：42 source files PASS。
- `uv lock --check --offline`：PASS；`uv build --offline`：wheel + sdist 构建 PASS。
- 暂存区 `git diff --cached --check`：PASS。
- 54 个冻结评测资产的工作区字节与暂存 Git blob 全部一致。`.gitattributes` 保留评测数据的精确字节及合法补丁上下文，避免跨平台 checkout 改变实验身份。
- 生产 prepare + fresh validator 路径的八项参考修复复查：全部 passed，见 [runtime-qualification.json](../../evaluation/next-dev-v0/runtime-qualification.json)。

正式 V0.1.1 基线运行 ID：`20261002T193618Z-96e0877c`。实际结果见下表。

## 正式 V0.1.1 八项基线

运行：`20261002T193618Z-96e0877c`（UTC run ID）。模型 `deepseek-v4.1-flash`，reasoning `high`；context window 1,000,000、max output 8,192、max steps 40。

结果：PASS 1、FAIL 7、ERROR 0、ABORTED 0、NOT_RUN 0。没有与之前 smoke/失败尝试拼接结果。

| Case | Result | Agent | Input | PeakCtx | FinalCtx | ToolResultBytes | Tools | Agent time |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| pvlib__pvlib-python-1707 | FAIL | failed | 84,281 | 13,470 | 13,470 | 33,334 | 16 | 196.9s |
| matplotlib__matplotlib-22835 | PASS | limited | 1,211,993 | 46,728 | 46,728 | 120,204 | 55 | 234.0s |
| django__django-11734 | FAIL | failed | 534,307 | 41,940 | 41,940 | 132,294 | 41 | 334.4s |
| psf__requests-6028 | FAIL | limited | 1,205,151 | 59,030 | 59,030 | 207,162 | 40 | 432.5s |
| sphinx-doc__sphinx-10323 | FAIL | limited | >=1,177,737 | 53,117 | 53,117 | 151,621 | 60 | 699.2s |
| click__path-generic-type | FAIL | failed | >=416,453 | 37,908 | 37,908 | 125,549 | 35 | 254.8s |
| pytest__doctest-optionflag-leak | FAIL | failed | unknown | unknown | unknown | 0 | 0 | 0.2s |
| rich__double-width-wrap | FAIL | failed | 819,184 | 40,012 | 40,012 | 95,505 | 44 | 597.5s |

累计已报告 usage 下界：input **>=5,449,106**、output **>=317,635**、total **>=5,766,741**；usage coverage **206/209**。Agent 时间合计 **2749.4s**，不含准备、补丁收集和外部验证。

### 执行结果与验证结果分开

- `pvlib__pvlib-python-1707`：Agent `failed/invalid_finish: length`；validator `failed`。
- `matplotlib__matplotlib-22835`：Agent `limited/max_steps`；validator `passed`。
- `django__django-11734`：Agent `failed/invalid_finish: length`；validator `failed`。
- `psf__requests-6028`：Agent `limited/max_steps`；validator `failed`。
- `sphinx-doc__sphinx-10323`：Agent `limited/max_steps`；validator `failed`。
- `click__path-generic-type`：Agent `failed/model_http_403`；validator `failed`。
- `pytest__doctest-optionflag-leak`：Agent `failed/model_http_403`；validator `failed`。
- `rich__double-width-wrap`：Agent `failed/invalid_finish: length`；validator `failed`。

Matplotlib 是真实的 `limited/max_steps` 但验证 PASS。累计 input 超过一百万并不代表一次活动 context 超过一百万；FinalCtx/PeakCtx 显示单次普通模型输入，累计 Input 表示所有调用的总成本。

Click 和 pytest 的 Agent 遇到 `model_http_403`；pytest 没有成功模型调用或工具执行，usage/context 为 unknown。这些是真实失败尝试，不是完整能力/成本观测，不能将缺失数据当作 0 或用于宣称 Context 优化收益。HTTP 403 的具体服务端原因未确认。

### 证据与比较

- 本机结果目录：`C:\Users\Archer\.nexus\evaluation\results\20261002T193618Z-96e0877c`。
- [完整终端报告](C:/Users/Archer/.nexus/evaluation/results/20261002T193618Z-96e0877c/report.txt)
- [可比较 summary.json](C:/Users/Archer/.nexus/evaluation/results/20261002T193618Z-96e0877c/summary.json)
- [运行 manifest](C:/Users/Archer/.nexus/evaluation/results/20261002T193618Z-96e0877c/manifest.json)
- 每项的 result.json 引用唯一 Session JSONL、候选 patch 和独立 validator 日志。
- 已核对本次实际运行源码/依赖/评测资产摘要与当前文件完全一致；八个 case 的 task、validator、reporter、image 身份一致，引用证据存在。

### 限制与停止线

- V0.2.1 尚未实现，After 对照未运行；本次仅交付 V0.1.1 基线。
- 这是固定 Next Dev Set V0 的本地验证，不是官方 SWE-bench resolved 结果。
- 八次单次模型运行不能证明统计显著性；输出长度上限和 40 步上限是这份基线的实际条件，未为提升 PASS 率而调整。
- Eval 面向源码开发安装；固定镜像使用已验证本地 ID，跨机同条件运行需迁移相同镜像。重建不能静默替换冻结 ID。
- 不支持评测 resume、并行调度、通用 benchmark 配置、自动 RCA 或 comparison CLI。
- Agent Loop、Context 投影/压缩、普通 native tool 行为保持原有语义；只增加组合层工具/环境显示注入和 Profile 纯数据导出。



## 最终完整性与改动范围

- 八个 session_id、run_id 均独立；本次 compaction calls/count 均为 0。
- 完成后本轮 `nexus-eval-*` / `nexus-collect-*` 容器及评测临时 workspace 均已清理；镜像、源树缓存与结果证据保留。
- 新增 `src/nexus/evaluation/` 五个功能模块，固定八项数据/环境/validator，以及两份评测测试文件。
- 原运行时仅改 `app/bootstrap.py`（可选工具/环境注入）、`app/cli.py`（两个 eval 入口）、`app/profile.py`（纯指标导出）、`core/context.py` 的 instructions 展示；未改变 Agent Loop 或 Context 策略。
- 更新两份 README、批准设计和本验收记录；`.gitattributes` 保护冻结评测资产字节。
- 本轮改动 git add；未 commit、push、merge 或发布。原有 `.idea/`、`i_love_you.txt` 未修改。
