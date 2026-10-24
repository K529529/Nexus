# Stage 3 CLI / TUI UX V0.1 验收说明

- 分支：`codex/nexus-v0.3-stage3-harness-hardening`
- 基线：`8cf40e4b2cbc9205983cfb86dc2d65516c64d2bb`
- 工作区：`D:\WorkSpace\releasePro\github\Nexus\Nexus-stage3`
- 本轮不运行付费模型、`nexus eval --all` 或 FilePilot 开发；不合并 V0.3。

## 根因与修复

1. 默认单行 `PromptSession` 把未带 Bracketed Paste 标记的换行解释为提交。多行粘贴因此变成连续 User Message / Run。
2. prompt_toolkit 会保存已读取但尚未使用的 typeahead 按键，供下次提示符消费；只取消 Agent 并不能取消这些排队输入。
3. `TERM=dumb` 下 PromptSession 默认切换到单独的简化提示符，可能绕过自定义输入绑定。本轮显式传入现有输出对象，保留统一输入规则。
4. 原 CLI 对输入整体 `.strip()`，并且提示符收到 KeyboardInterrupt 后无条件继续，不能做到“非空清稿、空稿退出”。

采用安全多行模式：Enter 始终换行，连续按 Esc、Enter 才提交。终端不报告粘贴边界时，原始换行与物理 Enter 没有可靠区别；本轮没有使用时间猜测或自动提交规则。Bracketed Paste 由 prompt_toolkit 正常处理；未标记粘贴同样不会因换行提交。CRLF 统一成逻辑 LF，保留空行、缩进、Tab、Unicode 和任务首尾空白。`.strip()` 仅用于空输入与 slash 命令判别。

运行/安全清理期间消耗并丢弃输入，只转发首次 Ctrl+C 给原有取消入口。返回编辑器前清除 typeahead。编辑时 Ctrl+C 清空草稿及已排队按键；空草稿 Ctrl+C 退出。Windows OS SIGINT 与键盘 Ctrl+C 使用同一绑定。退出清理期间忽略额外 SIGINT，不取消已经启动的清理。Agent/工具仍在原来的拥有任务中执行，MCP 生命周期未移到另一个任务。

## 按键

| 按键 | 行为 |
| --- | --- |
| Enter | 插入换行，不执行任务 |
| Esc 后紧接 Enter | 提交完整草稿（Alt+Enter 仅在终端发送相同序列时等效，不作为兼容保证） |
| 方向键 / Backspace / Delete | 编辑与滚动当前草稿 |
| Tab | 插入 Tab |
| Ctrl+C（非空草稿） | 清空草稿，不执行 |
| Ctrl+C（空草稿） | 安全退出 |
| Ctrl+C（运行中） | 取消本轮，等待清理后返回提示符；清理中额外 Ctrl+C 不重复取消 |
| `/exit` + 提交键 | 安全退出 |
| Ctrl+D（空草稿）或输入 EOF | 安全退出 |

运行和清理期间不支持提前输入下一个任务，这些输入会被丢弃。超过终端高度的草稿由 prompt_toolkit 换行、滚动；提交后只打印摘要及字符/行数。后台状态输出通过 prompt_toolkit 的 stdout 协调恢复提示符。

## 展示与数据边界

- Thinking 展示 **工具调用轮次里的公开 Assistant Content**：按已提交消息的 tool_calls / model_finished.finish_reason 分类，使用 `dim italic` 与左侧竖线。
- 已有协议校验确保无 Tool Call 的正常完成消息属于最终回答。最终内容用 Rich Markdown 渲染；标题、表格、列表、引用和语言代码块沿用 Rich 语义。
- 流式阶段尚未分类，使用 transient Live 临时预览。刷新最多约 10 Hz，预览保留末尾最多 4,000 字符 / 10 个逻辑行，物理高度受终端约束。正式消息到达后擦除临时帧，只打印一次分类后的完整内容。
- 取消或失败的未提交预览明确标为非最终回答；停止 Live 和待刷新计时器。非 TTY / dumb 输出使用普通文本流，不重复补印最终消息。支持光标控制但禁用颜色的终端仍可使用无颜色 Markdown。
- 工具状态行、实际 `patch_diff` 事件预览及旧日志兼容路径保留。
- 不读取或展示 private reasoning_content；不改模型消息、Session JSONL、Replay、Context、Memory、Agent Loop、工具协议或传输异常边界。

## 退出统计口径

统计范围是本次交互 CLI 中发布的 Run / Model / Tool / Compaction 事件，跨 `/new`、`/resume` 累计，与 `--profile` 的每 Run 重置相互独立。历史回放及私有 Memory Exit Summary 不计入这些事件计数，界面明确说明这一范围。

Wall Time 使用单调时钟，包含编辑和清理；Model / Tool Time 分别累计事件时长，不与 Agent 时长相加。工具成功率为已结束工具调用的执行成功率，另列 unfinished；无已结束调用显示 N/A。缺失 usage 显示 unknown 或 `>= ... (partial)`。

Interaction Summary 完全本地计算，不发模型请求。既有 Memory Exit Summary 的一次性、限时、低推理和静默日志行为保持不变；因此 Memory 开启且满足原有条件时，退出仍可能发生已有的 Memory 汇总请求，这不是新增 UI 统计请求。

## 验证

| 检查 | 结果 |
| --- | --- |
| 新 UX 测试 | 17 passed，含 2,000+ 中文任务、原始/Bracketed Paste、LF/CRLF、快速连续粘贴、OS SIGINT、重复取消、清理隔离、真实 EOF |
| 相关 CLI/TUI/Memory/Profile 定向回归 | 159 passed；随后新增真实管道 EOF、提交瞬间取消用例单独通过，纳入全量 |
| 完整 pytest | **793 passed, 10 skipped**，57.83s |
| Ruff | All checks passed |
| Mypy | Success: no issues found in 97 source files |
| 新 JSONL Replay / Session Resume | 真实 Conversation + 离线模型替身；一条完整 User Message、一个 Run，无提交前模型事件，恢复消息一致 |
| Patch 审计 / 工具副作用 / Compaction / 传输异常 | 原有相关测试包含在全量，通过 |
| Windows PyCharm 实际键盘、剪贴板与屏幕体验 | **NOT RUN，等待用户手动验收** |
| 8 Case / 付费模型 | **NOT RUN** |

原始结果见 [全量测试](evidence/stage3-tui-ux/pytest-full-final.txt)、[定向测试](evidence/stage3-tui-ux/focused-pass.txt)、[Ruff](evidence/stage3-tui-ux/ruff.txt)、[Mypy](evidence/stage3-tui-ux/mypy.txt)。开发中失败及修正见 [验证过程](evidence/stage3-tui-ux/attempts.md)。

[静态样例](evidence/stage3-tui-ux/preview.html) 由 [离线脚本](evidence/stage3-tui-ux/render_sample.py) 调用真实展示代码生成，明确标注为模拟事件，不代表真实模型、工具执行或 PyCharm 实测。静态导出省略被光标擦除的 Live 临时帧。

## Windows PyCharm 手动验收

不要直接依赖原 `Nexus-next/.venv` 的 editable 安装位置；该解释器可能仍指向旧 checkout。在用于验收的 PyCharm Terminal 执行：

```powershell
$env:PYTHONPATH = 'D:\WorkSpace\releasePro\github\Nexus\Nexus-stage3\src'
& 'D:\WorkSpace\releasePro\github\Nexus\Nexus-next\.venv\Scripts\python.exe' -c "import nexus; print(nexus.__file__)"
& 'D:\WorkSpace\releasePro\github\Nexus\Nexus-next\.venv\Scripts\python.exe' -m nexus --profile
```

第一条检查必须显示 `Nexus-stage3\src\nexus`。工作目录选择你愿意执行测试任务的目录；本轮不自动操作 FilePilot。

1. 粘贴 2,000 字符以上中文 Markdown，包含标题、空行、缩进、代码块、Tab 和 Emoji。等待几秒，确认没有 Run、工具调用或模型请求；方向键可查看和修改全文。
2. 连续按 Esc、Enter，确认只提交一次，字符/行数合理，仅出现一个 Run。观察工具轮次 Assistant Content 为灰色斜体竖线，最终回答为 Markdown；工具状态及 Patch 预览单独保留。
3. 第二次再次粘贴，按 Ctrl+C 清稿；确认没有任务执行。空提示符再按 Ctrl+C，确认退出总结只出现一次。
4. 重新启动，提交耗时任务；运行时 Ctrl+C，并在清理期间连续按 Ctrl+C、粘贴多行文字。确认清理后没有自动启动下一任务，也没有残余 Live 行；返回提示符后仍可提交新任务，再于空提示符 Ctrl+C 退出。
5. 分别通过 `/exit`（需提交）和空草稿 Ctrl+D 退出，确认总结口径正确。用 `nexus resume` 检查已保存消息和 Handoff，工具不自动重放。
6. 可分别关闭颜色、切换 PyCharm Terminal 引擎验证降级。快捷键若被 IDE 截获，记录终端引擎、TERM 和实际行为；不要把管道测试当作实机兼容性证明。

## 修改文件与限制

产品代码仅涉及 `src/nexus/app/input.py`（新增）、`cli.py`、`tui.py`、`profile.py`。新增 `tests/test_cli_ux.py`；其余 CLI/Memory/Profile/Skills 测试只适配模拟输入构造参数、输出与新提交键，保持原来的业务断言。

主要行为变化：Enter 改为换行；运行期间提前输入被丢弃。未标记粘贴不靠速度猜测。真正的终端快捷键拦截、窗口缩放、字体宽度和大 Markdown 渲染观感仍需 PyCharm 实测；超长输出生成中的预览有界，完整答案仍在结束时展示。用户验收通过前不继续 Dogfooding。
