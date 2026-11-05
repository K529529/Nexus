> 历史阶段记录：以下状态、实验与失败保留当时原貌。当前合入/发布状态见
> [0.3.0 发布准备记录](release-0.3.0-validation.md)，当前使用见 [手册](usage-guide.zh-CN.md)。

# Stage8 多 Agent 交互修复

日期：2026-11-03。基于 `6441042f88dbf88f72e745ffe1d9cb8baee39046`，
分支仍为 `codex/nexus-v0.3-stage8-multi-agent`。用户确认 Nexus 和 Pi 的三张
参考截图均来自 PyCharm 内置 PowerShell。本轮参考 Pi 的信息层次改善渲染，
不引入新的 Agent Loop、编排框架或消息模式。

## 实现

- 交互模式将 Transcript 绑定到 TaskInput。运行中的助手内容预览、工具活动和
  Working 状态统一由 prompt_toolkit 绘制，不再同时启动 Rich Live 移动终端光标。
  已提交正文仍通过既有 stdout 协调机制滚动到输入区上方；非交互 exec 保留原展示。
- 代理树放在输入框上方：角色、短 ID、状态、耗时、当前操作、用量和 Fresh 标记。
  普通生命周期变化更新树，不再逐条刷入正文；求助和失败仍给出醒目通知。
- 动态区最多 12 行，按可用高度进一步收缩，先为编辑器预留空间；超出树行数显示
  `/agents` 入口。长行按终端显示单元截断，避免中文长内容折行挤占输入区。
- 运行态帮助栏紧贴编辑器，包括 PyCharm Reworked 原生输出分支；窄窗口优先保留
  Enter guide / Tab queue，将目标移到 Working 行。保留原有 Reworked IME 右边框留白。
- Enter 直接指导当前目标、Tab 排队、审批草稿、多行、粘贴、取消和交接去重逻辑不变。
  不改变模型历史、子代理接收边界、执行关联、共享工作区、并发额度和工具结果配对。

## 验证

- 新增 `test_stage8_runtime_ui.py`，4 项通过。真实 prompt_toolkit 输入管道与 Rich
  共用 VT 输出，覆盖连续正文输出、模型预览、工具活动、8×25 短窄窗口及尺寸变化、
  中文多行与折叠粘贴、审批恢复、Enter / Tab 各投递一次并保留目标、完成后继续输入，
  以及 PyCharm 原生 Reworked 帮助栏/IME 适配分支。
- Windows 全量：**1226 passed / 10 skipped**，193.26 秒，16 条既有第三方
  LiteLLM/Pydantic 弃用警告。10 个跳过项均为显式 opt-in Docker 验收。
  [完整输出](evidence/stage8-ui/windows-pytest.txt)。
- 最后的窄窗口帮助栏调整发生在全量测试启动后；之后重新运行新增 4 项及完整专项集合，
  **164 passed**（59.02 秒），[专项输出](evidence/stage8-ui/targeted-pytest.txt)包含
  全部 Stage8、输入、审批、TUI、Conversation 和恢复测试。
- Ruff、完整 mypy（147 文件）、uv lock（102 packages）、wheel/sdist 构建通过。
  独立临时环境按锁定依赖离线安装，在源码外验证导入和 CLI，
  [安装证据](evidence/stage8-ui/package-smoke.txt)。
- 构建 wheel SHA256：`6acc441c302ebcd4eb45a0bbf4102e9bcb2f20582ac0780651a17bee7ce35125`。
  仓库版本仍为 0.2.0；没有发布新包。构建产物保留在已忽略的 dist 目录。

开发过程中的失败与边界：首轮新布局存在闭包变量重用导致的高度计算递归，已修复；
受限环境专项为 162 passed / 1 failed，真实进程树清理被限制，正常进程权限下的全量
通过。曾误用 PowerShell 不展开的 pytest 文件通配符，未运行测试，后改为显式文件列表。
这些尝试不计为验收通过。原有测试的消息完整性、投递去重等断言未削弱。
首笔 UI 提交 `c1acc19` 推送前的最后一次 HTML 尾空白清理引入了辅助脚本的 Ruff
ASYNC240 告警；随后改为在线程中写出文件并复验 Ruff、mypy 和快照生成，单独补交。

## 离线体验与未验证项

在仓库目录的 PyCharm PowerShell 中运行：

```powershell
.venv\Scripts\python.exe scripts\stage8_ui_smoke.py
```

这是显示夹具：模拟两名代理持续活动并输出工具摘要，可直接 Enter / Tab 验证输入手感，
Ctrl+C 退出，最长约 90 秒自动结束。不加载模型配置、不调用工具或网络、不写会话日志。
它不作为真实多 Agent 执行证据。

[渲染帧 HTML](evidence/stage8-ui/runtime-frame.html)由实际 prompt_toolkit 屏幕导出，
不是手写布局 mock。生成命令：

```powershell
.venv\Scripts\python.exe scripts\stage8_ui_smoke.py --snapshot docs\next\evidence\stage8-ui\runtime-frame.html
```

**尚未完成 PyCharm 宿主内的视觉和微软拼音人工体验验收。** Computer-use 技能禁止
操作 shell 应用，因此没有向用户既有终端注入命令；随后浏览器工具拒绝 `file:` 协议，
未能自动打开 HTML 做视觉预览。已验证屏幕布局数据与交互，但不将其等同于宿主截图。
Linux/macOS 本轮未重复验证。ABK、本地真实 8-case、真实模型和外部遥测试发均未运行。
保留既有未跟踪文件，不改用户代理或模型配置；不 merge / publish，等待复审与人工验收。
