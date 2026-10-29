# Stage 6 Durable Execution V1 · 实施与验证

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
