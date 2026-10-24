# Stage 3 CLI / TUI UX V0.2 交付说明

- 分支：`codex/nexus-v0.3-stage3-harness-hardening`
- 基线：`dd900a81ff0a2223df3f1e83662c23437cfeb8c7`（UX V0.1）
- 工作区：`D:\WorkSpace\releasePro\github\Nexus\Nexus-stage3`
- 本轮仅输入控制、命令补全、Renderer 与相关测试；未改 Agent Loop、Model Adapter、Memory 实现、Patch、Eval、Context、Compaction。
- 不运行付费模型、8 Case 或 FilePilot 开发，不合并 V0.3。

## 体验与实现

### 输入和粘贴

输入使用 prompt_toolkit 内置 Frame，紫色 `#a78bfa`，普通单行草稿只占三行（含边框），多行可滚动。原 `You ›` 输入提示改为 `Nexus ›`；提交后的用户消息展示继续沿用既有逻辑。

Enter 直接发送。标准 Bracketed Paste 的正文不会触发提交；不少于 4 行或 500 字符时折叠为 `[paste #N +M lines]`。折叠只发生在输入编辑器内：正文保存在内存，发送时展开为完整原文，历史记录也保存原文。保留段落、缩进、空行、Unicode 和首尾空白；CRLF/CR 归一为逻辑 LF。

用户已确认 **F2 安全粘贴降级**：不提供 Bracketed Paste 的终端需在粘贴前按 F2，结束后再按 F2，最后 Enter 发送。F2 捕获期间的换行，包括分批慢速到达的文本，都只加入同一个草稿。F2 本身不提交。没有采用“输入速度像粘贴”的推断。

粘贴块在缓冲区是一个原子，可一次删除、撤销恢复、F3 展开编辑。占位标签不会用于字符串全局替换；字面输入同名标签、内部字符也不会错误展开。内置剪贴板和历史保存完整内容。Ctrl+C 清除草稿时，块正文和待处理输入一起清除；执行期间输入隔离沿用 V0.1。

### Slash 补全

输入 `/` 自动弹出当前已有命令列表，继续输入过滤，↑/↓ 选择。菜单打开时第一次 Enter 只选中候选，第二次 Enter 执行命令；完整手输 `/exit` 等命令可直接 Enter。只有 Slash 输入预留菜单高度，普通草稿不留大块空白。

包含 `/help`、`/new`、`/resume`、`/compact`、`/memory` 及 on/off/status、`/skills`、`/skill` 及 off、`/exit`。需要具体参数的命令仍沿用现有语义；不新增命令或自动执行。

### 输出

- 模型开始和流式等待使用紫色点状 `Working...`，最高 8 次/秒刷新；临时正文有长度及行数限制。
- 提交的公开 Assistant Content 如带 Tool Call，继续显示灰色斜体 `Thinking` 与左侧竖线；不读取或展示私有 reasoning_content。
- 最终内容使用 Rich Markdown，紫色标题与链接，代码块高亮；支持表格、列表、引用、等宽目录树。
- 临时 Live 区域在工具开始、正式消息、完成或中断时停止并清除，保留独立工具状态行及已有 patch_diff 预览。非交互/简化终端沿用普通文本降级。
- 不新增模型消息或公共日志字段，不改 JSONL、Resume、Replay、Profile、退出总结的数据契约。

## 快捷键

| 按键 | 行为 |
| --- | --- |
| Enter | 发送；Slash 菜单打开时先选择候选 |
| Ctrl+J / F4 | 手动换行 |
| F2 | 开始/结束安全粘贴捕获，不发送 |
| F3 | 展开光标所在或紧邻左侧的粘贴块以编辑 |
| Backspace / Delete | 普通编辑时删除字符；折叠块作为整体删除 |
| Ctrl+_ | 普通编辑时撤销；F2 捕获中禁用以免移除正在接收的块 |
| ↑ / ↓ | 菜单打开时选择候选；普通输入按现有历史/光标行为 |
| Ctrl+C | 非空输入清稿；运行中取消；回到空输入后再次按下退出 |
| /exit / EOF | 沿用统一安全退出和总结 |

无需 Esc+Enter。对于终端编码无法区分的组合键，以 Ctrl+J/F4 的实际表现为准。

## 验证结果与证据

- 最终定向回归：**157 passed**，见 [focused-final.txt](evidence/stage3-tui-ux-v0.2/focused-final.txt)。
- 最终全量 pytest：**808 passed / 10 skipped，59.28 秒**；10 项均为 opt-in Docker isolation acceptance。Ruff：**All checks passed**；Mypy：**98 source files，无错误**。见最终日志 [pytest-final.txt](evidence/stage3-tui-ux-v0.2/pytest-final.txt)、[ruff.txt](evidence/stage3-tui-ux-v0.2/ruff.txt)、[mypy.txt](evidence/stage3-tui-ux-v0.2/mypy.txt)。
- 新增 15 个输入/展示用例；既有多行中文 Markdown、单次提交对应一次 Run、JSONL/Replay、取消及残余输入、Memory 退出和 Markdown/Thinking 混合展示测试同步通过。
- 真正的 PipeInput + VT100 输出验证了 83 行粘贴只显示块标签、紫色框、紧凑高度与光标映射；命令过滤/选择、F2 慢速多批输入、Ctrl+C、删除/撤销/F3、剪贴板、跨提示符历史恢复均有确定性覆盖。
- 实际 Rich Live 对象验证动画帧变化、工具前停止与最终回复后清理；不把静态截图当动画证明。
- 第一次新增测试有 1 failed / 11 passed：测试误用了不存在的 `PromptSession.default_buffer_control`。改用公开 `app.layout.current_control` 后通过。原始失败保存在 [first-focused.txt](evidence/stage3-tui-ux-v0.2/first-focused.txt)，没有隐藏失败尝试。
- 离线样例：[preview.html](evidence/stage3-tui-ux-v0.2/preview.html)、[preview.txt](evidence/stage3-tui-ux-v0.2/preview.txt)。输入区来自真实 prompt_toolkit 渲染屏幕；Thinking/最终回答使用合成事件，仅证明展示，未执行模型或工具。可用 [render_sample.py](evidence/stage3-tui-ux-v0.2/render_sample.py) 重现。

## Windows / PyCharm 手动验收

真实 PyCharm Terminal、系统剪贴板和 IDE 快捷键拦截：**待用户手动验收**，本次未自动操作 IDE。

先确保加载 Stage 3 源码，避免共用虚拟环境的 editable install 指向另一个检出：

```powershell
Set-Location D:\WorkSpace\releasePro\github\Nexus\Nexus-stage3
$env:PYTHONPATH = 'D:\WorkSpace\releasePro\github\Nexus\Nexus-stage3\src'
& 'D:\WorkSpace\releasePro\github\Nexus\Nexus-next\.venv\Scripts\python.exe' -m nexus --profile
```

1. 输入 `/`，检查紫色候选高亮，输入 `/res` 过滤，上下选择，Enter 只补全。另试完整输入 `/help` 后 Enter。
2. **若不确定终端是否提供 Bracketed Paste，先用 F2。** 粘贴含代码块、中文、空行、缩进的长任务，再 F2 结束；检查 `[paste #1 +N lines]`，此时无模型/Run 启动。F3 展开检查全文，Ctrl+C 清稿。
3. 已确认提供 Bracketed Paste 的终端，可直接粘贴同一任务；确认折叠且未提交，Enter 后只产生一个 User Message 和一次 Run。可连续粘贴两个块再一次发送。
4. 普通输入 Enter 发送，Ctrl+J/F4 手动换行；试块删除与撤销、F3 展开编辑。F2 后可以分两次慢速粘贴，直到结束捕获并按 Enter 才提交。
5. 用户自行发送一个任务，观察 Working 动画、工具前 Thinking 灰色竖线、工具状态行和最终 Markdown。检查临时文本不重复留在终端历史。
6. 执行中 Ctrl+C 取消，回到提示符后不应自动执行残余粘贴；空提示符再次 Ctrl+C 退出。分别测试 `/exit`、EOF，总结各只显示一次。
7. 检查长表格、代码、中文换行，缩小终端宽度后仍可编辑；必要时检查 IDE 是否拦截 F2/F3/F4。

## 边界与剩余风险

- **不支持 Bracketed Paste 时，普通模式无法可靠区分粘贴换行和 Enter。必须在粘贴前启用 F2。** 未标记粘贴直接进入普通模式仍可能因 Enter 发送；这是用户确认的显式降级边界。
- F2 模式捕获普通文本和换行，功能键继续是编辑指令；不保证任意含终端控制序列的二进制剪贴板可以原样粘贴。
- 历史回忆和 F3 展开的内容显示完整正文，允许用户检查/修改；不是自动重新折叠。
- 真终端颜色、字体、功能键编码与刷新体验取决于 PyCharm/终端版本；管道测试不等于真实终端兼容认证。未验证的 Shift+Enter/Ctrl+Enter 不作为必需快捷键。
- 长文本完整保存在当前草稿内存中，折叠减少显示高度，不减少发送给模型的原始任务长度。

## 修改文件

- `src/nexus/app/input.py`：Frame/样式、粘贴块、F2/F3/F4、Enter、SlashCompleter、剪贴板/历史边界。
- `src/nexus/app/cli.py`：快捷键提示和 `/help` 文案。
- `src/nexus/app/tui.py`：Working 动画、Markdown 紫色主题；保留既有 Thinking 分类与工具行。
- `tests/test_input_v02.py`：新增输入、真实渲染和动画测试。
- `tests/test_cli_ux.py`：按 V0.2 提交/粘贴方式验证既有 CLI 与 JSONL 行为。
- `tests/test_memory_daily.py`：仅将实际输入测试的 `/exit` 提交键改为 Enter，Memory 实现未改。
- 本说明与 `docs/next/evidence/stage3-tui-ux-v0.2/` 下明确列入提交的离线证据。

本轮 7 个专用系统临时目录已核验后清理，未修改权限；原有工作区报告、`.idea/` 和 V0.1 临时证据保留。见 [cleanup.txt](evidence/stage3-tui-ux-v0.2/cleanup.txt)。
