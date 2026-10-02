# Nexus Next — Evaluation V0 Reduced Architecture

- 日期：2026-10-03
- 分支：`refactor/lean-agent-core`
- 状态：**IMPLEMENTED；Stage 1→5 及 V0.1.1 基线已完成，V0.2.1 After 待实现**
- 目标：对固定 8 个编码任务运行一次或全部运行，获得确定性验证结果、现有 Profile 指标及诊断证据，用于 Context V0.1.1 与 V0.2.1 的 Before/After 对照。
- 本文替换上一版提案；不新增长期 Contract / ADR 权威层。Stage 1 必须实际准备并验证八个 case 的镜像和 validator。
- 实施补充：Aggregate Report 一级表格必须包含 `FinalCtx` 和 `ToolResultBytes`。V0.2.1 尚未实现，本轮 Stage 5 先完成 V0.1.1 八项真实基线及可比较 JSON，After 对照待其实现后执行。

## 原方案组件裁决

| 原组件 | 裁决 | 本版处理 |
| --- | --- | --- |
| Agent 外部 Evaluation Runner | KEEP | 顺序编排固定 8 个 case，调用真实 Conversation |
| Conversation / Agent / ModelClient | KEEP | 全部留在宿主机，不复制 Agent Loop |
| RuntimeEvent / RunProfiler | KEEP | 消费相同事件，按需增加纯数据导出 |
| 单 case / 全套运行 | KEEP | 仅 `eval <case-id>` 与 `eval --all` |
| 干净 workspace 与固定 base | SIMPLIFY | 导出准确源代码快照，建立一个合成初始 commit |
| Git refs、reflog、alternates 和对象裁剪体系 | REMOVE | 不向 Agent 复制原仓库 `.git`，无需清理历史对象 |
| Docker 项目环境 | KEEP | 5 个 benchmark 来源环境、3 个 curated 环境，镜像复用，容器每次新建 |
| 容器内 Nexus worker | REMOVE | 容器只运行项目命令，不运行另一套 Nexus 进程 |
| Model API gateway / reverse proxy | REMOVE | 宿主机 ModelClient 直接访问现有模型服务 |
| sandbox.py / network 子系统 | REMOVE | 小型 Docker 工具适配集中在 environment.py |
| 网络与答案隔离 | SIMPLIFY | Agent 工具容器 `--network none`，无上游历史、隐藏测试或控制面挂载 |
| 独立验证容器与 workspace | KEEP | 原始 base + 候选 patch + 隐藏验证材料 |
| swebench / commands 两套 validator | SIMPLIFY | 统一为固定命令、受控测试材料和确定性结果采集 |
| 完整官方 SWE-bench harness 集成 | POSTPONE | V0 做本地固定任务验证，不声称获得官方 resolved 结果 |
| suite.toml / suite.lock.json / 大型 fingerprint 系统 | REMOVE | 固定 case 定义、镜像身份和小型运行 manifest 足够 |
| EvalCase TOML + task.md | SIMPLIFY | 只保留任务身份、环境镜像和 validator 定义入口 |
| CaseResult 与失败分类 | SIMPLIFY | 正确性与 Agent outcome 分开，六类诊断原因 |
| 轨迹、patch、日志归档 | SIMPLIFY | 每种证据保留一份，结果只保存引用 |
| 八行 aggregate report | KEEP | V0 一级交付，显示结果及主要资源指标 |
| 可复现性 / Before-After | SIMPLIFY | 固定 case、环境、验证规则和模型配置，直接对照 JSON |
| 框架测试与 validator 资格检查 | KEEP | 测关键边界，真实环境单独验收 |
| --prepare-only / --config / --suite | POSTPONE | 自动准备环境，使用现有用户模型配置 |
| resume / parallel / compare 命令 | POSTPONE | 不恢复半次评测，不并行，不新增比较工具 |
| 自动 RCA、数据库、队列、Dashboard、LLM judge、分布式 worker、插件框架 | REMOVE | 均不属于本版范围 |

## 1. 修订后的执行架构

**采用“宿主机 Agent + Docker 项目工具”的组合。Agent core semantics: unchanged。**

```text
宿主机
  eval CLI → 串行 Runner → Conversation → 现有 Agent / Context / ModelClient
                               │                             │
                               │ RuntimeEvents               └→ Model API
                               └→ RunProfiler
                               │
                         eval 工具注册表
                         ├─ exec_command → 无网络项目容器 /workspace
                         └─ apply_patch  → 一次性 workspace（同一挂载目录）

  Agent 结束 → 收集 patch → 全新验证 workspace + 容器 → 固定 validator
                                              ↓
                                   CaseResult → 全套汇总
```

当前代码支持这一组合：

- [Tool](../../src/nexus/core/types.py) 使用 `execute` callable；不要求工具与 ModelClient 处在同一进程环境。
- [Conversation](../../src/nexus/app/bootstrap.py) 当前直接调用 `native_tools()`。增加可选工具注册表注入即可；普通运行默认行为不变。
- [apply_patch](../../src/nexus/tools/patch.py) 已有 workspace 相对路径及越界检查，可以复用。
- [instructions](../../src/nexus/core/context.py) 当前使用宿主机 OS 和路径。评测需要小型显示参数覆盖，让模型看到 `Linux / /bin/sh / /workspace`，而非 Windows 宿主机路径。

没有发现必须引入模型网关的运行时约束。评测调用一次 `Conversation.turn(task)`；每个 case 创建新的 Conversation、Session 和 RunProfiler。评测结果不反馈给 Agent 继续修复，也不自动重跑模型获取 best-of-N。

## 2. 修订后的目录布局

以下布局已按批准范围实现：

```text
src/nexus/evaluation/
    __init__.py
    cases.py          # 固定清单、TOML 读取和小型数据结构
    runner.py         # 单 case / 八 case 串行编排
    environment.py    # 快照、Docker、工具适配、patch 收集和清理
    validation.py     # 新验证环境、固定检查、确定性判定
    report.py         # CaseResult、manifest、JSON 和终端汇总

evaluation/next-dev-v0/
    cases/
        <case-id>/
            case.toml
            task.md
    validators/
        <case-id>/
            validator.toml
            ...必要的隐藏 test.patch 或回归检查...
    environments/
        click/
        pytest/
        rich/
            ...Dockerfile 与必要依赖定义...
```

五个 benchmark 来源环境复用对应的固定依赖配置；如需去除已有答案或测试材料，只增加必要的镜像准备文件，不复制整套 harness。固定执行顺序在 `cases.py` 的一个常量中声明，无额外 suite 配置层。

运行结果默认放在 `~/.nexus/evaluation/results/`，可复用的控制面缓存放在 `~/.nexus/evaluation/cache/`。这些目录都不挂载给 Agent。

## 3. 修订后的 EvalCase schema

统一 TOML，不区分两种 validator 后端：

```toml
id = "pvlib__pvlib-python-1707"
source = "swe-bench-lite"
repo = "pvlib/pvlib-python"
base_commit = "40e9e978c170bdde4eeee1547729417665dbc34c"
task_file = "task.md"
image = "<prepared-image-pinned-by-sha256>"
validator = "../../validators/pvlib__pvlib-python-1707/validator.toml"
tags = ["numerical", "regression"]
```

`image` 是设计占位符，实施时必须替换为已准备镜像的不可变身份；加载器不接受占位符或浮动 `latest`。可使用仓库 digest，或已存在的本地 sha256 image ID。所有相对路径以声明它们的文件为基准解析。

字段只有：`id / source / repo / base_commit / task_file / image / validator / tags`。`tags` 可省略。不把网关、网络规则、通用任务调度配置塞进 case。

`task.md` 只包含模型应该收到的问题描述；验证目标、参考修复和隐藏资产留在控制面。case 定义和完整 task 不复制进项目 workspace。

固定清单不变：

| case_id | 来源 |
| --- | --- |
| pvlib__pvlib-python-1707 | SWE-bench Lite |
| matplotlib__matplotlib-22835 | SWE-bench Lite |
| django__django-11734 | SWE-bench Verified |
| psf__requests-6028 | SWE-bench Verified |
| sphinx-doc__sphinx-10323 | SWE-bench Verified |
| click__path-generic-type | Curated |
| pytest__doctest-optionflag-leak | Curated |
| rich__double-width-wrap | Curated |

各 case 的 repo、base 和任务内容沿用用户选定清单；不能在实现时换成最新 HEAD 或临时重写问题描述。

## 4. Workspace / base 隔离

采用单快照仓库：

1. 控制面取得指定 repo 的准确 base commit，记录原始 commit 和 tree 身份。
2. 将该 tree 的源文件导出到新 workspace，不复制原 `.git`、remote 或其它历史。
3. 在新 workspace 执行 `git init`，加入所有基线文件，建立**一个合成初始 commit**。
4. 记录其 `workspace_base_commit`，之后只把该 workspace 挂载给 Agent。

导出必须保留基线文件内容和必要文件属性，包括被 `.gitignore` 忽略但已跟踪的文件。不能直接假定默认 `git archive` 总是与原始 tree 等价：`export-ignore`、`export-subst`、换行转换和符号链接需要在准备阶段验证。

Agent 可以使用 `git status / diff / add / commit`。初始仓库没有上游历史或 future objects，因此无需 refs 清洗、reflog 清理或 Git 对象可见性扫描系统。

最终 patch 相对**合成初始 commit** 收集，不能仅使用当前 HEAD 的 unstaged diff；即使 Agent 已执行 commit，变更也必须保留。沿用现有 [collect_patch](../../scripts/swebench_v0.py) 的临时 index 收集思路，包含删除及可纳入 Git 的新增文件。原脚本检查真实 upstream HEAD 的逻辑不直接套到合成仓库上，也不修改旧脚本的既有行为。

收集以控制面保留的基线快照为准，不依赖 Agent 可能改写的 `.git` 元数据。涉及最终工作树的 Git 收集命令在无网络容器中使用干净的临时 Git 元数据执行，不在宿主机触发项目 hook 或 filter；这仍是 environment.py 内的收集步骤，不新增 Git 隔离子系统。

控制面原始 base 和合成初始 tree 必须内容等价，才能把该 patch 应用到全新的验证快照。每次评测重新生成 workspace；不 reset 用户仓库，不复用前一 case 的目录。

## 5. Docker 执行模型

镜像一次准备、多次复用。每个 case 新建执行容器，结束后销毁；验证另建一个容器。五个 benchmark 来源环境与三个 curated 环境都遵守这一规则。

准备阶段可以下载仓库、拉取或构建依赖镜像。镜像身份冻结后，Agent 与验证阶段均不在线安装缺失依赖。镜像缺失或不匹配时，内部准备函数尝试准备并核验；无法得到固定镜像就报告 `setup_error`，不能静默换成新版本。

评测工具注册表只包含：

| 工具 | 执行方式 |
| --- | --- |
| `exec_command` | 通过 Docker CLI 的结构化 argv，在当前 case 容器内执行 `/bin/sh -c <command>` |
| `apply_patch` | 复用现有实现，只操作宿主机的一次性 workspace；容器看到同一目录 |

`exec_command` 保留已有输入 schema、输出字段、输出限额和工具事件语义。工作目录按容器的 POSIX 路径解释，默认 `/workspace`；不能拿模型的路径当宿主机 cwd。

这需要一个小型 eval executor，而不是给现有 Windows shell 执行器换一个字符串。现有执行器会根据宿主机 `sys.platform` 选择 shell；Docker 命令必须由受信任参数构造，不经过宿主机 PowerShell 展开。可复用现有输出缓冲和结果类型。

超时或取消必须终止**容器内对应的进程组**，并在继续前确认清理完成。只结束 `docker exec` 客户端不够。若无法确认，停止该 case 容器并记录精确清理错误，不带着残留进程继续运行。该边界纳入 Docker 验收，不扩展成通用 worker/RPC 系统。

准备、Agent、验证分别计时。Agent duration 沿用现有 `RunProfiler` 的 run 边界，包含模型和工具执行，不包含镜像下载、workspace 准备及外部验证。

## 6. 网络隔离

- 执行容器使用 `--network none`；不挂载宿主机 Docker socket、用户目录、源码缓存或结果目录。
- 工具进程不接收宿主机模型密钥、用户 MCP 配置或代理环境变量。
- 评测只注册两个 native tools，不连接用户 MCP，不提供 search/network 工具。
- ModelClient 在宿主机直接访问用户已配置的模型 API；模型密钥仍从原环境变量读取。
- 控制面和隐藏 validator 资产始终位于 Agent 可见挂载之外。

项目镜像可以包含依赖，但不能预置参考修复、隐藏 test.patch、上游历史或可直接读取的答案缓存。优先使用环境镜像；已有 instance 镜像如包含这些材料，必须在准备时换用或构建干净版本。

这样阻断的是工具主动检索上游答案的路径。它不声称消除模型预训练中可能存在的 benchmark 记忆，也不试图在 V0 建造通用安全沙箱。

## 7. Validator 生命周期

只保留一个小接口：

```text
validate(case, candidate_patch, output_dir) → ValidationResult
```

统一流程：

1. 创建新的 base workspace 和新的固定镜像容器。
2. 应用保存的候选 patch；不复制 Agent 容器、依赖改动、进程或临时状态。
3. 安装该 case 声明的隐藏 test.patch / 检查资产。
4. 执行固定 FAIL_TO_PASS / PASS_TO_PASS 检查，记录退出码及实际测试结果。
5. 输出 `passed / failed / error / not_run`、检查明细、原因和耗时，销毁验证容器。

Validator 定义只需指定必要资产、固定命令、超时以及期望的检查集合。八个 case 共用此流程；测试结果解析按所用测试框架做少量函数处理，不引入插件发现或 validator 类层级。

**PASS 需要目标检查实际运行并通过。** 不能只看到 exit code 0 就通过；缺失目标、必需目标被 skip、未运行的检查都不能算通过。固定检查确实失败记 `failed`；环境故障、结果无法解析或缺少可靠证据记 `error`。

隐藏材料由控制面提供。对于与候选测试改动重叠的已声明隐藏测试路径，验证时恢复该路径的 base 再叠加隐藏材料；完整候选 patch 仍保留用于诊断。此规则随 validator 固定，不临时猜测冲突。不能借此覆盖候选产品源码；若隐藏补丁需要修改产品代码，必须先重新整理 validator。

| Case | 固定验证内容 |
| --- | --- |
| pvlib | 完整目标 FAIL_TO_PASS / PASS_TO_PASS，包含 `test_physical_n1_L0` |
| Matplotlib | BoundaryNorm cursor data 回归和原任务保留测试 |
| Django | 对应 ORM exclude / OuterRef 回归及原任务保留测试，固定数据库配置 |
| Requests | 两个认证 URL 参数回归及原任务保留测试 |
| Sphinx | dedent、prepend、append 顺序回归及原任务保留测试 |
| Click | 固定类型检查器，断言 Path / str / bytes 返回类型；禁止 Any 假通过，同时检查运行时兼容性 |
| pytest | 同一进程内 docstring option flags 隔离，覆盖失败、skip、xfail 路径及哨兵检查 |
| Rich | CJK / ASCII 混合文本的字符完整性与固定换行预期；宽度小于单字 cell 宽度时单独定义预期 |

五个 benchmark 来源 case 使用完整的原任务测试集合，不能只挑清单举例的一个测试。它们与 curated case 的区别在数据来源，不在执行架构。

正式跑模型前，每个 validator 都要通过资格检查：

- `base + validator`：确实复现目标缺陷，而非导入或环境错误。
- `reference fix + 同一 validator`：目标及保留检查通过。
- 参考修复和隐藏资产从未进入 Agent workspace 或执行镜像。

本版报告的是 **Next Dev Set V0 的本地验证结果**。不接入整套官方 harness，也不把本地 PASS 称为官方 SWE-bench `resolved`。若某 case 无法用固定流程可靠复现目标语义，先报告该 case 的具体阻碍，不直接扩大为通用 harness 集成。

## 8. RunProfiler 集成

每个 case 实例化现有 [RunProfiler](../../src/nexus/app/profile.py)，消费现有 RuntimeEvent。必要时只新增一个纯数据函数：

```text
profile_metrics(profile) → JSON-compatible dict
```

导出如下现有指标：

| 类别 | 指标 |
| --- | --- |
| 执行 | Agent outcome / reason、steps、Agent duration |
| 模型 | model calls、retries、failed attempts |
| 工具 | tool calls、tool failures、tool-result bytes |
| Usage | 累计 reported input / output / total，usage coverage 与调用总数 |
| 上下文 | first / peak / final normal model input |
| 压缩 | compaction calls、compaction count |

累计 usage 包含现有 Profile 计入的普通请求、重试及压缩请求；不擅自改聚合口径。上下文三项继续使用已有 `context_inputs` 语义，不把 token 估算混入 reported usage。

数据不足保留 `null / unknown` 和 coverage；已知 usage 部分和按下界显示。尤其不能在有 usage 缺失时将已观察到的局部最大值冒充完整 peak。

不解析终端文本，不复制另一套事件计数器，不修改 Agent / Context 决策。准备和 validator 的命令不进入 Agent Profile。评测 CLI 只打印简短 case 进度和最终汇总，不连续打印八份完整 Profile 或工具输出。

## 9. 结果 schema 与 artifacts

`manifest.json` 保存本次运行的最小身份：

- evaluation_run_id、时间、按顺序选择的 case IDs。
- Nexus commit；工作区有改动时记录影响运行的源码与依赖文件内容摘要，不能只写一个 `dirty=true`。
- 模型名、服务 base URL（不含凭证）、reasoning effort、context/output limits、步数和相关执行限额。
- Context policy/version；它是本次实验标签，真实代码身份由 Nexus commit / 工作区摘要确定。
- 各 case 的 repo、原始 base commit/tree、task hash、镜像 digest/ID、validator hash，以及本地 workspace_base_commit。

Validator hash 覆盖定义及其引用的测试资产。无需 dataset row hash、suite lock、网络策略版本等并行身份体系。不保存 API key 或整份用户配置。

每个 `result.json` 使用相同 schema：

| 字段 | 内容 |
| --- | --- |
| `schema_version`、`evaluation_run_id`、`case_id` | 格式及运行身份 |
| `status` | `PASS / FAIL / ERROR / ABORTED / NOT_RUN` |
| `agent` | outcome、reason、session_id、run_id |
| `validation` | status、固定检查结果、退出码、简短原因 |
| `metrics` | Profile 纯数据导出；未运行时标明不可用 |
| `timing` | prepare / agent / validation / total |
| `provenance` | 本 case 的最小身份及运行 manifest 引用 |
| `artifacts` | patch、session、validator log 的相对路径 |
| `diagnostics` | phase、category、reason |

正确性与执行状态分开。例如 Agent `limited` 但候选 patch 独立验证通过，可记 `PASS`，同时保留 `agent_limited` 诊断。验证未完成不能记为代码修复 `FAIL`；隔离或准备不可信不能计入 PASS。

诊断类别仅保留：`setup_error / agent_runtime_error / agent_limited / agent_aborted / validation_failed / validation_error`。用 phase 和明确 reason 表达具体错误，不自动判断根因。

```text
results/<evaluation-run-id>/
    manifest.json
    summary.json
    report.txt
    <case-id>/
        result.json
        patch.diff
        session/          # 现有 SessionLog 实际布局，仅一份 JSONL 轨迹
        validator.log
```

通过现有 `Conversation(home=...)` 将 SessionLog 放入 case 结果目录，记录实际 writer 路径；不改 JSONL 格式，也不另存重复 trajectory。测试框架原始结果文件仅在支持判定或诊断时保留，准备/错误日志同理。

每个 case 完成即写入结果并刷新 summary，不能等八个全结束才落盘。准备失败时不伪造 patch、轨迹或验证日志；结果中明确缺失原因。

Before/After 按 case_id 对齐 JSON。case、task、镜像、validator、模型及预算必须一致；Nexus 实现身份和 Context policy 可以作为预期变化项。其它条件变化要单独标注，不能混成同条件实验。

## 10. CLI

V0 只有两个必需入口。按当前本地启动方式：

```powershell
# 运行一个固定 case
.\.venv\Scripts\nexus.exe eval pvlib__pvlib-python-1707

# 按固定顺序运行全部 8 个
.\.venv\Scripts\nexus.exe eval --all
```

上述接口已实现；实际验收及基线结果见 [Evaluation V0 记录](evaluation-v0-evidence.md)。

使用现有用户模型配置；仅在内存中的评测配置禁用 MCP，并声明容器工具环境。无需额外 `--profile`，评测默认收集 Profile。开发版从 Nexus checkout 加载固定 case 数据；打包分发独立评测资产暂不扩展。

未知 case、缺少选择或同时给 case 与 `--all` 时给简短错误，不启动模型。CLI 在普通交互 TUI 初始化之前分派 eval，不启动交互输入循环。

普通 case 失败后继续下一个；共享 Docker 设施或隔离条件失效时停止后续执行。Ctrl+C 保存已有证据，当前 case 标记中断、未开始项标 `NOT_RUN`，有界清理后退出。不自动 resume。

建议退出码：全数 PASS 为 0；存在 FAIL 为 1；存在 ERROR 或非用户中断导致的 NOT_RUN 为 2；用户中断为 130。退出码只作命令级结果，详细事实看 JSON。

## 11. Aggregate report

`eval --all` 必须输出一份八行报告。每行链接到 case artifacts；单 case 模式使用同一格式的一行版本。一级表格包含 `FinalCtx`（最后一次普通模型输入）和 `ToolResultBytes`（工具结果累计字节数），缺失数据保持 unknown。下例仅演示布局，不是本次测量数据：

```text
Next Dev Set V0
Cases 8   Passed 6/8   Failed 2   Errors 0   Aborted 0   Not run 0
Input >=3.2M   Output ...   Usage coverage ...   Model calls 147
Tool calls 236   Agent time 31m

Case          Result   Input   PeakCtx  FinalCtx  ToolResultBytes  Tools  Time  Artifacts
pvlib         PASS     521k    43.8k    ...       ...              31     209s  <case-dir>
matplotlib    PASS     ...     ...      ...       ...              ...    ...   <case-dir>
django        FAIL     ...     ...      ...       ...              ...    ...   <case-dir>
requests      PASS     ...     ...      ...       ...              ...    ...   <case-dir>
sphinx        PASS     ...     ...      ...       ...              ...    ...   <case-dir>
click         FAIL     ...     ...      ...       ...              ...    ...   <case-dir>
pytest        PASS     ...     ...      ...       ...              ...    ...   <case-dir>
rich          PASS     ...     ...      ...       ...              ...    ...   <case-dir>

Failures
  django: validation_failed — <简短原因；result / trajectory / validator 路径>
  click:  validation_failed — <简短原因；result / trajectory / validator 路径>
Artifacts: <evaluation-run-directory>
```

`report.txt` 与终端使用同一份摘要数据，`summary.json` 保存机器可读的八行及合计。PASS 数以本次选中案例数为分母；ERROR、ABORTED、NOT_RUN 不隐藏。Agent 非正常结束但验证 PASS 的情况显示一个简短提示，避免只看 PASS 忽略执行问题。

Token 不完整时显示下界或 unknown，并保留 usage coverage；Agent 时间合计不含准备/验证耗时。不在这张表重复完整轨迹和全部 Profile 字段。

## 12. 框架测试与验收

框架测试使用 fake model / fake Docker 适配边界，不发真实模型请求：

| 边界 | 必需测试 |
| --- | --- |
| CLI / case | 两入口、互斥参数、未知 case、固定八项顺序 |
| 执行组合 | 注入工具实际生效，MCP 未连接，普通 Conversation 默认工具不变；错误路径不回退宿主机执行 |
| 工具环境 | 模型看到 Linux 和 /workspace；容器 cwd 不变成宿主机路径；输出与事件字段兼容 |
| 快照 / patch | 无上游 .git、remote；准确基线；Agent commit 后仍能收集修改及新增/删除 |
| 独立性 | 每次新 workspace / Session / 容器；验证不继承执行目录或依赖改动 |
| 验证 | PASS、真实检查失败、缺失/skip 目标、解析失败、测试覆盖规则；completed 不等于 PASS |
| Profile | 与现有聚合一致，usage 缺失不记 0、不伪造 peak，准备/验证不进入指标 |
| 报告 / 生命周期 | 八行汇总、结果逐 case 保存、错误后继续、Ctrl+C 保留证据及未运行项 |

实际 Docker 测试另验证：无网络、不可见控制面/密钥/隐藏资产、符号链接和文件属性、命令超时与取消后的真实进程清理。模拟测试不能替代这些实测。

每个 validator 做 base 负对照与 reference fix 正对照。然后运行项目 `pytest / ruff / mypy`，报告实际执行和未执行项。

最终按用户要求验收十点：

1. `nexus eval <case>` 能运行一个固定 case。
2. `nexus eval --all` 能串行运行八个。
3. 每次从干净、固定项目环境开始。
4. Agent 工具无法通过网络或上游历史检索答案。
5. Agent 执行期间不可见任务 validator。
6. 候选改动在全新环境独立验证。
7. 每个 case 附有现有 Profile 指标或明确的未运行原因。
8. 一份 aggregate report 展示八个结果与主要指标。
9. 失败 case 可以用轨迹、Profile 和 validator 日志人工 RCA。
10. Before/After 生成相同格式、可按 case 直接对照的 JSON。

## 13. 精确实施阶段

| 阶段 | 工作 | 完成条件 |
| --- | --- | --- |
| 1. 冻结八项数据 | 整理 case.toml、task、固定镜像及统一 validator；完成正负对照 | 八个任务定义完整；每个环境和 validator 有资格检查证据 |
| 2. 最小环境执行 | 源树快照、合成 commit、Docker executor、复用 apply_patch、候选 patch 收集 | fake model + 实际 Docker 证明隔离、路径及超时清理成立 |
| 3. 接入真实运行 | Conversation 工具注入与环境显示；单 case runner；Profile 导出和独立验证 | 一个真实 case 贯通，保存最小结果与证据 |
| 4. 八项汇总 | `eval --all`、错误/中断处理、八行 report、summary JSON | 八项串行运行，单项失败不丢失其它证据 |
| 5. 基线与对照准备 | 完整检查、V0.1.1 真实全套与可比较 JSON；V0.2.1 After 待实现 | 八项基线、条件差异和限制已记录；不伪造尚未实现的 After |

环境准备由内部函数承担，不先做额外 CLI。阶段中发现镜像或 validator 问题时，只解决对应固定 case 所需内容，不扩展通用环境平台。

## 14. 预计实现范围与触及文件

**Agent core semantics: unchanged。** 不改 run_turn、ContextBuilder、compaction、模型重试策略或普通工具决策。实际评测的命令执行位置改变，是工具组合层的变化。

| 位置 | 预计改动 |
| --- | --- |
| `src/nexus/evaluation/` | 5 个功能模块 + 空/轻量 `__init__.py`；约 800–1,200 行 Python，包含 Docker 适配、验证、结果及报告 |
| `src/nexus/app/bootstrap.py` | 可选工具注册表注入；传递工具环境展示参数；错误路径保持注入语义 |
| `src/nexus/core/context.py` | 仅 instructions 的 OS / workspace 展示覆盖，默认不变；不改上下文选择或压缩 |
| `src/nexus/app/profile.py` | 必要时增加纯数据导出函数 |
| `src/nexus/app/cli.py` | 两个 eval 选择入口与早期分派 |
| 上述现有运行时文件合计 | 约 70–140 行调整，优先保持默认运行路径不变 |
| 框架测试 | 约 4–6 个测试文件、400–700 行；Docker smoke 与真实验收单独标记 |
| case 数据 | 8 份 TOML + 8 份 task.md |
| validator 数据 | 8 份简短定义，按任务需要添加 test.patch / 固定回归资产 |
| curated 环境 | 3 组小型 Dockerfile 和必要依赖文件；五个来源环境只补必要准备材料 |

行数是成本估算，不是要求写满的指标；第三方测试 patch 和依赖清单不计入 Python 编排代码。主要工作应留在 evaluation 目录。若工具适配开始要求修改 Agent Loop、引入网关或额外常驻进程，应回到当前需求核对具体阻碍，不能顺势扩成新子系统。

已授权并实施上述范围；实际检查结果与未完成项以 [验收记录](evaluation-v0-evidence.md) 为准。

## 15. 尚待实测解决的风险

| 风险 | 本版处理与待确认事项 |
| --- | --- |
| Windows + Docker Desktop 文件语义 | 先用实际八个源树检查大小写、LF、可执行位及 symlink；源树不等价则该 case 不启动，报告具体差异 |
| Docker 超时 / Ctrl+C 清理 | 必须实测容器内子进程确实退出；宿主机进程结束不是充分证据 |
| 五个来源环境的可用性与污染 | 资格阶段核对依赖、镜像身份及预置材料，不能假定任意 SWE-bench image 都可直接给 Agent |
| 本地 validator 的覆盖 | 必须包含原任务完整目标及保留集合，并通过正负对照；结论限于这些固定检查，不声称官方评分 |
| Curated 检查细节 | 固定 Click 类型断言、pytest 同进程退出路径、Rich 窄于字形的预期；用 base/ref-fix 证据冻结 |
| 模型服务与抽样波动 | 固定模型配置和实验条件，保留 timestamp/usage coverage；单次 Before/After 不足以证明统计显著性 |
| 未提交 Nexus 修改 | commit + 实际运行源码/依赖摘要标识实现；避免两个 dirty 工作区误认成同一版本 |
| 时间与 token 数据缺失 | 沿用 Profile 的 unknown/下界语义，不能靠估算补齐为精确测量 |

这些风险通过具体 case 的准备和验收解决。V0 不增加 compaction 策略、tool observation lifecycle、自动 RCA 或通用 benchmark 平台。

**实施边界：按批准的五个阶段推进；不扩展为通用平台、网络网关、插件体系或并行调度。**
