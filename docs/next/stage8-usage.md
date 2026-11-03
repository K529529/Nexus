# V0.3 Stage8：原生协作与验收

适用分支：`codex/nexus-v0.3-stage8-multi-agent`。尚未合入 V0.3，也未发布新版包。
安装该分支源码后使用；PyPI 的 0.2.0 不包含本阶段功能。

## 配置

在已有 `~/.nexus/config.toml` 中增加下列配置。保持现有 `[model]`、沙箱和 API key
环境变量设置；本阶段不会自动修改个人配置。

```toml
[multi_agent]
enabled = true
allowNestedSubagents = false
max_agents = 3

[multi_agent.roles.scout]
reasoning_effort = "low"
mcp_servers = []

[multi_agent.roles.worker]
reasoning_effort = "high"
mcp_servers = []

[multi_agent.roles.reviewer]
reasoning_effort = "high"
mcp_servers = []
```

`enabled` 默认 false，关闭时不增加委派提示或原生协作工具。运行中输入仍然可用。
`max_agents` 是整棵树中活跃子/孙代理总额度，不包含主代理；等待决策和取消清理仍占额度。
每个角色可选 `model = "已配置的 models 别名"`，省略时继承创建者当前模型；
`model = "default"` 使用启动时默认模型。模型及推理强度在创建时固定，恢复保留快照。

`mcp_servers` 填根会话已配置的服务名。子代理只拿父权限与角色名单的交集，默认无 MCP。
同一服务的调用串行，不同服务可并行；连接只由根会话持有。Skills 目录可见性继承，
加载状态和上下文彼此独立。自定义工具环境必须提供按 Session 创建的 registry 工厂，
不能把绑定主会话的工具字典直接复用给子代理。

打开 `allowNestedSubagents` 后允许主 → 子 → 孙，最多两层。worker 可委派任意内置角色，
scout 只能委派 scout，reviewer 可委派 scout/reviewer。scout/reviewer 不提供 apply_patch；
其只读职责是协作约定，Shell 并非只读安全隔离。实际执行权限仍由现有沙箱决定。

## 运行中输入与代理查看

| 操作 | 行为 |
| --- | --- |
| 空闲 Enter / Tab | 提交新任务 / 原有补全 |
| 执行中 Enter | 指导当前任务，在完整工具批次后的安全边界交给模型 |
| 执行中 Tab | 排队为根代理的后续任务 |
| `/pending` | 查看当前选中代理尚未投递的消息及 ID |
| `/edit ID 新内容`、`/withdraw ID` | 修改、撤回未投递输入 |
| F6 / `/agents` | 打开当前任务及跨轮待处理代理选择器；↑↓ 选择，Enter 进入会话 |
| 选择器 H / `/agents history` | 已归档的历史任务代理；名称、ID 和详情入口仍保留 |
| `/agents ID`、`/agent ID` | 进入子代理独立会话视图；ID 支持唯一前缀 |
| Esc / `/agent root` | 关闭选择器，或从子视图回到主代理；各自草稿保留 |
| PgUp / PgDn / Ctrl+End | 阅读当前视图历史 / 翻页 / 回到最新；新输出不抢回阅读位置 |
| `/agent detail ID` | 按需查看该代理独立轨迹 |
| `/agent cancel ID` | 取消该代理及后代，并等待工具清理 |

补全菜单优先于排队快捷键；现有多行、粘贴、审批交互保留。已收到与已投递分开显示。
开始投递后不再接受编辑或撤回；排队内容按任务文本执行，即使以 `/` 开头也不执行空闲命令。
选中子代理后 Enter 只指导它，同时通知其父代理；Tab 仍排到主代理。审批标明所属代理，
全树审批串行，审批结束保留输入草稿。子代理详细输出不混入主区域。
完成后的子代理会话为只读，不会因 Enter 自动复活或误把文字发成主代理新任务。
运行中的输入框持续存在；输入消息不重启整个输入应用。状态未变时不定时刷新，
流式预览合并更新，编辑草稿时不由模型 token 驱动重绘。
代理树只占紧凑区域，完整名单在选择器中滚动；累计用量仍是主代理和全部子代理的
会话总计（包括历史任务），不是本轮用量。实现和证据见 [完整交互改版](stage8-interaction.md)。

模型通过原生 `spawn_agent`、`wait_agents`、`list_agents`、`message_agent`、
`report_parent`、`cancel_agent`、`resume_agent` 协作，不需要用户手写工具调用。
创建立即返回，等待超时不会取消。子代理 Fresh 启动，只接收委派目标、背景和范围，
不复制父对话。所有代理共享工作区，父代理应分配互不重叠的文件；范围约定不是文件锁。
主任务结束前收回结果或取消剩余代理，不保留无人管理的后台任务。

## 恢复、历史与统计

根 JSONL 下的 `<session_id>/subagents/<agent_id>.jsonl` 保存每个实例的独立日志。
恢复根会话会重建代理关系及中断状态，不重建旧 asyncio Task，也不自动恢复整棵树。
父代理可显式调用 `resume_agent`，使用该子代理自己的历史继续。历史一次性批准不复用。
未知 Shell 副作用仍需核实，不能通过新建子代理绕过；恢复不会证明旧 OS 进程已经退出。
`cleanup_failed` 不释放额度或宣称文件范围已交回。

本地八项评测仍生成原有 `history.html` 和 `report.html`。实际委派时增加代理树、
按代理筛选、共同时间轴上的并行操作及通信轨迹，未委派和旧记录不出现空面板。
报告保留各子代理模型/推理强度；开启能力与实际使用委派分别记录。
全树 token 按各代理实际请求汇总，包括重试、失败和压缩；缺失为 unknown，
缓存是输入的子集，不再次加到总 token。总耗时不叠加并行子代理时长，费用默认 unknown。
评测中的 context 指标仍为根代理，`context_scope` 明确标注。

遥测沿用现有配置，由根导出全树；不新增上传消息正文、提示词、源码或工具完整输出。
执行 ID、父子关系、调用关联、状态、时间、实际用量及通信方向/决策标志进入元数据。
恢复创建新的执行轨迹并关联原任务，不伪造停机期间的运行时间；未完成 span 不捏造结束时间。
导出失败不影响本地结果，可继续用现有 `nexus trace export` 重试。

## 人工体验与 ABK 对照

本次开发不运行 ABK，由 Archer 手动运行。先在临时测试项目完成四组真实体验：

1. 单 Agent 运行长命令时输入指导和后续任务：指导不破坏工具配对，原任务继续，后续任务排队。
2. 让子代理跑测试，主代理同时处理另一文件：确认并行推进，查看树与独立轨迹。
3. 让两个 worker 分别修改不同文件，主代理收回结果并运行集成测试。
4. 子代理求助、选中指导、取消真实命令、退出后恢复根会话，再显式恢复一个中断子代理。

Windows 终端重点观察中文 IME、多行粘贴、缩放、审批、草稿保留和 Ctrl+C。
确定性替身模型测试通过不等于这四组真实模型体验已经验收。

对照运行使用相同代码基线、任务、环境及主模型参数，仅切换 `multi_agent.enabled`；
保存两次 manifest/summary、history/report、JSONL 和失败证据。比较通过率、根任务实际耗时、
全树 token 与失败类型，记录子代理实际模型/推理强度及实际委派情况；无价格则费用未知。
多 Agent 是否更快、更省需由对照结果决定。

Windows 很长的临时/日志路径可能超过系统文件名限制；使用较短测试父目录。
平台测试结果、初始失败及修复记录见 [阶段进度](stage8-progress.md)。
