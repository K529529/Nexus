> 历史阶段记录：以下状态、实验与失败保留当时原貌。当前合入/发布状态见
> [0.3.0 发布准备记录](release-0.3.0-validation.md)，当前使用见 [手册](usage-guide.zh-CN.md)。

# Stage8 完整交互改版

2026-11-04，开发分支 `codex/nexus-v0.3-stage8-multi-agent`，起点 `a6f8cdb`。
根据用户在同一 PyCharm PowerShell 中与 Pi 对比的反馈，这次按完整使用流程改版。

## 交互

- 一个 prompt_toolkit 视口包含会话标题、可滚动内容、紧凑代理树、提示和输入框。
  Rich 只负责生成格式化内容，不再与输入应用分别抢占终端输出。
- F6 或 `/agents` 打开代理选择器，方向键选择，Enter 进入，Esc 返回主代理。
  `/agents ID` 与 `/agent ID` 等价，唯一短 ID 可直接进入。选择器 H 切换历史。
- 主代理与子代理分别展示自己的对话、工具状态、输出和草稿。子代理展示任务名、角色、
  短 ID、状态与是否可指导；已完成代理只读，Enter 给出明确说明，不新建/恢复代理。
- 运行中 Enter 指导所选代理，Tab 排队到主代理；同一个输入应用持续运行。
  提交时固定文字、模式、目标，异步接收串行处理，审批/收尾只暂停读取输入，
  已提交内容继续交付一次。持久化或内部错误仍传播，不通过宽泛捕获吞掉错误。
- PgUp/PgDn 阅读历史，Ctrl+End 回到最新；新输出不把阅读位置拉到底部。
  窄窗口优先保留编辑器，缩短快捷键提示，短窗口收起代理摘要，F6 仍可查看完整名单。
- 未改变状态不发起定时重绘；输出更新合并，已格式化内容缓存。模型预览更新时，
  草稿非空便不主动触发重绘；用户编辑、完成消息和关键状态变化仍能更新画面。
- 延续当前根任务分组，跨轮仍需处理的代理保留，已完成无待办代理归档。摘要明确标为
  “会话累计（主+全部子代理）”，历史执行算在其中，不把它显示成本轮用量。
- Ctrl+C 仍取消本轮，子树单独取消仍使用 `/agent cancel ID`。多行、粘贴、审批、
  恢复、角色、额度、Fresh 上下文和共享工作区不变；没有第二套 Agent Loop。

界面仅缓存每个视图最近 400 个输出块，完整轨迹继续保存在原 JSONL 中。
当前活动会话首次进入时从自身消息加载，恢复实例从其独立日志加载。
退出时主代理已提交内容打印回普通终端滚动历史。

## 验证

新增真实 prompt_toolkit 输入管道回归覆盖：

- 普通/Reworked 终端适配中的持续输入应用、主子视图切换、独立草稿、审批返回，
  Enter 子代理指导、Tab 主代理排队、中文多行粘贴与实际 Inbox 内容。
- 无变化时终端字节不变；编辑草稿时预览不会发起重绘；实际历史画面滚动和新输出不抢位；
  10×40、8×25、24×90 窗口内输入框、光标和草稿保持可用。
- 提交回调被异步屏障阻塞时仍能继续编辑/排队，暂停后稿件保留，消息不丢不重；
  接收写入错误会显式传播。
- 真实 CLI → 两个真实 Subagents → 原 Loop/Inbox：进入子代理，处理错误命令，
  指导进入模型请求，排队在主任务后执行，完成后拒绝指导，返回主代理，`/new` 清空视图，退出。
  仅模型网络调用替换成确定性回复/异步屏障，不调用真实模型或 ABK。

专项 **181 passed**（69.04 秒）：[原始输出](evidence/stage8-interaction/targeted-pytest.txt)。
随后新增真实界面 Ctrl+C 取消和新会话清屏断言，界面/模型菜单/MCP TUI/Conversation
组合 **72 passed**（27.23 秒）；最终粘贴历史修复后新增界面集合 **6 passed**。
[最终 UI 组合](evidence/stage8-interaction/final-ui-pytest.txt)。

最终代码 Windows 全量 **1244 passed / 10 skipped**（166.96 秒），16 条既有第三方
LiteLLM/Pydantic 弃用警告，跳过项为显式 opt-in Docker 验收。
[全量原始输出](evidence/stage8-interaction/windows-pytest.txt)。

Ruff、mypy（150 文件）、uv lock（102 packages）、wheel/sdist 构建通过。
专用系统临时环境按锁定依赖离线安装 wheel，在源码外校验 screen/input/cli 模块
来自 site-packages 且与最终源码哈希相同，再运行五帧离线渲染及 CLI version。
[安装检查输出](evidence/stage8-interaction/package-smoke.txt)。wheel SHA256：
`0da46792322e6726c611c795dae7632d5b77ffb99ca721ae67b5f692810f85ad`。
版本号仍为 0.2.0，未发布或更改用户全局安装。

开发中发现并修复：PgUp 只改变光标而未正确翻动画面、空闲切换命令误恢复为草稿、
子视图命令反馈隐藏在主视图、折叠粘贴在持续输入时保存历史的时机。
旧会话生命周期测试替身缺少 session ID，改用真实 Session，
保留关闭/恢复断言。首轮专项 180 passed / 1 failed，失败输出保存在
[首次失败](evidence/stage8-interaction/targeted-initial-failure.txt)，不能记为通过。
全量首轮 **1238 passed / 5 failed / 10 skipped**：另外五个失败也来自恢复/模型菜单
旧替身缺少会话 ID，改用真实 Session 并保留全部原断言，见
[全量首次失败](evidence/stage8-interaction/windows-initial-failure.txt)。
这些失败不是用户提供的 Linux/SOCKS/mypy 既有问题，未更改用户代理配置。
开发中的类型/行长及离线图片导出参数问题修正后复验。

## 视觉证据与使用

本机 `Get-Command nexus` 指向 `D:\Tools\python\py312\Scripts\nexus.exe`，
其模块来自该全局 Python 的 site-packages，未包含本次 ConversationScreen。
直接输入 `nexus` 不会自动使用本分支代码。在目标工作区的 PowerShell 中明确运行：

```powershell
& 'D:\WorkSpace\releasePro\github\Nexus\Nexus-next\.venv\Scripts\nexus.exe'
```

该入口已核对为导入本仓库源码，当前目录仍是用户要操作的项目；未改 PATH 或全局安装。

`.venv\Scripts\python.exe scripts\stage8_ui_smoke.py` 提供无模型、无网络、无配置写入的
交互演示。显示数据为夹具，不代表真实代理执行；真实执行由上面的端到端测试覆盖。

使用 `--snapshot docs/next/evidence/stage8-interaction/view.html` 导出实际渲染单元格，
生成主视图、选择器、子视图、完成只读、窄窗口五帧 HTML 与 JSON。
PNG 是由这些单元格离线绘制，非 PyCharm 原生截图：
[主视图](evidence/stage8-interaction/view-root.png)、
[选择器](evidence/stage8-interaction/view-picker.png)、
[子视图](evidence/stage8-interaction/view-child.png)、
[完成只读](evidence/stage8-interaction/view-completed.png)、
[窄窗口](evidence/stage8-interaction/view-narrow.png)。

未验证：PyCharm 原生宿主的人工手感/微软拼音候选窗、Linux/macOS、真实多模型协作、
真实外部遥测、ABK/本地 8-case。ABK 由用户手动运行。自动化和离线帧不代替宿主体验验收。
不合并 V0.3/main，不发布，不改用户代理或模型配置。完成后等待复审与人工体验验收。

收尾未产生仓库根目录 pytest 临时文件。独立安装环境与可访问的失败测试目录已清理；
三个本轮系统临时测试目录因访问拒绝保留，未改权限，具体路径见
[临时清理记录](evidence/stage8-interaction/temporary-cleanup.txt)。失败文本仅清除行尾空白，
保留堆栈、断言和失败计数。原有 `.idea` 与 Stage4 PoC 未跟踪文件保持不动。
