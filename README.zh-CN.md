<p>
  <img src="docs/next/assets/nexus-wordmark-violet.svg" alt="Nexus" width="320">
</p>

### 在项目终端里，用自然语言完成编程任务。

Nexus 阅读代码、修改文件、运行工具，并保留可检查的会话记录。
核心是一套手写、消息驱动的 Agent Loop；可选子代理复用同一套循环。

Python 3.12+ · Windows / Linux · [MIT](LICENSE) · [English](README.md) / **简体中文**

**当前为 0.3.0 待发布候选，不代表 PyPI 已提供 0.3.0。**
[Release Notes](docs/next/release-0.3.0.md) · [验收记录与 CI](docs/next/release-0.3.0-validation.md)
· [使用手册](docs/next/usage-guide.zh-CN.md)

## 快速开始

在独立环境中安装候选源码：

```sh
git clone --branch codex/nexus-v0.3 https://github.com/K529529/Nexus.git
cd Nexus
python -m venv .venv
```

PowerShell 激活：`.venv\Scripts\Activate.ps1`；Linux 激活：
`source .venv/bin/activate`。然后运行：

```sh
python -m pip install -e .
python -m nexus --version
cd /path/to/your/project
nexus
```

启动目录就是工作区，其中的 `AGENTS.md` 提供项目指导。
可编辑安装会跟随这份源码；换项目后仍需使用同一个环境，
也可以通过该环境的 `nexus` 可执行文件绝对路径启动。

首次交互启动会询问模型名、endpoint、上下文窗口和 **API Key 环境变量名**，
保存到 `~/.nexus/config.toml`。在启动终端设置对应变量，不把密钥写入聊天或 TOML：

```powershell
$env:NEXUS_MODEL_API_KEY = "<你的密钥>"
nexus
```

Linux 使用 `export NEXUS_MODEL_API_KEY="<你的密钥>"`。长会话还需要单独配置压缩模型；
首次向导只配置主模型，不会自动启用压缩。详见[配置与升级](docs/next/usage-guide.zh-CN.md#配置)。

默认 **Local**，工具以你的用户权限执行。长期记忆默认**开启**，首次使用可能下载
用户目录内的 Node/QMD 和检索模型；不需要时可先执行 `nexus memory off`。
多 Agent、Web 工具和外部遥测默认**关闭**。

## 日常使用

| 操作 | 命令 / 快捷键 |
| --- | --- |
| 交互 / 单次任务 | `nexus` / `nexus exec "修复问题并运行测试"` |
| 指导正在执行的任务 | 直接输入，按 **Enter** |
| 排队根代理的下一项任务 | 运行中直接输入，按 **Tab** |
| 查看 / 编辑 / 撤回待处理输入 | `/pending`、`/edit ID 内容`、`/withdraw ID` |
| 选择代理会话 | **F6**、`/agents`、`/agents ID` |
| 回到主代理 / 查看归档代理 | **Esc**、`/agent root` / `/agents history` |
| 阅读历史 / 回到最新 | **PgUp**、**PgDn**、**Ctrl+End** |
| 换行 / 取消 | **F4** 或 **Alt+Enter** / **Ctrl+C** |
| 模型 / 权限菜单 | `/model` / `/permissions` |
| 恢复 / 新会话 | `nexus resume`、`/resume` / `/new` |
| 记忆 / Skills / 帮助 | `/memory`、`/skills`、`/help` |
| JSON 事件 / 运行统计 | `nexus exec "任务" --json` / `nexus --profile` |

补全菜单优先于 Tab 排队。进入子代理视图后，Enter 指导该子代理，
Tab 仍排到根代理；已完成子代理为只读。见[多 Agent 配置与边界](docs/next/stage8-usage.md)。

## V0.3 能力

- 连续会话上下文、独立压缩模型、确定性的执行事实；原始 JSONL 不被摘要覆盖。
- 全局 Markdown 记忆、QMD 检索、可恢复遗忘与稳定会话快照。
- Windows Native、Linux/WSL Docker 受限执行、显式审批与工具级恢复；
  Windows 是部分强制隔离，Local 使用当前用户权限。
- 模型别名与切换、stdio MCP 资源/工具、可选 Web 与仅元数据的 OTLP 遥测。
- 可选 Fresh 上下文的 scout/worker/reviewer；共享工作区、全树并发额度、
  消息交付、取消、恢复和独立会话视图。

Agent 宣布完成不等于独立测试通过。[发布说明](docs/next/release-0.3.0.md)保留验证证据、
平台限制及历史失败。FeatureBench 对照固定为 `0fd7fa4`，发布修复是后续提交。
DeepSeek Pro/high 单 Agent 批次记录为进行中；本轮发布准备未启动多 Agent 对照。
不预填分数，也不宣称多 Agent 更快或更准确。

## 文档入口

- [使用手册](docs/next/usage-guide.zh-CN.md)：配置、权限、恢复、日志和快捷键。
- [当前架构与代码导读](docs/next/engineering-walkthrough.md)。
- [开发、构建与发布流程](docs/next/development-and-release.md)。
- [本地八项评测](evaluation/next-dev-v0/README.md)：与 ABK FeatureBench 分开。
- [历史 0.2.0 发布记录](docs/next/release-0.2.0.md)：保留历史安装和成绩。
