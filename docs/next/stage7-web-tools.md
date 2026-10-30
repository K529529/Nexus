# Stage 7 · Web Search / Fetch

2026-10-31 用户批准实施，分支 `codex/nexus-v0.3-stage7-tools-protocol`。
已有 MCP 内嵌文本资源支持保留；本增量只增加两个内置 Web 工具。

## 使用

在用户级 `~/.nexus/config.toml` 配置：

```toml
[web]
enabled = true
api_key_env = "DEEPSEEK_API_KEY"
timeout_seconds = 60
```

Key 从启动进程环境读取，不写入 TOML。未配置时关闭；启用但缺 Key 时明确提示变量名，
仅停用 Web 工具，不阻止其他工具工作。修改配置/环境后重新启动 Nexus。
主模型与 Web 凭据独立，主模型不必使用 DeepSeek。

- `web_search(query, count=5)`：query 1..128 Unicode 字符，count 1..50；固定
  `need_content=false`。返回标题、URL、summary（空时回退 snippet）、date 和服务商评分。
- `web_fetch(url)`：单个公开 HTTP(S) 页面，返回标题和 Markdown 正文。URL 最长 8192，
  不接受凭据、明显本地地址或非公网 IP；不在本机解析域名或抓取目标网页。

API 固定为 `https://api.deepseek.com/v1/tools/search` 和 `/fetch`，Bearer 鉴权。
仅发送工具参数；不自动附带仓库或完整会话。返回内容是工具数据，不是系统指令。
参考：[官方 Search](https://api-docs.deepseek.com/zh-cn/api/search/)、
[Fetch](https://api-docs.deepseek.com/zh-cn/api/fetch/)、
[指南](https://api-docs.deepseek.com/zh-cn/guides/search/)。

## 实现边界

- 一个 `tools/web.py`，复用现有 httpx；按 Conversation 复用客户端，实际调用才创建，退出关闭。
- 沿用 ToolResult 的 data/error_code/truncated；UTF-8 预算按序列化后的 data 计算，
  保留合法 JSON 和完整 URL，长正文截断不算失败。结果过多用 omitted_results 表示。
- 共享现有 output_limit_bytes；HTTP 响应最多 2 MiB；总请求期限默认 60 秒（配置 1..600）。
- 空搜索结果成功；无正文、非法响应、超时和 HTTP 错误形成失败 observation。
  401/402/403/404/429 等可区分，不回显原始错误正文或凭据，不自动重试/改走 Shell。
- Local 默认工具组装按配置注入，capability 为 web-read，继续经过 Gateway/ask_tools。
  read-only/workspace-write 当前停用；不把宿主 HTTP 标为 internal 来绕过网络边界。
  自定义/评测 registry 不自动继承用户 Web 配置。
- API 请求不跟随重定向；目标网页的 DNS/重定向由 DeepSeek 处理，不声称完整 SSRF 隔离。
- 沿用 Stage 6：完整结果恢复不重发；已分发但中断的远端完成/计费状态未知，不自动重放。
  不引入新 Loop、协议顶层字段、Provider 框架、插件、缓存或后台任务。

## 验收

离线测试覆盖请求契约、结果/错误、预算、取消、客户端关闭、配置/权限、默认与评测注册隔离，
以及 scripted model → search → fetch → model、日志/恢复不重复请求。完整测试、Ruff、mypy、
锁文件、构建及仓库外 wheel smoke 使用项目既有验收方式。

用户明确选择自行启动 Nexus 做真实任务验收。本次自动验证不发送真实 DeepSeek 搜索、
抓取或模型请求；离线 PASS 不代表账号/余额/真实网页访问已通过验收。
建议人工任务：搜索 Python 官方 asyncio 文档，使用 web_fetch 阅读其中一页，回答并附来源 URL。
