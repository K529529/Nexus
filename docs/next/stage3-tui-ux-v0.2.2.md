# Stage 3 CLI / TUI UX V0.2.2

基线：`44e09c8f50a26db3b260d1fe1e7913148aa7f9ef`。继续 `codex/nexus-v0.3-stage3-harness-hardening`，工作区为 `D:\WorkSpace\releasePro\github\Nexus\Nexus-stage3`。

## 问题确认与修改

- 上版输入区只有顶部和左侧，是主动采用的开放式设计；发送后的“你”消息卡片是另一种完整边框组件，因此出现“发送前残缺、发送后完整、下一轮又残缺”的观感。这不是 Agent 状态丢失。本版恢复 prompt_toolkit 内置完整 Frame，删除开放式前缀构造。普通输入三行（含上下边框），Slash 菜单按需展开。保留细竖线光标、无定时重绘、普通字符原生坐标映射及 F2 粘贴降级。
- Thinking 标题与工具状态行均从第 0 列开始，取消两格额外缩进；正文竖线也在第 0 列。标题采用 `#ece9f4 italic`，正文 `#b8b3c4 italic`，竖线 `#82798e`，不再叠加 `dim`。流式预览与过程提示同步提高亮度。
- 最终 Markdown 正文显式设为 `#e5e7eb`，保留紫色标题、表格、代码块等既有渲染。
- 退出总结采用亮白粗体标题、分隔线、绿色字段标签、浅色数值；非零工具失败数用红色，零调用仍显示 N/A。统计、缺失 Usage 处理、模型调用和退出行为均未修改。
- 工具状态行及 Patch Diff Preview 未修改。用户消息紫色卡片保持不变。

## 验证

- 定向 pytest：**119 passed**，见 [focused.txt](evidence/stage3-tui-ux-v0.2.2/focused.txt)。
- 全量 **823 passed / 10 skipped（opt-in Docker），62.13 秒**；Ruff 通过，Mypy 99 source files 无错误。见 [pytest-full.txt](evidence/stage3-tui-ux-v0.2.2/pytest-full.txt)、[ruff.txt](evidence/stage3-tui-ux-v0.2.2/ruff.txt)、[mypy.txt](evidence/stage3-tui-ux-v0.2.2/mypy.txt)。首次 Ruff 检出一行样式文本超过 100 字符，换行后重新检查。
- 新增跨两次提示符的实际屏幕测试：首轮与第二轮都检查四个角、右边线、底边线和三行高度；原中文混排、组合字符、emoji、左右移动删除、粘贴块、终端缩窄、取消与 JSONL/Replay 回归继续通过。
- 新增总结样式测试，确认高亮生效、0 次调用仍 N/A、重复 render 不重复总结。
- [离线展示样例](evidence/stage3-tui-ux-v0.2.2/preview.html)及[生成脚本](evidence/stage3-tui-ux-v0.2.2/render_sample.py)：输入框来自真实 prompt_toolkit 管道渲染；对话及总结是合成数据，不是 PyCharm 截图或模型运行证据。

## 手动验收

在 PyCharm FilePilot 终端退出旧 Nexus，使用先前命令重新启动，保持 `PYTHONPATH` 指向 `Nexus-stage3\src`。

1. 首次启动即应显示完整紫色框。发送一条消息，任务结束回到下一轮，框的四边应同样完整。
2. 输入中文拼音并停留在候选阶段，确认光标及高度；选字后继续输入、移动、删除。管道测试不能证明 Windows IME 候选窗口兼容性，**本项仍需 Reworked 真实终端复测**。
3. 检查 Thinking 标题/竖线与工具行左端对齐，正文比上一版更亮；最终回答正文为正常亮度。
4. `/exit` 检查 Goodbye、Interaction Summary、Performance 标题与绿色标签/浅色数值。0 调用必须 N/A，退出总结只显示一次。

## 范围

代码仅修改 `app/input.py`、`app/tui.py`、`app/profile.py` 的输入/展示部分；测试为 `test_tui_polish.py`、`test_input_v02.py`、`test_cli_ux.py`。不修改 Agent Loop、Model、工具执行、Context/Memory/Compaction、Session 协议或 Eval；不启动付费模型/8 Case，不合并 V0.3。

本轮 2 个专用系统临时目录已检查路径与 reparse 属性后清理；原有工作区未跟踪文件保留，未修改权限。
