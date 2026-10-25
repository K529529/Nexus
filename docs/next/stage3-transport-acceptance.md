# Stage 3 · Model Transport 异常专项验收

日期：2026-10-24。开发目录：`D:/WorkSpace/releasePro/github/Nexus/Nexus-stage3`。基线：`412e51175c9a116bdb327a4ac2e9a7782d4d7e7f`。目标分支：`codex/nexus-v0.3-stage3-harness-hardening`。

## 结论与范围

本轮修复 Model Adapter 对未包装 HTTPX 传输异常的处理、Attempt 结束事件缺口，以及显式 Stream Cleanup 覆盖主结果的问题。Agent Loop、Patch、exec_command、Context、Compaction、Memory、模型请求参数、300 秒配置和 Max Steps 均未修改。

确定性验收：**定向 88 passed；全量 776 passed、10 skipped；Ruff PASS；Mypy PASS（95 source files）**。10 个 skip 均为已有的 opt-in Docker isolation acceptance，本轮未启用。没有运行付费模型、第三轮 8 Case 或 Stage 3.2，没有 Resume 历史评测会话。此前 4/8、6/8 报告与未跟踪文件保留。

本轮证明的是故障处理契约，不证明外部网络已修复，也不预测下一轮通过率。

## 1. RCA：真实日志能证明什么

只读检查指定两轮 JSONL，并在 [rca.json](evidence/stage3-transport/rca.json) 保存每个 session 路径、SHA-256、配对数量和失败事件：

| Run | Case | Step / Attempt | 已保存事实 |
|---|---|---|---|
| `20261023T212800Z-def5aaf5` | Matplotlib | 3 / 1 | model_started 后直接 runtime_error:RemoteProtocolError；无 model_finished |
| 同上 | Click | 3 / 1 | 同类缺口 |
| 同上 | Rich | 3 / 1 | 同类缺口 |
| `20261024T055859Z-d3c0919a` | Django | 6 / 1 | 同类缺口 |

同时保存上述两轮中已归类的 model_transport，包括 Requests、Sphinx、pytest 及其他出现错误的 case。第一轮共 11 条已归类错误和 3 次未闭合请求；第二轮共 2 条已归类错误和 1 次未闭合请求。日志没有阶段、底层异常详情或 seen_delta，不能把四次故障定位到 create、stream、close 中某一个位置，也不能判定 DeepSeek、代理或本地资源为根因。

源码确认：

1. 旧 adapter 捕获 APIConnectionError、APIStatusError、TimeoutError；原样 HTTPX RemoteProtocolError/ReadError 不在此边界内。
2. 成功事件在 finally 关闭之前发送。关闭随后失败时，调用方收不到已完成的结果，但日志已经记录成功。
3. 读取或取消失败时，finally 的 stream.close 再抛异常，会覆盖原始异常。
4. SDK 的流读取使用异步迭代，并不保证所有底层异常都被包装成 APIConnectionError。实际 SDK + HTTPX MockTransport 的断流测试已覆盖此路径。

[rca.py](evidence/stage3-transport/rca.py) 从 Git 读取基线 model.py，在同一组 Fake Client/Stream 上运行前后对照，未修改历史代码或 Session：

| 注入场景 | 基线行为 | 新行为 | 新 starts/finishes |
|---|---|---|---|
| create 两次 RemoteProtocolError | 原始异常逃逸，1/0 | 两次有限尝试后 model_transport | 2/2 |
| 首个 Delta 前断流，第二次正常 | 原始异常逃逸，1/0 | 一次安全重试并成功 | 2/2 |
| 部分正文后断流 | 原始异常逃逸，1/0 | 不重试，model_transport | 1/1 |
| 完整响应后 close 断流错误 | 已记成功，但结果被关闭异常丢弃 | 回收客户端后保留完整结果，附清理诊断 | 1/1 |
| ReadError 后 close 再失败 | RemoteProtocolError 覆盖 ReadError | 主错误仍为读取传输错误；另记 close 类型 | 1/1 |
| 取消后 close 再失败 | 取消被 RemoteProtocolError 覆盖 | 仍为 CancelledError | 1/1 |
| 正常成功 | 返回完整结果 | 相同结果 | 1/1 |

## 2. 控制流与事件契约

旧流程：started → create/read/validate → finished/return-or-error → finally close。原始异常可能绕过 finished，close 又可能覆盖结果。

新流程：started → create/read/validate → 保存结果或主失败 → 有界 close/必要时回收客户端 → **唯一 finished** → 安全重试、返回或抛出主失败。

使用现有事件，不添加模型消息或新日志系统。model_finished 保留原有 `error` 作为错误码，避免再引入内容重复的 error_code 字段；补充：

| 字段 | 含义 |
|---|---|
| attempt | 本次 complete 内的 1 或 2 |
| phase | 主结果阶段 create / stream；清理本身成为主失败时 close；事件输出失败时 publish |
| error | 失败码；成功时不包含该键 |
| exception_type | 有界异常类名，最多 80 字符，不记录 str(exc) |
| failure_kind | transport / timeout / http / protocol / cancelled / internal / cleanup |
| duration_ms | 本次尝试至清理完成的耗时，包含有界清理 |
| seen_delta | 是否已收到有效输出片段 |
| retry | 本次结束时的重试决策；不是重试已完成的保证 |
| cleanup_warning | 仅异常清理时存在，包含 close 阶段、类型、client_closed/failed，以及必要的恢复异常类型 |

实际重试次数仍由后续 model_started 的 attempt > 1 统计。取消或事件写入失败可以阻止下一次尝试；不将 retry=true 当作已经发出了第二次请求。

诊断不保存异常文本、Headers、URL、API Key、Prompt、Reasoning 或 Tool Call 正文。现有 assistant_delta 展示语义保持不变。没有给正常调用增加额外 warning 事件；清理告警附在同一个结束事件内。

正常 Event Sink 下每个已开始 Attempt 有且仅有一个结束事件。事件存储本身失败时不能保证持久化成功，错误继续上抛，不重试模型请求、不伪造已落盘状态。新增故障测试覆盖 delta 发布失败与 finished 写入失败。

## 3. 分类与有限重试

捕获明确的 APIConnectionError、HTTPX NetworkError、RemoteProtocolError、ProxyError、TimeoutException 和 TimeoutError；HTTP 状态错误继续单独处理。**LocalProtocolError、ValueError 等不归类为网络故障**；记录终止信息后按原异常抛出。

- create 或 stream 阶段、尚无有效 Delta 的瞬时传输失败：最多重试一次，仍使用既有 0.25 秒间隔。
- HTTP 429/5xx：保留一次有条件重试；其他 HTTP 错误不重试；context_limit 不重试。
- Timeout：error 仍为 model_transport，failure_kind=timeout，保留既有调用方语义。
- 正文、Reasoning、Tool Call 片段任一已出现：不重新发送整个请求，部分 Tool Call 不进入 Loop 执行。
- 仅 role 或空心跳片段不算有效模型输出；不因 metadata-only chunk 阻止安全恢复。
- 已完整接收的 empty_or_invalid_final 保留既有特殊恢复策略，与传输错误共用最多两次 Attempt；这不允许重试部分 Reasoning 流。
- 用户取消保持 CancelledError，不转成传输重试。

为何部分输出不能重试：正文可能已显示，Reasoning 和 Tool Call 可能已在缓冲；重发会产生重复展示或不同的调用片段。当前实现丢弃未完成回复，不产生虚假 ModelReply，也不重新执行已经发生的工具副作用。

## 4. Cleanup 与资源安全

显式 stream.close 最多等待 2 秒。发生关闭异常时，不信任 Response.is_closed（HTTPX 可以在底层清理前设置它）；退役整个客户端连接池，再尝试 client.close，最多 2 秒。整个流程在当前任务内完成，没有后台清理任务。

- 主读取失败已存在：清理错误放入 cleanup_warning，不能替换主错误。清理无法确认时不继续重试。
- 完整有效回复已存在，关闭仅发生明确的传输/超时异常，且 client.close 成功：保留回复及 usage，结束事件仍为成功，记录 recovery=client_closed。
- 连客户端清理也失败或超时：标记 model_cleanup_incomplete，禁止继续使用该 adapter；没有主错误时明确失败，不谎称资源已经回收。若主错误存在则保留主错误，同时保存 recovery=failed。
- 关闭中的未知编程错误不静默吞掉，即使客户端回收成功也按失败传播。
- Nexus 自己创建的客户端确认回收后，下次使用时新建相同配置的客户端，不复用可疑连接。仅重建连接池，不额外发起模型请求。测试注入的外部客户端没有可安全推断的构造方式，因此回收后拒绝复用，返回 model_client_closed。
- 取消期间也走有界清理；关闭过程中收到新的取消仍保持取消语义。

已验证 async close 可取消的情况。若第三方协程刻意吞掉取消或阻塞事件循环，asyncio deadline 无法强制杀死它；本专项没有增加后台任务或进程来绕过这一边界。

另一个边界：SDK 自身可能在异步迭代内部自动关闭响应；若 SDK 已经用清理异常覆盖了更早的底层异常，adapter 无法恢复从未收到的原始错误。本次保证的是 adapter 看见的主异常不会再被自身显式 close 覆盖。此类内部异常记录为 stream 阶段，不猜测其更深层来源。

## 5. 测试结果与兼容性

[定向完整日志](evidence/stage3-transport/targeted.txt) / [逐项 JUnit](evidence/stage3-transport/targeted.xml)：88 passed，其中 48 个既有模型测试、40 个新增 transport 测试。

| 测试类别 | 实际结果 |
|---|---|
| create/首个 Delta 前：RemoteProtocolError、SDK Connection/Timeout、ReadError、ProxyError | PASS；最多一次重试，阶段与类型正确 |
| 部分 Content / Reasoning / Tool Call 后断流 | PASS；不重试，不生成完整工具调用 |
| 两次瞬时失败 | PASS；两个配对 Attempt 后终止 |
| HTTP 400/401/429/500/503、context_limit | PASS；原分类与重试边界保留 |
| 成功后 close 失败/挂起 | PASS；池回收确认后保留结果 |
| read + close 双失败、客户端清理失败/挂起 | PASS；主原因保留，资源不明时停止复用 |
| create/stream/close 取消、真实 asyncio Task.cancel | PASS；无重试，有界结束 |
| LocalProtocolError、ValueError、未知 cleanup 错误 | PASS；不伪装成网络错误 |
| SDK 实际流迭代 + HTTPX Fake ByteStream | PASS；原始 RemoteProtocolError 进入 adapter 分类 |
| Event Sink 失败 | PASS；不盲目重试、不重复 finished 调用 |
| owned client 回收后的新请求/安全重试 | PASS；用新池，不重用旧连接 |
| Loop + JSONL + Replay/Resume + Rich TUI + RunProfiler | PASS；消息不改写、不执行半截工具、正文只显示一次、失败/重试计数正确 |
| 旧事件缺少新增字段 | PASS；Replay 消息相同 |

[全量日志](evidence/stage3-transport/full.txt) / [JUnit](evidence/stage3-transport/full.xml)：**776 passed、10 skipped，55.81 秒**。覆盖现有 Patch 审计、精简结果、cmd 别名、Session/Replay、TUI、Compaction 测试。未修改这些生产模块。

[Ruff](evidence/stage3-transport/ruff.txt)：All checks passed。[Mypy](evidence/stage3-transport/mypy.txt)：95 source files 无问题。

保留初始失败：第一次运行遇到旧系统 pytest 目录访问限制，同时发现新增诊断局部变量与 usage 局部变量重名，随后修正；隔离目录下又发现两个集成测试误把 Path 传给 read_records，修正为 BinaryIO 后通过。后者完整失败输出见 [targeted-initial.txt](evidence/stage3-transport/targeted-initial.txt)。这些是实施过程失败，未隐藏或混算为最终 PASS。测试使用系统临时目录的本次专用父目录，未创建仓库根 .pytest-tmp。

## 6. 数据量与性能

测量脚本与原始数据见 rca.py / rca.json。对同一 Fake 成功响应将 duration_ms 归零，以紧凑 UTF-8 JSON 计算 model_finished 的 data 载荷：

| 样本 | 基线字节 | 新字节 |
|---|---:|---:|
| 正常成功 | 184 | 233 |
| 成功但 close 有恢复告警 | 184（随后丢失结果） | 335（保留结果） |
| 部分输出断流 | 0（漏事件） | 185 |
| 读取与关闭双失败 | 0（漏事件） | 277 |

正常调用增加 **49 字节/Attempt**；同一事件封套未增加事件条数。表中不包含 JSONL 封套、磁盘 flush、真实长模型名等固定部分；不是文件总大小估算。

微基准每组 200 次 Fake complete，5 组取中位数，含模型对象创建：基线 **22.744 μs/次**，新实现 **24.863 μs/次**，增加约 **2.119 μs/次**。这是同进程无网络、无 JSONL 写入的局部测量，有计时噪声；不能外推真实评测耗时或成功率。异常 cleanup 的最坏额外协作等待约 4 秒（stream 2 秒 + client 2 秒），不改变模型请求超时配置。

## 7. 修改文件与剩余风险

生产变更：

- `src/nexus/core/model.py`：生命周期、明确异常分类、结束事件、重试边界、有界清理与客户端退役。
- `pyproject.toml` / `uv.lock`：将原本由 SDK 间接安装的 HTTPX 声明为直接依赖；离线 lock 只增加根依赖声明，未升级任何依赖版本。

测试与交付：

- `tests/test_model_transport.py`：40 项故障注入及实际调用链集成测试。
- `tests/test_model.py`：调整结束事件精确码和新增清理 deadline 的既有断言。
- 本报告与 `docs/next/evidence/stage3-transport/`：验收日志、原始数据及离线复现脚本。

新增行为风险：无有效输出时的原始 HTTPX 错误现在可能多发一次请求，可能增加服务商计费；服务器是否已处理请求不能由本地断流判断，但此阶段不会执行半截工具。role-only chunk 不再阻止该重试。成功清理后连接池可能被重建。模型协议错误的结束事件现在记录精确码，而非统一 stream_interrupted；已验证现有消费者仍正常，但外部自定义消费者应读取 error 字段而非假定唯一旧值。

未覆盖/不承诺：外部服务、代理、系统内存的真实故障根因；强杀进程时的事件落盘；底层不响应取消的阻塞；SDK 内部已经丢失的异常；真实付费 8 Case 的改善幅度。上述均不影响本轮确定性验收结论。

## 8. 复现命令与交付边界

在 Stage3 worktree，使用已装好依赖的 Python，pytest 使用系统临时目录专用父目录：

```powershell
$env:PYTHONIOENCODING = 'utf-8'
$transportTemp = Join-Path ([IO.Path]::GetTempPath()) ('nexus-transport-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory $transportTemp | Out-Null
$env:PYTEST_DEBUG_TEMPROOT = $transportTemp
$py = 'D:/WorkSpace/releasePro/github/Nexus/Nexus-next/.venv/Scripts/python.exe'
& $py -m pytest tests/test_model.py tests/test_model_transport.py -q
& $py -m pytest -q
& $py -m ruff check .
& $py -m mypy
& $py docs/next/evidence/stage3-transport/rca.py
```

仅提交本专项的明确文件清单并推送 Stage3 分支；不纳入此前未跟踪的分析报告、.idea 或其他证据目录，不合并 V0.3。真实 `nexus eval --all` 保持 NOT RUN，等待审查后再决定。

收尾检查：已将首次集成测试失败时生成的两个 JSONL 样本保存为 evidence/stage3-transport/initial-fixture-1.jsonl 与 initial-fixture-2.jsonl，逐文件哈希一致。检查无 junction/symlink 后，已清理本次专用系统临时目录（包括通过测试遗留的 Git 只读对象）；未触碰其他 pytest 目录。
