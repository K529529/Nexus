# Stage 4D：TUI 跨平台兼容与 HITL 交互

日期：2026-10-28。分支：`codex/nexus-v0.3-stage4-sandbox`。

基线：`a9de1b78950d39cc25fb88f072c38f2fb864e750`。
最终实现 Commit：`cf50a4b14d8344e78072e24b3bc4cbc887ecaf02`。
本报告与原始证据由后续 documentation commit 记录；交付分支 tip 以 Git 历史为准。

## 结论与验收状态

用户截图中的 WSL 首行提前提交、重复紫色边框已在真实 PowerShell→WSL 链路复现。
候选在该链路的 ConPTY 和 WinPTY 两种桥接模式下，10 轮输入均完整、只有一个当前编辑框，文字位于编辑行。
HITL 改为可见的默认拒绝选择界面，原有精确调用授权协议不变。

**待用户双平台人工复验，不宣称所有 Windows 终端均通过。** 原生 Windows 被 WinPTY 包装时仍存在布局异常，旧基线同样失败；ConPTY 原生 Windows 通过。本轮保留 Windows 原生布局和即时 Enter 路径，没有为这个额外组合更换 Windows UI。
本轮不合并 V0.3/main，不继续扩展 Sandbox。

## 修改边界

生产代码仅修改 `src/nexus/app/input.py`、`tui.py`、`cli.py`：分别负责输入/布局/审批输入所有权、Host 审批摘要、现有决策值映射。
增加 `tests/test_tui_stage4d.py`，调整现有输入与 CLI 测试，增加两个终端探针脚本。
Agent Loop、Model Transport、SecurityGateway、SecurityPolicy、Patch Worker、Windows Native、Linux Docker 和 JSONL 协议均未改动。
未运行旧 SRT PoC，未修改 ACL、模型配置、用户凭据或 Docker 镜像。

## 复现与根因

### 任务截断

原 Enter 绑定看到非空输入立即提交。传统终端转发的多行粘贴可能没有 Bracketed Paste 标记，CR 被识别为 Enter：第一行成为任务，其余字节进入 typeahead，随后被 Busy 隔离丢弃。`nexus exec` 绕过该输入路径，因此正常。

基线在真实 WSL PTY、PowerShell→WSL ConPTY/WinPTY 均出现 raw-paste 失败；Bracketed Paste 路径完整。输入含中文、emoji、Markdown、代码块和多行命令，以原文/UTF-8 SHA256 验证，未仅依赖屏幕显示。

修复检查输入队列，并在 VT 字节流路径为独立 Enter 留出 120ms，识别跨 OS read 的粘贴突发；突发内换行进入同一草稿，CRLF 只计一个换行。原生 Win32 普通 Enter 保持即时提交。F2/F3/F4、Ctrl+J、剪贴板、历史和 slash completion 保留。

未标记的字节流本身不携带“人按 Enter”与“慢速粘贴换行”的区别。120ms 是有界突发识别，不保证任意慢速分块粘贴；这类终端使用 F2 显式粘贴模式。该限制没有通过修改模型消息或自动补全丢失内容掩盖。

### 边框错位

普通 POSIX PTY / ConPTY 基线没有复现累积边框；WinPTY→PowerShell→WSL 可以稳定复现。基线 10 轮可见顶框数为 `0,1,1,2,2,3,3,4,4,5`，文字落在下边框，符合用户截图。

固定屏幕底部的 `bottom_toolbar` 与经典 Console 桥接的 CPR/滚动区域计算共同触发重绘错位。对照实验：留出最后一列无效；禁用 CPR 的诊断实验有效，但未采用；把提示放入编辑器布局的普通一行后有效。给该行继续使用 `class:bottom-toolbar` 或把旧 toolbar 设为空字符串仍失败；空字符串仍满足 toolkit 的 `is not None` 条件，CPR 后会启用旧底部窗口。

最终按实际 `Vt100_Output` 能力适配：帮助行跟随编辑器正常排布，旧 `bottom_toolbar=None`，保留 CPR、全宽边框和全部输入功能。`Windows10_Output`/Win32 原生布局不变；已有 Reworked IME 右边框与动态高度处理保留。
候选 PowerShell→WSL 的两种桥接 10 轮顶框数均为 10 个 `1`，逐轮检查文字与 `Nexus` 位于同一编辑行。

### Windows Unicode 边界

真实 Win32 VT 输入会将部分 emoji 分成两个 UTF-16 surrogate key events。新增输入层配对合并，避免无效 UTF-8 进入日志/模型；中文普通字符不变。探针在子进程 JSON 序列化之前计算 UTF-8 哈希，以免 JSON 解码自动合并 surrogate 而掩盖问题。回归覆盖独立两个 KeyPress。

## HITL

- 默认 Deny；Tab/↓ 下一项，Shift+Tab/↑ 上一项；Enter 确认，Esc/Ctrl+C 取消。
- D/F3 展开详情，PgUp/PgDn 滚动参数与 Patch。
- 每次审批新建局部选择状态；选择必须先绘制，排队的 Tab+Enter 不能批准尚未显示的选项。
- 粘贴/普通文本不能批准，会重置为 Deny 并清除输入队列；进入和退出时隔离 typeahead。
- `source.detach()` 暂停 Busy 读取器，由一个 Application 临时持有 stdin，结束/超时恢复 Busy，随后正常编辑焦点恢复。
- Operation/Target/Mode/Scope 来自 Host 保存的 ApprovalRequest；Agent reason 单独标示。详情先做现有配置密钥值脱敏和终端控制字符清理，再截断；32,000 字符上限明确提示信息不足应 Deny。
- CLI 仍生成原有 request ID 对应的 `allow_once`/`deny`/`cancel`；digest、超时、单次生命周期、审计事件均由原 SecurityGateway 处理。

真实 UI + Gateway + JSONL writer 集成覆盖允许、拒绝、取消、超时、参数篡改拒绝；执行端用计数器观察 dispatch，不将其冒充真实 OS 隔离验收。本轮未重新做 Sandbox 隔离测试。

## 验证结果

证据目录：[`docs/next/evidence/stage4-tui`](../next/evidence/stage4-tui/)。每个终端目录保留 report.json、原始 ANSI 与重建屏幕 JSON；只含合成测试任务，不复制用户桌面截图。

| 验证 | 结果 | 证据 |
|---|---|---|
| Windows 全量 pytest | 1005 passed，10 skipped（原有 opt-in Docker 测试） | `pytest-full-final.log` |
| WSL 已安装候选 UI/Gateway 回归 | 162 passed | `linux-ui-final-02.log` |
| Windows 最终 CLI 输入同步测试 | 24 passed | `windows-cli-final-02.log` |
| Ruff / Mypy | PASS，61 source files | `ruff-final.log` / `mypy-final.log` |
| Wheel 构建、两平台独立安装 | PASS，安装文件与实现源码哈希相同 | `build-final.log`、`install-*-final.log`、`installed-*.json`、`source-manifest.json` |
| WSL POSIX PTY，CPR 开/关 | raw/bracketed/F2+F3+resize/10轮原文均 PASS | `final-posix-cpr` / `final-posix-no-cpr` |
| PowerShell→WSL，ConPTY | 原文与 10 轮布局 PASS | `final-conpty-wsl` |
| PowerShell→WSL，WinPTY/classic Console bridge | 原文与 10 轮布局 PASS | `final-winpty-wsl-02` |
| 原生 Windows，ConPTY | 原文与 10 轮布局 PASS | `final-conpty-native-02` |
| 原生 Windows，WinPTY | 原文 PASS，布局 FAIL；旧基线亦 FAIL | `final-winpty-native`、`baseline-winpty-native` |
| 原生 Windows WinPTY 直接 Console 单元核对 | 基线/候选布局均 FAIL，不仅是 ANSI 转录误差 | `*-winpty-native-console` |
| 真实审批：WSL WinPTY / Windows ConPTY | Deny、Allow、Cancel、下一次默认 Deny 均 PASS | `final-winpty-wsl-approval` / `final-conpty-native-approval` |

真实终端探针启动独立 Python/PowerShell/WSL 子进程，使用实际 TaskInput、prompt_toolkit 与 Rich 输出，无模型调用。`pyte` 用于转录屏幕核对，不替代真实终端执行。WinPTY backend=1，ConPTY backend=0；目录 `baseline-conhost-wsl` 是早期命名，实际 backend=0，以 report.json 为准。
软件版本：prompt_toolkit 3.0.53，Rich 14.3.4；WSL `TERM=xterm-256color`、Vt100Input/Vt100_Output；Windows 探针 `TERM=dumb`、Win32Input/Windows10_Output。只记录允许列出的诊断字段，没有导出整个环境。

本轮独占 Windows TEMP 测试目录与已归档安装日志已清理，未发现探针残留进程，未修改 ACL，详见 `cleanup.json`。Linux 失败测试的系统 TEMP 目录仍按 pytest 原有保留策略保留；仓库未新增 pytest 临时目录。

CLI 集成测试通过 ScriptedModel 捕获实际请求，检查首个用户消息、持久 JSONL 与 resume 原文完全相同且只启动一次 Run。Host 另附 plan/budget 消息与任务原文分开断言。本轮无付费推理。

长 raw 草稿和 F3 展开后的顶部边框可能位于可视区上方，`frame_counts=0` 本身不表示短编辑框错位；10 轮短输入用独立 layout_pass 判据。真实窗口 IME composition、实际鼠标粘贴、Windows Terminal GUI 的 Ubuntu Profile 仍需人工复验；ConPTY 自动化通过不等于该 GUI 全面通过。

## 保留的失败与修正

- `input-initial.log`：留一列实验造成 2 个边框断言失败，删除该无效方案。
- `ui-02.log`：8 个断言把 Host plan/budget 用户角色消息也当作任务原文；调整为实际首个任务后通过。未改变消息构造。
- `linux-ui-final.log`：4 个 raw 测试仅等待首字节便提前按 Enter；改为等待完整草稿后，162 项通过。退出时 bad file descriptor 是失败清理阶段的继发错误。
- `windows-cli-final.log`：受限工具进程无法访问此前普通用户测试创建的 TEMP，8 个 setup errors；同一普通用户权限运行后 24 项通过，没有修改 ACL。
- `candidate-winpty-wsl-01`：留最后一列没有修复积框；`final-winpty-wsl`：带 bottom-toolbar 样式的首版仍积框，不能视为最终通过。
- 诊断 NO_CPR 仅影响探针子进程；未修改全局环境或采用该实现。
- 早期跨 `/mnt/c` 状态文件原子替换遇读句柄冲突，改为追加 JSONL；首次诊断 HSplit 参数错误、早期 Windows surrogate 写状态错误均保留原始输出。部分旧探针退出码为 0 但 report 的 pass/layout_pass 为 false，判定以 report 各字段为准；最终脚本已返回失败退出码。
- 原生 Windows WinPTY 布局不修饰为 PASS。保留现有 Windows 原生路径，等待日常 PowerShell 人工验收决定是否需要另一个窄范围适配。

## 独立候选与人工复验

旧 Windows/WSL Stage 4C 候选保留。新候选配置解析均成功；配置默认仍为 Local，启动命令显式指定权限模式。
安装代码哈希见 manifest。Docker Worker 指纹保持 `2927d7ed4632f291e3b265405f497de1537d3904dda2927d00296521e9298f04`，没有构建/替换镜像。

Windows 普通 PowerShell，在既有批准的测试 Workspace 启动：

```powershell
cd D:\WorkSpace\nexus-native-hardening-manual-20261026\workspace
& D:\WorkSpace\nexus-stage4d-windows-20261028\Scripts\nexus.exe --permission-mode workspace-write
```

WSL2 Ubuntu，使用现有模型凭据会话：

```bash
cd /home/archer/nexus-stage4c-20261027/work
/home/archer/nexus-stage4d-20261028/venv/bin/nexus --permission-mode workspace-write
```

本轮不更改凭据。若新 WSL 会话没有模型所需环境变量，沿用此前已验收的 Windows→WSLENV 会话传递流程；不要把密钥放进命令历史、仓库或 config.toml。

人工步骤：

1. 先 Windows，再相同 PowerShell→WSL 入口：输入中文、英文和混合标点，连续 10 轮短任务，观察文字在框内且没有空框累积。
2. 粘贴包含中文、Markdown、缩进代码的长任务，确认完整后发送。测试 F2 开始/结束粘贴、F3 展开、F4/Ctrl+J 换行、窗口缩放与 Ctrl+C 清空/退出。
3. 观察连续工具输出后下一轮输入框，确认历史/补全仍正常。
4. 退出后用相同候选与 Workspace，改为 `--permission-mode read-only`。只要求修改测试文件，触发 Patch 权限请求。
5. 默认 Deny；用 D/F3 查看 Host 范围和 Patch，按 Tab 选择 Allow once，看到选择后按 Enter。确认本次 Patch 成功，后续仍是 Read-only。
6. 再触发一次，确认默认又是 Deny；分别验证 Deny、Esc/Cancel 不产生写入。不要修改真实项目。

只有用户确认两个平台实际 UI 均正常后，才考虑单独授权的 Stage 4 合并。本轮到此暂停开发。

## 实现依据

检查了实际安装的 prompt_toolkit 3.0.53 `PromptSession`、Application、Renderer、Win32Input 和 Windows10_Output 源码，尤其 bottom_toolbar 的 `is not None` 条件及 input attach/detach 生命周期。
参考：[prompt_toolkit API](https://python-prompt-toolkit.readthedocs.io/en/3.0.52/pages/reference.html)、[Application 源码](https://github.com/prompt-toolkit/python-prompt-toolkit/blob/main/src/prompt_toolkit/application/application.py)。本轮结论以本地版本和保存的运行证据为准。
