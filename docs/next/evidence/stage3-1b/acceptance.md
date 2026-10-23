# Stage 3.1B：Patch 协议可靠性与 command 参数兼容

## 范围与结论

基于已验收 Stage 3.1A `b4061c412256d40809500c00a5d939092896082e`，在
`D:/WorkSpace/releasePro/github/Nexus/Nexus-stage3` 的
`codex/nexus-v0.3-stage3-harness-hardening` 开发。本轮不合并 V0.3。

本地定向测试 **211 passed**；全量 **736 passed, 10 skipped**；另行启用的真实 Docker
执行边界测试 **1 passed**；Ruff、Mypy、lock、build 和源码目录外 wheel smoke 通过。
这证明已覆盖的协议、恢复和执行边界，没有证明真实模型更少生成错误 Patch 或 `cmd`。

仅新增两种受控行为：Add File 内部纯空行规范化；唯一合法 `cmd` 参数别名。
匹配仍为精确匹配，然后仅忽略行尾空白，每一阶段都要求唯一候选。
不忽略 Python 行首缩进、不猜旧代码、不补 End、不改 Agent Loop/Compaction 阈值/Memory。

## 修改文件

| 文件 | 内容 |
|---|---|
| `src/nexus/tools/registry.py` | 明确 Patch 边界、每个 @@ 自有编辑正文、同文件同 section、Add 空行 `+`；示例保留 Python 缩进并展示空行；command 正式名称与 JSON 示例 |
| `src/nexus/tools/patch_format.py` | 仅规范化 Add 内部纯空行；线性后继扫描；冲突预期文本、当前搜索起始行/总行数、最多 5 个歧义候选行及恢复指导 |
| `src/nexus/tools/patch.py` | 结果按需记录 `add_file_blank_lines_normalized` 计数 |
| `src/nexus/tools/execution.py` | 共享无副作用别名规范化；原验证/执行流程不变；按需兼容标记 |
| `src/nexus/evaluation/environment.py` | Eval Docker 执行器使用相同边界及标记 |
| `tests/test_stage31b_reliability.py` | 格式/诊断/真实失败/alias/真实会话 Replay 回归 |
| `tests/test_evaluation_v0.py` | 13 类非法别名请求 × native/Docker，禁止启动进程 |
| `tests/test_evaluation_docker.py` | 实际 `env.registry()` 的 alias 执行和原参数保留 |
| `tests/test_patch_v02.py` | 可执行工具描述示例随缩进和 Add 空行更新 |
| `tests/fixtures/stage31b/*` | 真实请求、错误返回、修复请求及可核验历史源文件；属性固定源文件字节 |
| `docs/next/evidence/stage3-1b/*` | 本报告、日志、预检、失败复现及离线比较脚本/输出 |

## 真实 FilePilot 失败审计

原始轨迹可访问，没有用合成请求冒充真实轨迹：
`C:/Users/Archer/.nexus/sessions/bca7b89a6a50f2d727f0c5ae/2925ee2af1b441a4af9b5d91298e44eb.jsonl`。
完整 SHA256 为 `cffef6eb18b6fb0d4e8f4db5a15158477f58030ee7728fc2d2a16e2f62855543`。
[原请求与恢复窗口](../../../../tests/fixtures/stage31b/rename-failures.json)保留 call_id、原始 arguments_json、错误结果及修复请求；
[新旧诊断](failure-comparison.json)由[离线实际执行脚本](reproduce_failures.py)生成。

| 原 result seq / 文件 | 原因 | 新实现结果 | 是否需要模型修复 |
|---|---|---|---|
| 86 / `app/fsops.py` chunk 2 | 连续 @@ 把上下文与真正编辑分成两个 chunk，前一个无 +/- | 仍为 invalid_patch；工具描述提前明确该约束 | 是；真实修复请求解析通过 |
| 138 / `web/app.js` chunk 2 | 同类 context-only chunk | 仍为 invalid_patch | 是；真实修复请求解析通过 |
| 221 / `tests/test_web_rename_ui.py` line 241 | 裸空行后还有非空 docstring 正文缺少 +，并非只有空行 | 仍拒绝；不能推断或补造非空内容 | 是；真实修复请求解析通过 |
| 229 / `tests/web/create_delete_harness.js` chunk 5 | 旧代码 `console.log(JSON.stringify(out))` 与当前源文件不符 | 仍为 patch_conflict；现在明确搜索从第 304 行开始、文件共 528 行、预期旧文本和重建指导 | 是；真实修复已实际写入并匹配历史 after_hash |

第四项输入源文件来自 FilePilot `4a82987:tests/web/create_delete_harness.js`，SHA256
`274344f1938e1f2569512414e1307196342243c8b2810ab71d0fdc574c974f69`，等于历史成功修复的 before_hash。
修复后 SHA256 为 `09bb8514537ef79afbb83a348911d00a74887c00da3830fb95e6d2ccdf7fed98`，与真实成功结果一致。
前三项只验证真实修复请求可解析，未重建全部文件的历史 preimage，不能宣称已完整重跑其多文件修复。

失败到下一次相关成功结果的历史窗口：86→94 为 1 次模型/1 次工具，138→151 为 2/1，
221→248 为 3/4，229→240 为 1/2。窗口包含修复工具调用，后两项重叠，不能相加为因果收益。
其中可见一次 `Select-String web/app.js`；不足以证明反复读取失败目标文件。
改动的确定性收益是更有界的定位提示及有限空行兼容；减少实际重读/重试须由后续 8 Case 观察。

## 安全格式与诊断边界

仅满足以下全部条件才把裸空行当作 Add 内容空行：已在 Add File 正文中；之前已有显式 + 行；
该行完全为空（LF/CRLF 均可）；向后越过同样的纯空行后，下一行也是显式 + 正文。
前导空行、末尾空行、文件 section 间隔、空格/Tab-only 行、非空缺前缀、缺失 End 都继续拒绝。
现有有效 Patch 的字节语义不变。后继检查为一次 O(n) 扫描，避免连续空行导致重复扫描。
`add_file_blank_lines_normalized` 只在完成解析且发生规范化时出现，含义是解析规范化计数，**不是文件写入成功计数**。
写入是否完成仍以 ok/partial/files/patch_diff 为准。

预期文本最多 256 UTF-8 字节，整体诊断最多 2048 字节；歧义候选最多显示前 5 个起始行。
当前位置来自真实读取的文件和搜索 cursor，不搜索相似代码来猜测目标。
原有精确优先、行尾空白兼容和唯一性策略未扩大；CRLF/无结尾换行、缩进、重复锚点均有测试。
本地 Codex `codex-rs/apply-patch/src/seek_sequence.rs` 用作参考；未引入其 trim-both、Unicode
标点等更宽松匹配，也未采用首个命中策略。

## command / cmd 边界

正式 JSON Schema 仍只有 `command`（必需）、`workdir`、`timeout_ms`。
仅 command 键不存在、cmd 为非空字符串且无其他未知字段时，复制参数字典并内部改名。
原字典、原 Assistant Tool Call 的 arguments_json 保持不变。
改名后继续走每个环境原有时间、类型、cwd 和进程校验。

| 请求 | 结果与证据 |
|---|---|
| 标准 command | 正常执行，无兼容标记 |
| 唯一合法 cmd | native 真实命令及 Eval Docker registry 实际执行成功；`argument_alias_used: "cmd_to_command"` |
| command 与 cmd 同时存在（即使 cmd=null） | invalid_arguments，未创建进程 |
| cmd 非字符串、空/纯空白、其他未知键 | invalid_arguments，未创建进程 |
| timeout bool/字符串/0/>600000；workdir 错误类型/空 | invalid_arguments，未创建进程 |
| 不存在的 workdir | 仍为 invalid_workdir；兼容标记不代表已执行 |
| 输出超限、取消、超时、子进程 | 保留原有截断/取消/清理行为；取消返回保留标记 |

真实 Agent Loop 的脚本模型测试核验：一次原始 cmd call 对应一次 ToolResult，落盘/Replay 原参数不被重写，
没有增加模型消息。另有真实 Docker smoke 同时覆盖隔离、超时、取消及子进程退出。

后续统计必须分开：原始 cmd 次数、这些调用实际 invalid_arguments 次数、alias 标记次数、
alias 获得进程 exit_code 次数、alias ok 次数。非零退出/超时仍有可能表明已通过参数验证执行，
但不能算成功命令；单有标记也可能是 invalid_workdir。

## Stage 3.1A 与安全回归

`test_patch_audit.py` 覆盖：实际修改后记录事件、部分成功、取消/日志失败/崩溃、独立 Diff 容量、
Hash/增删行、脱敏、TUI 一次有界预览、Tool Call/Result 配对、新日志 Replay、旧 `files[].diff`
Resume、Runtime Facts、Compaction 和 Checkpoint。全部随定向和全量通过。
模型 ToolResult 仍无 diff；不裁剪历史 Assistant Patch，也不把 patch_diff 作为模型消息。
原 Stage 3.1A [Token 对比](../stage3-1a/token-comparison.json)保留：等价 Rename 样本的
DeepSeek ToolResult 19,338→1,635 Tokens，节省 17,703（91.5451%），新增审计 JSONL 59,682 bytes。
这是 Stage 3.1A 的离线输出节省，不是本轮新测的成功率收益，亦不代表历史 Patch 污染消失。

## 验证与尝试记录

- [定向](directed-pytest.txt)：211 passed，12.36s。
- [最终全量](full-pytest-final.txt)：736 passed，10 skipped，52.29s。
- [真实 Docker smoke](docker-smoke.txt)：1 passed，11.99s。
  默认跳过的 10 项均为 opt-in Docker 测试；本轮另行开启其中 1 项，其余 9 项未单独执行。
- [Ruff](ruff.txt)、[Mypy](mypy.txt)（94 文件）、[lock](lock.txt)、[build](build.txt)、
  [源码外 wheel](wheel-smoke.txt)通过；git diff --check 通过。
- 第一次旧定向组合为 154 passed / 2 failed：两项旧测试仍断言合法 cmd 必须拒绝，与本轮授权行为冲突。
  已替换为 native/Docker 各 13 类非法输入拒绝测试，并增加合法真实执行测试。原工具输出有记录，
  当次未保存原始 stdout 文件，不能把后续日志冒充首次日志。
- 首轮全量 736/10 也通过（[原日志](full-pytest.txt)）；之后修正工具说明示例中的 Python 缩进、
  展示 Add 空行，再跑最终定向/全量。没有修改匹配策略来通过测试。

复现：从 Stage3 根目录使用现有锁定依赖解释器
`D:/WorkSpace/releasePro/github/Nexus/Nexus-next/.venv/Scripts/python.exe`：

```powershell
$py = 'D:/WorkSpace/releasePro/github/Nexus/Nexus-next/.venv/Scripts/python.exe'
& $py -m pytest tests/test_stage31b_reliability.py tests/test_patch_v02.py tests/test_patch_audit.py tests/test_tool_reliability.py tests/test_tools.py tests/test_evaluation_v0.py -q
& $py -m pytest -q
& $py -m ruff check .
& $py -m mypy src tests
uv lock --check
uv build
& $py docs/next/evidence/stage3-1b/reproduce_failures.py
```

测试使用系统临时目录，没有在仓库根目录新增 pytest 临时工作区。失败测试临时证据保留，不修改权限。

## 8 Case 启动与后续比较

用户最后补充明确授权：推送 Stage3 后直接启动 `eval --all`，无需等待审查或监控结束。
本报告交付时只记录预检和命令，不预先填写本轮评测成绩；具体启动 PID/run_id 在启动记录中保存。

主基线 `20261023T143533Z-46a28d3d`，commit `709dab8efa1c25e4e232b13a85c1d51e717b8e3c`：
7/8 PASS、230 模型调用、352 工具调用、38 次 apply_patch、6 invalid_patch、1 patch_conflict；
原始 cmd 37 次并全部 invalid_arguments；总 invalid_arguments 38 次（另有一次 Patch 参数错误）。
输入 8,169,091 / 输出 147,898 Tokens；Agent 时间 1,464.172 秒。
[复算结果](baseline-analysis.json)与各 result.json 工具/模型计数、原始参数及配对一致。

[预检](preflight.json)：相同八项和顺序，task/validator/reporter/base/image 标识全部一致；
DeepSeek `deepseek-flash`、high、128k context、32768 输出、300s 请求超时、40 步；
压缩模型 Qwen3.5-35B-A3B 配置也一致。八个镜像及缓存源码本地存在，无需拉取。
以 `PYTHONPATH` 固定本次 Stage3 源码，避免 PATH 上旧版 nexus；评测入口/参数无改动：

```powershell
Set-Location 'D:/WorkSpace/releasePro/github/Nexus/Nexus-stage3'
$env:PYTHONPATH = 'D:/WorkSpace/releasePro/github/Nexus/Nexus-stage3/src'
& 'D:/WorkSpace/releasePro/github/Nexus/Nexus-next/.venv/Scripts/python.exe' -m nexus eval --all
```

后台启动用独立隐藏进程、重定向 stdout/stderr，启动后只确认进程/首个 case 开始。
标准 Eval 保持历史默认目录 `C:/Users/Archer/.nexus/evaluation/results/<run_id>`，
源码工作树及启动日志在 D 盘；不更改 HOME 或全局配置，不重跑失败任务，不改 Harness/validator。

睡醒后离线对比（将 `<new_run_id>` 替换为启动记录中的 ID）：

```powershell
& 'D:/WorkSpace/releasePro/github/Nexus/Nexus-next/.venv/Scripts/python.exe' docs/next/evidence/stage3-1b/analyze.py 'C:/Users/Archer/.nexus/evaluation/results/20261023T143533Z-46a28d3d' 'C:/Users/Archer/.nexus/evaluation/results/<new_run_id>' --output artifacts/stage31b-comparison.json
```

脚本只读日志，不执行模型/验证器；比较每项 PASS、Tokens/时间/模型调用、Patch 数/失败分布、
失败后下一个同路径 Patch、cmd 原始生成/实际拒绝/alias 执行、步数/limited 和 Stage3.1A
ToolResult 无 Diff / 审计关联。恢复计数为同路径后续调用代理指标，不代表确定因果，窗口可能重叠。
无效探索要人工回读轨迹，不能凭一次非零命令退出判定。

此固定集是 Next Dev Set V0 的本地独立验证，不是官方 SWE-bench 总榜。
本轮同时包含描述、格式、诊断、alias 以及 Stage3.1A 的差异；相同远端模型名也不固定模型权重。
应分别报告三类贡献，不能把最终分数变化简单归因于某项改动。

## 剩余风险

1. 空行兼容扩大了可接受语法，但只覆盖内部纯空行；四个真实失败仍须模型修复。
2. `cmd` 兼容会让原来参数拒绝的请求真正执行，这是明确授权的新行为；原始生成率必须继续统计。
3. 诊断增加有限结果长度；搜索位置只是恢复起点，不保证模型一次重建成功。
4. 本地测试不能证明真实模型成功率或成本改善；8 Case 只启动，最终分析留给后续审查。
5. 旧日志中的 Diff 与历史 Assistant Patch 保留；没有实现 Stage3.2 或 Historical Patch Pruning。
