# Stage8 集成交付证据

日期：2026-11-03。分支 `codex/nexus-v0.3-stage8-multi-agent`，从 V0.3
`c6f1b2ca1c1351abc6a3542a232cf18dc8e9a85c` 创建；本轮不 merge / publish。

## 验证结果

| 检查 | 结果 |
| --- | --- |
| Windows 全量 pytest | 1209 passed / 10 skipped；16 条第三方 LiteLLM/Pydantic 弃用警告 |
| 最后增加的排队 `/` 文本边界回归 | `test_stage8_input.py` 6 passed（包含原 5 项与新增 1 项） |
| Ruff | `ruff check --no-cache .` 通过 |
| mypy | `mypy src tests`，144 个文件通过 |
| 锁文件 | `uv lock --check --offline`，102 packages，通过 |
| 构建 | `uv build --offline`，wheel / sdist 成功 |
| Windows 独立安装 | 新建系统临时 venv，按 uv.lock 约束安装 wheel，源码目录外 `python -I -m nexus --version` 与角色/HTML 资源检查通过 |
| Windows uv tool | 独立临时 UV_TOOL_DIR / UV_TOOL_BIN_DIR 安装同一 wheel 并启动通过 |
| Linux | WSL Ubuntu / Python 3.12.3，独立 venv 安装最终 wheel，118 passed / 1 skipped |
| diff | `git diff --check` 通过 |

Windows 全量原始输出：[windows-pytest.txt](windows-pytest.txt)。最后一个队列命令边界修复
发生在该全量运行之后，随后复验完整输入测试，并在最终 Linux wheel 集合中覆盖。
Linux 安装路径与原始输出：[linux-smoke.txt](linux-smoke.txt)；使用 `-o pythonpath=.`
排除 sdist 的 src 路径，测试实际加载安装包。两个平台模型均为确定性替身；Shell、
进程树取消、崩溃子进程、MCP 本机 stdio 为真实执行。
Windows 跳过 10 项显式 opt-in Docker 隔离验收；Linux 跳过 Windows PowerShell 专属项。

最终 wheel SHA256：`f6dac6840cdcbb36eab93ad4ec9fcffeffee7b1ffff87160cb543ac5a8ea90b0`。
包版本仍为仓库原有 0.2.0；这是该开发分支的本地构建，未发布新版。

## 尝试与修复

- 首轮全量：1196 passed / 11 failed / 10 skipped。11 个失败来自旧 CLI 测试替身缺少
  Application.layout 或 Inbox 接口；更新替身后相关 145 项通过，再全量 1208 passed。
- 收尾修复退出汇总漏算子代理、输入投递期间编辑/撤回竞态、嵌套通信遥测重复记录；
  后续全量 1209 passed。另补排队 `/help` 等文本不进入空闲命令分派、只执行一次的回归。
- 初次独立安装因 uv 缓存不完整失败；使用锁文件约束补齐依赖后完成真正独立安装。
- WSL pip 下载触发 files.pythonhosted.org TLS EOF；通过 Windows uv 下载锁定版本的
  Linux 包到本轮临时目录，复制到新建 WSL venv 后再用 pip 安装本地 wheel。
  未复用或修改旧 WSL 测试环境。初次 smoke 命令误指不存在的 test_mcp.py，未执行测试；
  更正为 test_mcp_tui.py / test_mcp_resources.py 后执行通过。
- 未改变个人配置、API key、全局工具安装或原有 untracked 文件。

## 验收边界

未运行 ABK、本地真实 8-case、真实模型多 Agent 体验、LangSmith 远端试发或人工 Windows
IME/TUI 手感验收。没有性能收益或费用降低结论。用户明确指定 ABK 自行手动运行。
本地报告和遥测投影已验证；远端元数据范围由用户明确授权，未新增上传正文。

Windows 超长日志路径仍受系统文件名限制。测试使用系统临时目录下唯一短父目录，
没有在仓库创建 pytest basetemp。既有失败临时目录由 pytest 短期保留；不清理来源不明目录。
独立安装验证的专用临时环境在证据保存后清理，构建产物保留在已忽略的 dist 中。

配置、四组真实体验和 ABK 对照准备见 [使用与验收](../../stage8-usage.md)。
