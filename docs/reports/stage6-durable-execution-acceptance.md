# Stage 6 Durable Execution V1 · 实施与验证

## 2026-10-30 封版与合入记录

用户在了解本次 Local 8 Case 为 **5/8** 及失败原因后，明确授权将 Stage 6 合入并推送
`codex/nexus-v0.3`。这是保留已知模型解题失败的合入决定，不将其记为八例全部通过。
开发分支保留，`main` 不在本次合入范围内。

### 最终增量与自动化验证

| 提交 | 变更 | 本会话已执行的检查 |
| --- | --- | --- |
| `d7feff7` | Tool-level Durable Execution V1 初版 | 见下文 2026-10-29 原始记录 |
| `1929de4` | Recovery Guard 仅匹配工具名与规范化参数摘要；旧 UNKNOWN 不重放、不因新调用获准而清除 | 定向测试 89 passed；全量 pytest 1057 passed、10 skipped；Ruff、mypy 通过 |
| `cbe55f1` | 区分 MEMORY.md、Daily Notes、SCRATCHPAD.md；删除最终回复前专门判断写入的要求 | Memory 相关测试 92 passed；Ruff、mypy 通过；AST 对比确认仅提示词/描述变化，六个工具 schema 一致 |
| `38d1b03` | 删除关于无需为总结任务写入长期记忆的后半句，保留前句 | Memory Prompt/会话测试 13 passed；Ruff 通过 |

以上后续检查结果来自本次开发会话执行输出；不冒用下文初版日志作为后续提交的原始日志。
10 项 skipped 是既有可选 Docker 隔离验收。合入收尾只增加本说明与证据归档，未修改运行代码，
未重跑模型或用重试结果替换失败记录。最终 Recovery Guard 语义以
[技术说明](../next/stage6-durable-execution.md) 为准；下文“未知风险下所有新 Shell 均审批”
是初版历史行为，已由 `1929de4` 收窄。

### 用户手动 Local 8 Case：5 PASS / 3 FAIL

- 命令：`uv run --frozen nexus eval --all --permission-mode local`。
- Run：`20261030T090846Z-5c8b8566`；被测提交：`38d1b0352bebc5be1b014d089f566544b995ed66`。
- 结果：**5 PASS、3 FAIL、0 ERROR、0 ABORTED、0 NOT_RUN**；失败均为固定验证断言失败。
- 模型：`deepseek-flash`，reasoning effort `high`，128000 context，32768 max output，40 steps。
- 原始目录：`C:\Users\Archer\.nexus\evaluation\results\20261030T090846Z-5c8b8566`。

| Case | 本次结果 | 失败原因 |
| --- | --- | --- |
| pvlib__pvlib-python-1707 | PASS | — |
| matplotlib__matplotlib-22835 | FAIL | `test_format_cursor_data_BoundaryNorm`：期望 `[0.9]`，实际 `[0.9000000000000000]`，补丁的数值格式精度不符合要求 |
| django__django-11734 | FAIL | `test_subquery_exclude_outerref`：`qs.exists()` 仍返回 True，期望 False，补丁没有正确解决查询语义 |
| psf__requests-6028 | PASS | — |
| sphinx-doc__sphinx-10323 | PASS | — |
| click__path-generic-type | PASS | — |
| pytest__doctest-optionflag-leak | PASS | — |
| rich__double-width-wrap | FAIL | `test_width_smaller_than_glyph`：宽度 1 时额外产生开头空行；固定验证 156 passed、1 failed |

### Rich 失败与前次对照

本次模型在 `rich/_wrap.py` 新增 `_fold_word()`。首字符“中”的终端宽度为 2，
行宽为 1 时，函数立即将偏移量 0 加入切分点，产生首个空行。因此：

```python
Text("中A文").wrap(Console(), 1)
# 期望 plain 列表：[" ", "A", " "]
# 实际 plain 列表：["", " ", "A", " "]
```

从保存的补丁提取该函数，在内存中连续三次调用均得到切分点 `[0, 1, 2]`，
确认首个空段是确定性代码缺陷。模型新增的参数化测试仅覆盖宽度 2–6；修改后的额外
检查也从宽度 2 起步，遗漏了宽度 1。模型自己的定向测试为 137 passed，不能替代固定验证。
模型尝试全套测试时还遇到缺少 `pkg_resources`，忽略对应模块后遇到 Markdown 颜色码断言
差异；这些是另行保留的验证限制，不是上述 Rich 固定验证失败的原因。

前次 `20261028T104039Z-57e6d22e`（提交 `6853e70`）是 6/8，Rich 通过，
Matplotlib 与 Django 失败。本次这两个失败仍是同一断言；前次 Matplotlib 实际为 `[0.900]`。
前次 Rich 修改 `chop_cells()`，只有当前行已有内容时才换行；本次选择了不同补丁方案。

两轮 manifest 中模型配置、预算相同；这三个 Case 的来源提交、镜像、任务及验证器
provenance 相同。Rich 会话的持久化系统指令与用户任务逐字相同；本次没有恢复事件、
审批事件、Memory Snapshot 注入或上下文压缩，Gateway 未产生额外审批。
因此工程归因是模型生成方案的波动与边界测试遗漏，未发现 Stage 6 恢复机制导致失败的证据。
这不是同版本多次受控实验，不能从两轮结果证明全部版本影响为零，也不据此声称模型能力提升。

### 证据保留

- [本次八例结构化结果](../next/evidence/stage6-durable/local-8case-20261030/summary.json)。
- [原始证据 ZIP](../next/evidence/stage6-durable/local-8case-20261030/raw-evidence.zip)：
  本次八例的 result、patch、tests、validator log 及 run manifest/summary/report；
  Docker 未启动的 `20261030T090702Z-27b43d64` 准备失败记录（8 NOT_RUN）；
  前次 6/8 的 run manifest/summary/report 与三个失败对照 Case 的产物。
- [SHA-256 清单](../next/evidence/stage6-durable/local-8case-20261030/sha256.json)：
  ZIP 内 61 份文件逐字节核对通过，保留原始换行和失败输出。

完整会话 JSONL 留在原始本机目录，未打包发布；result 中保留其相对定位信息。
下文为初版交付时的历史结果，初版的 NOT RUN 和“尚不合入”不覆盖本节后续验收与授权。

---

## 2026-10-29 初版交付记录

日期：2026-10-29（Asia/Shanghai）。开发分支：
`codex/nexus-v0.3-stage6-durable-execution`。基线：
`10b473ef0da38125b64abf20e3c5947b6d7f2c78`，抓取和交付前远端核对均一致。
用户明确授权实现、提交并推送开发分支；随后明确将 Local 8 Case 改为用户自行手动运行。

## 实现摘要

- Session JSONL 新增以 run_id/call_id 关联的 PREPARED、DISPATCHING、COMPLETED
  checkpoint，关键边界 flush + fsync；完整结果在普通 tool message 之前保存。
- Resume 校验 checkpoint、恢复完整结果，只续跑明确未分发的 PREPARED；UNKNOWN
  不重放。保存损坏尾部原件，关联唯一恢复副本，防止再次选原件导致重复续跑。
- Patch 的全批 manifest、逐文件 planned/committed 日志与 ACK 持久化顺序统一。
  Worker ACK 增加事件阶段关联；恢复核对 before/after hash，识别 applied、not_applied、
  partial、conflict，缺完整结果时不伪造完成、不自动重新应用。
- 未知副作用风险跨 Run/resume 保留；新的 Shell/Patch/外部副作用操作使用现有 HITL
  逐次确认。保留 Gateway 原 Deny 和受限 Backend 路由。

详细语义和限制见 [技术说明](../next/stage6-durable-execution.md)。

## 自动化结果

主机：Windows，Python 3.12；测试使用当前 checkout 的锁定开发环境。

| 检查 | 结果 | 证据 |
| --- | --- | --- |
| `uv run --frozen pytest -q` | **PASS：1048 passed，10 skipped，124.60s** | [最终全量日志](../next/evidence/stage6-durable/full-final.log) |
| 恢复/Plan/Skills 最后一次专项 | PASS：105 passed，19.62s | [专项日志](../next/evidence/stage6-durable/focused-05.log) |
| `uv run --frozen ruff check .` | PASS | [Ruff](../next/evidence/stage6-durable/ruff-final.log) |
| `uv run --frozen mypy src tests` | PASS：124 source files | [Mypy](../next/evidence/stage6-durable/mypy-final.log) |
| `uv lock --check` | PASS | [锁文件](../next/evidence/stage6-durable/lock-final.log) |
| `uv build` | PASS：wheel + sdist | [构建](../next/evidence/stage6-durable/build-final.log) |
| 源码目录外离线安装 wheel、导入恢复模块及运行 CLI help | PASS；临时环境已删除 | [安装日志](../next/evidence/stage6-durable/wheel-install.log)、[复核脚本](../next/evidence/stage6-durable/verify_wheel.py) |
| `git diff --check` | PASS | 交付前直接执行 |
| `uv run --frozen nexus eval --all --permission-mode local` | **NOT RUN：按用户最新指示留给用户手动执行** | 本轮未产生八例评测结果，不引用旧结果充当本轮结果 |
| Stage 6 真实 Linux Docker / Windows Native OS 隔离验收 | NOT RUN | 本轮是本地崩溃恢复与 Bridge/Worker 协议验证，未重新进行 OS 隔离验收 |

10 个 skipped 均为原有 `tests/test_evaluation_docker.py` 的 opt-in Docker 验收测试；
未通过缩小全量测试扫描范围隐藏失败。最终全量测试包含新增的 29 个恢复测试。

崩溃测试使用独立子进程中的 `os._exit(77)`，跳过 Agent 的 finally 和取消清理：

- 非幂等 Shell 真正追加并 fsync 一个字节，覆盖 PREPARED、DISPATCHING、执行中、
  执行已返回但结果未保存、COMPLETED 已保存五个窗口；每个窗口再覆盖完整与损坏尾部。
- 续接和再次 resume 验证未知调用不增加计数，只有明确 PREPARED 执行一次；完整结果
  逐字段恢复，包括 stdout、error_code、duration_ms 和 truncated。
- Patch 在 manifest、planned、实际文件写入后但 committed 前、committed 后强制退出，
  校验磁盘哈希和并发编辑冲突，不写第二个文件或重放原补丁。
- fsync 故障停止后续工具；新 call_id 不能绕过风险确认；拒绝、批准一次、无 approver
  三条分支均覆盖。另覆盖参数脱敏、checkpoint 关联错误、批次顺序与内部工具二次恢复。

## 失败尝试保留

| 尝试 | 实际结果及处理 |
| --- | --- |
| [focused-01](../next/evidence/stage6-durable/focused-01.log) | 100 passed、13 failed；旧测试预期写后审计或固定旧事件序列，改为校验写前屏障及 planned/committed |
| [focused-02](../next/evidence/stage6-durable/focused-02.log) | NOT RUN；工具沙箱拒绝读取 D:\\uv-cache；随后用授权执行环境运行 |
| [focused-03](../next/evidence/stage6-durable/focused-03.log) | 79 passed |
| [full-01](../next/evidence/stage6-durable/full-01.log) | 1032 passed、12 failed、10 skipped；剩余旧 Patch/Plan/Skill 事件顺序断言更新 |
| [focused-04](../next/evidence/stage6-durable/focused-04.log) | 235 passed |
| [full-02](../next/evidence/stage6-durable/full-02.log) | 1046 passed、10 skipped；随后补入内部工具恢复 step 与批次后续结果保留两项检查 |

另修复了开发过程中的 lint/type 问题；最终结果以上表 final 日志为准。
两份失败日志的可读 `.log` 仅清理行尾空白以通过 diff 检查，原始字节分别保存在
[focused-01.raw.log.gz](../next/evidence/stage6-durable/focused-01.raw.log.gz) 和
[full-01.raw.log.gz](../next/evidence/stage6-durable/full-01.raw.log.gz)。
Docker 初始未启动，准备八例回归时已启动 Docker Desktop；收到用户自行运行指示后
未启动评测，也未进行新的模型调用。本轮不包含模型能力、八例通过率或性能改善结论。

## 已知限制与交付范围

不是外部副作用 exactly-once 或事务回滚。没有完整持久化结果就保持 UNKNOWN；
不会重连旧 PID，也不会根据一次哈希检查声称旧进程已停止。Windows 文件数据执行
fsync，但目录项断电持久性未验证；本轮无物理断电或 Linux 实机验证。

未知风险下 Shell 均保守请求确认，包括用于查看状态的 Shell；没有自动清除风险或
批量解除风险接口。一次批准只对应当前新操作，不能修改旧调用的事实。

仅提交本轮源码、相关测试、技术说明和验证记录。保留原有 `.idea/`、
`docs/next/evidence/stage4-srt-poc/` 及两份 `scripts/stage4_srt_*` 未跟踪文件，
不纳入本次提交。普通 pytest 使用系统临时目录，仓库根未新增 `.pytest-tmp*` /
`.pytest-temp*`；未清理用途不明的历史路径。只推送 Stage 6 分支，不合入 V0.3 或 main。
