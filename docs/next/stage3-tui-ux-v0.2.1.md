# Stage 3 CLI / TUI UX V0.2.1

基线：`784cc6b46667f9ed680e00a0a6f509e2a463a0e7`。工作区：`D:\WorkSpace\releasePro\github\Nexus\Nexus-stage3`，继续现有 Stage 3 分支。

## 本轮变化

1. **输入区**：闭合矩形框改成紫色顶部横线 + 左侧输入标识的开放式框。普通单行输入占两行；顶部横线最多 100 列，并避开终端最后一列。取消位于编辑正文右侧、下方的闭合边框，减少输入时跨行返回绘制边框的布局需求。显式使用细竖线光标与事件驱动刷新（不定时重绘）。输入、发送、F2/F3/F4、Slash 补全和取消语义保持 V0.2。
2. **中文光标**：无粘贴原子时，显示处理器直接返回原始 fragments，普通中英文均由 prompt_toolkit 原生映射光标。含折叠块时保留既有单原子映射，新增中文混排及终端缩窄的坐标测试。没有用手工补空格或删除中文字符补偿光标。
3. **已发送的用户消息**：从与模型正文相似的 `You ›` 单行改成紫色圆角区块，标题为“你”，配低亮紫色底。短任务保留多行、缩进及字面 Markdown；长任务预览最多六个正文显示行，附原始行数/字符数，避免刷屏。仅展示截断，实际提交内容不变。窄屏、非交互或简化终端用“你 ›”及正文回退。
4. **Thinking**：较亮的柔和灰紫斜体标题、两格缩进、低亮紫灰竖线、灰色斜体正文和上下留白。仍仅对公开 Assistant Content 进行分类；私有 reasoning_content 不展示。工具状态行、Patch Diff Preview、Working 和最终 Markdown 的执行/分类行为没有改变。

## IME 证据与边界

用户确认：**尚未选字时漂移，并把输入框撑大**；截图显示 **Reworked 2025**。本机只读检查也确认运行 `D:\Tools\PyCharm 2026.2.3\bin\pycharm64.exe`，PyCharm 日志中 FilePilot 的终端启动记录明确标为 Reworked。没有修改 IDE 配置或切换引擎，未将整份 IDE 日志/环境变量复制到仓库。

这是候选合成阶段的问题，不能仅凭截图归因于已提交汉字的字符宽度。Python 进程没有获得可用于复现完整候选窗口的 IME composition 协议。本轮措施是减少闭合框的重绘/换行依赖、使用原生坐标映射及稳定的光标形状；**属于待真实终端验证的兼容性调整，不能宣布系统输入法问题已被确定修复**。

JetBrains 的公开问题记录也涉及[交互终端中文输入](https://youtrack.jetbrains.com/issue/IJPL-148371)与[Reworked 中文重叠](https://youtrack.jetbrains.com/issue/PY-80686)。它们仅作为排查线索，不证明本次截图与其同根，也不证明当前 IDE 版本仍有对应缺陷。

## 测试与样例

- 定向：**89 passed**，见 [focused.txt](evidence/stage3-tui-ux-v0.2.1/focused.txt)。
- 全量：**821 passed / 10 skipped，60.75 秒**；跳过的 10 项均为 opt-in Docker isolation acceptance。见 [pytest-full.txt](evidence/stage3-tui-ux-v0.2.1/pytest-full.txt)。
- Ruff：通过；Mypy：99 source files，无错误。原始输出在本轮 evidence 目录。
- 新增 13 个用例：汉字/中英/组合符/emoji 光标列、左移删除、无输入时不重绘、终端行数改变不撑高、中文与折叠块混排/缩窄、用户回显在 18/40/110 列和非交互模式的降级、Thinking 样式与工具行不变、私有字段不显示。
- 管道测试覆盖已提交字符及应用屏幕坐标，不模拟 Windows 输入法候选窗口。
- 首轮定向测试 **2 failed / 87 passed**：窄屏测试错误地要求一整段中文不被物理换行，修改断言后通过，原始失败见 [focused-first.txt](evidence/stage3-tui-ux-v0.2.1/focused-first.txt)。首次静态检查还发现一个未使用导入、CursorShape 参数需要配置对象、fragments 列表类型不变性，均已修正。
- [离线样例 HTML](evidence/stage3-tui-ux-v0.2.1/preview.html) / [文本](evidence/stage3-tui-ux-v0.2.1/preview.txt) / [生成脚本](evidence/stage3-tui-ux-v0.2.1/render_sample.py)。输入屏幕来自真实 prompt_toolkit 管道渲染；对话部分是合成展示数据，未请求模型或执行工具，不是 PyCharm 实测截图。

## 请在当前 Reworked 终端验收

保持现有终端引擎，关闭旧 Nexus 进程后重新启动。确认加载 Stage 3 源码：

```powershell
$env:PYTHONPATH = 'D:\WorkSpace\releasePro\github\Nexus\Nexus-stage3\src'
& 'D:\WorkSpace\releasePro\github\Nexus\Nexus-next\.venv\Scripts\python.exe' -m nexus --profile
```

可继续在 FilePilot 的工作目录启动；本轮没有修改 FilePilot。

1. 在空输入区切换中文输入法，输入 `nihao`，停留在候选阶段，翻页候选，再选字；检查光标和输入区高度。再试中文后输入英文、删除、左右移动和接近行尾的选字。
2. 粘贴长文本检查折叠、F3 展开；无 Bracketed Paste 时仍使用 F2 → 粘贴 → F2 → Enter。
3. 连续发送两条短消息和一条多行消息，向上滚动确认“你”的紫色区块容易找到，模型正文保持正常亮度。
4. 触发带工具的任务，检查 Thinking 和工具状态行之间的层次，以及最终回答没有重复打印。
5. Ctrl+C 清稿、运行中取消、空输入退出，确认提示符恢复。

如果候选阶段仍漂移，请保留新截图并说明是否在选字后恢复。先据此区分宿主 IME 问题与应用已提交字符的渲染问题，再决定下一步；本轮没有强制切换 Classic 或修改系统输入法。

## 修改范围

- `src/nexus/app/input.py`：开放式输入框、光标形状、普通字符映射直通。
- `src/nexus/app/tui.py`：用户消息回显和 Thinking 样式。
- `src/nexus/app/cli.py`：用统一展示方法替换旧单行用户回显。
- `tests/test_tui_polish.py`、`tests/test_cli_ux.py`、`tests/test_input_v02.py`：新增及适配展示测试。
- 本文及本轮离线证据。

Agent Loop、模型协议、Session JSONL、Context/Memory/Compaction、Patch、Eval 和工具状态行实现均未修改。没有运行付费模型或 8 Case，没有合并 V0.3。

本轮 3 个专用系统临时目录已核验后清理，未更改权限；原有未跟踪报告与 V0.1 证据保留。清理记录见本轮 evidence/cleanup.txt。
