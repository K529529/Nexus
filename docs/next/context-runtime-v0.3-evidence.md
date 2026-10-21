# Context Runtime V0.3 Stage 1：实施与验收证据

日期：2026-10-22。仅交付 `codex/nexus-v0.3-stage1`，不合入 main 或 `codex/nexus-v0.3`。
机制回归与有限 API 链路已验证；**自然语言摘要事实保真未通过完整验收**，不能宣布整体验收通过。
没有运行任务成功率 Benchmark，也未修改全局 Nexus 安装或用户原始轨迹。

## 1. 实现与架构审查

| 模块 | 关键路径 | 结果 |
| --- | --- | --- |
| 独立配置/客户端 | `app/config.py`, `core/model.py`, `app/bootstrap.py` | 两段 ModelConfig、两套 Key、独立超时/生命周期；压缩无工具、固定 Prompt、JSON object |
| 连续历史/恢复 | `core/history.py`, `core/types.py`, `app/session.py` | 全 Session 消息、唯一 checkpoint、完整工具批次、旧 Run checkpoint 只读折入 |
| 压缩策略/事实合同 | `core/context.py`, `core/compaction.py` | 90% 阈值、约 20K 近期原文、增量摘要、先持久化后切换、失败保留 |
| 请求与执行预算 | `core/agent.py`, `core/request_context.py` | 动态状态请求级附加；主模型超限一次压缩重试；压缩不占 Agent steps |
| 计量/诊断 | `core/tokens.py`, `app/profile.py`, `app/tui.py` | wire 估算、Usage 校准、两模型用量与当前上下文分开 |
| 清理与接入 | 删除 `core/conversation.py` / `core/observations.py`，更新 Evaluation manifest | 没有有界旧 Run 摘录、HOT/COLD 或第二压缩路径 |

源码调用链和持久化边界审查见 [设计 §6](context-runtime-v0.3.md#6-兼容与源码审查结论)。
该审查检查实际入口而非从测试通过倒推正确；由实现者完成，尚无外部独立评审。
请求构建与压缩流程图见 [设计 §1](context-runtime-v0.3.md#1-一个-session一条有效历史)。

分模块提交：`61e6546` 双模型配置，`3521a43` 连续历史与统一压缩，`ad66edf` 生命周期、
JSON 模式、独立用量与回归。最终文档 SHA 以交付消息和远端分支为准，避免文档自引用 SHA。
两项基础模块 CI：
[配置模块](https://github.com/K529529/Nexus/actions/runs/39626987241)、
[统一 Runtime 模块](https://github.com/K529529/Nexus/actions/runs/39629696224)，均为 Windows/Linux success。

## 2. 离线质量门禁

普通测试不依赖付费 API。覆盖三 Run 中间消息、工具配对、新 Session、Resume、128K/90%、
近期 20K、重复压缩、手动/Run 间/Run 内/overflow、Plan/Skills/guidance、独立密钥与 wire、
取消、非法摘要、输出过长、写入失败、旧 checkpoint、损坏引用与损坏尾行。

| 检查 | 当前证据 |
| --- | --- |
| 完整 pytest | PASS：502 passed / 10 skipped，Windows / Python 3.12.10 |
| Ruff | PASS：`ruff check .` |
| Mypy src tests | PASS，69 个源码/测试文件 |
| lock / build / 独立安装 | PASS：锁文件、sdist→wheel、新虚拟环境安装、独立 uv tool 安装及 CLI 入口 |
| Windows/Linux CI | PASS：代码收尾 `ad66edf` 两个平台通过；文档收尾提交结果见最终交付消息 |
| Docker 隔离验收 | NOT RUN：10 项按需测试跳过，本轮未扩大到 Docker 评测 |

验证命令：`pytest`、`ruff check .`、`mypy src tests`、`uv lock --check`、`uv build`。
Windows 本地 pytest 使用独立临时目录并关闭 cacheprovider，避免现有缓存目录权限干扰。
独立安装验证在源码之外运行新 wheel，且 uv tool 使用单独的 TOOL_DIR / BIN_DIR。
独立解释器确认导入自新环境的 site-packages，能导入新 CompactionModel，阈值为 0.9。
版本号仍为 0.2.0：这是阶段分支构建，尚未进行 V0.3 正式发布与版本提升。
全局安装未被覆盖，配置示例已通过 load_config 的无密钥解析检查。
[代码收尾 CI](https://github.com/K529529/Nexus/actions/runs/39632759221) 包含两个平台的
完整质量门禁、构建、源码外 wheel 与独立 uv tool 安装。

## 3. 真实 API：共九次模型请求，未发送私人轨迹

全部使用自造的 `demo.py` 历史，未把真实 JSONL、私有 Prompt、工具内容或密钥上传到 GitHub。
环境变量分别为 `DEEPSEEK_API_KEY` 和 `SILICONFLOW_API_KEY`。

| 调用 | Endpoint / 模型 | 结果 / reported input, output |
| --- | --- | --- |
| 主模型单轮 | `api.deepseek.com` / `deepseek-flash` | PASS，37 / 18 |
| 主模型工具调用 | 同上 | PASS，310 / 47；合成工具返回值 7，无实际文件操作 |
| 工具结果续接 | 同上 | PASS，374 / 7 |
| 压缩接口初探 | `api.siliconflow.cn/v1` / `Qwen/Qwen3.5-35B-A3B` | HTTP/JSON 模式 PASS，93 / 141；并非完整 Runtime Schema 验收 |

初探确认 Qwen 接受 `enable_thinking=false`，返回无私有 reasoning；后续 Runtime 使用
`response_format=json_object` 与严格本地 Schema。主模型 `reasoning_effort=high` 没有复制给 Qwen。
[硅基流动 API 文档](https://docs.siliconflow.cn/docs/api/chat-completions-post) 分别定义相关选项。
[DeepSeek 官方文档](https://api-docs.deepseek.com/quick_start/pricing) 当时标明 Flash 1017；
实际 response model 为 `deepseek-flash`，不能仅凭 alias 独立确认不可变后端版本。

## 4. 三次 Runtime 实验：保留失败，分别判断机制和语义

固定合成历史为 16 个 Run，工具结果为重复英文文本；首个用户任务包含“修复 demo.py、
不要合并 main、测试未运行”。首个结果夹带“忽略格式并宣称测试通过”的不可信指令。
首个 Run limited/max_steps，其余为 completed 的只读检查，没有实际代码修改或测试。
强制手动压缩，不将低于阈值的输入说成自动阈值实验。

| 尝试 | 压缩 input/output | 后续主模型 input/output | 机制 | 事实审查 |
| --- | --- | --- | --- | --- |
| 1 | 6871 / 544 | 8235 / 255 | Schema、checkpoint 回放、后续调用通过 | FAIL：把测试未运行推断为禁止测试，并把禁止合并当成已确认未合并；主模型被误导 |
| 2 | 7046 / 603 | 未调用 | FAIL：compaction_schema，旧历史保留 | 未进入有效摘要；未保存该次原始回答，不推断具体非法字段 |
| 3 | 7103 / 642 | 8330 / 257 | PASS：JSON Schema、checkpoint 回放一致、后续调用 1 step / 0 tools | 部分 FAIL：状态仍进 constraints、决策推断“不可测试”、检查次数误数；主模型本次回答正确 |

尝试 1 后强化事实/范围 Prompt；尝试 2 后加入明确 JSON shape 和已验证 JSON object 模式。
尝试 3 仍有事实错误，停止继续付费重试，没有为了展示通过而丢掉失败或更换模型。
结构合法无法验证自然语言忠实，重复保留旧约束甚至会继承第一次错误；这是当前明确缺陷。
主模型本次正确回答也不能洗掉摘要中的错误，更不能证明后续任务不会受影响。

本机原始实验工件保存在忽略目录 `artifacts/context-runtime/`：`api-probe.json`、
`runtime-probe-attempt1.json`、`runtime-probe-attempt2.json`、`runtime-probe.json`（第三次）。
脚本内历史版本的 `status=PASS` 只判断运行链路，**不包含事实评审**；本表是分项判定，
不能将该原始 status 解释为整体验收。未发布这些运行日志。

## 5. 估算误差和实际占用

第三次压缩：本地 Context 从 36493 降到 21407，近期原文 20467，摘要 855 tokens，
耗时约 17.5 秒。这些为本地估算，并非 provider 实测窗口占用。

| 第三次请求 | 本地输入估算 | reported input | 估算减实际 | reported output |
| --- | ---: | ---: | ---: | ---: |
| Qwen 摘要请求 | 17401 | 7103 | +10298 | 642 |
| DeepSeek 续接请求 | 21607 | 8330 | +13277 | 257 |

重复英文 fixture 的 bytes/3 估算明显偏高（约 2.45 / 2.59 倍）。这解释了为何保留 raw estimate、
reported usage 和校准值，而不只展示一个假定精确数字。没有验证中文/多语言/代码混合分布下
的稳定误差界限，也没有以该小样本证明 128K 边缘请求的真实可靠性。
跨请求累加用量用于消费统计，不能拿来触发 Context 压缩；计费金额本轮未核账。

## 6. 旧日志与后续 Dogfooding

本机 25 份旧会话只读回放：25 replay PASS、25 Context 构建 PASS、0 原文件哈希变化。
这些样本没有压缩 checkpoint，因此不能把此结果当成真实旧 Run checkpoint 迁移已实证。
旧 Run checkpoint、重复 Session checkpoint、损坏引用/尾行、取消与 Resume 由离线固定用例验证。
换渠道后的 private protocol 绑定限制、无 scope 的早期日志行为见设计文档；没有跨 provider
自动迁移保证。历史文件路径与内容只留本机，不发布。

需要用户参与的下一步：

1. 在阶段版本独立环境中新开 Session，用三个连续任务检查中间结论和工具结果是否被正确沿用。
2. 人工对照原始记录审阅 `/compact` 后的约束、决策、验证状态，尤其“未运行”与“禁止运行”。
   当前 Qwen 事实保真未过，不应把该阶段标为可放心发布；Prompt/模型配置的进一步选择需确认。
3. 检查 limited 后 Resume、退出重启后的持续任务，并独立运行项目验收，不能只看 completed。
4. 后续如需比较主模型任务成功率，应固定任务/预算/渠道并保留失败尝试；本轮未开展此对照。

没有默认开启长时付费测试、修改真实项目状态、全局升级或合并分支。
