# Stage7：OTel 与模型适配

本增量按 `stage7-otel-litellm-codex.md` 授权实施，取代 V0 原文中禁止 LiteLLM 依赖的旧限制。
Agent Loop、Session JSONL 与 durable 恢复仍为 Nexus 自己管理。

## OTel 使用

默认关闭。在 `%USERPROFILE%/.nexus/config.toml` 添加：

```toml
[tracing]
enabled = true
endpoint = "https://api.smith.langchain.com/otel/v1/traces"
api_key_env = "LANGSMITH_API_KEY"
project = "nexus"
timeout_seconds = 10
# workspace_id = "your-workspace-uuid" # 多工作区 key 按实际权限选择
```

endpoint 为完整 **traces endpoint**，直接传给官方 Python exporter，不再拼接路径。
EU/APAC/AWS US 域名分别为 `eu.api.smith.langchain.com`、
`apac.api.smith.langchain.com`、`aws.api.smith.langchain.com`。
agent 型工作区使用 `agent_id` + `environment` 替代 `project`，两者不能混用。
environment 为 local/development/staging/production。

PowerShell（密钥已存为 Windows 用户变量时）：

```powershell
$env:LANGSMITH_API_KEY = [Environment]::GetEnvironmentVariable('LANGSMITH_API_KEY', 'User')
& "D:\WorkSpace\releasePro\github\Nexus\Nexus-next\.venv\Scripts\python.exe" -m nexus
```

用户变量和当前进程环境不同；旧终端必须刷新或从已刷新环境的父进程重新启动。
在 LangSmith 打开对应 workspace → nexus 项目，按元数据 `otel.span.nexus.run_id`
定位本轮任务。平台可能把 OTLP 根 span ID 映射为其 trace UUID；原始 trace ID
保存在 `OTEL_TRACE_ID`。父节点页面显示的汇总 usage 由平台聚合，Nexus 只在 attempt 上发送 usage。

失败后使用同一解释器运行：

```powershell
& "D:\WorkSpace\releasePro\github\Nexus\Nexus-next\.venv\Scripts\python.exe" -m nexus trace export "C:\path\session.jsonl" --run-id "run-id"
```

此命令显式发送指定 run，复用自动路径的投影与 exporter。稳定 ID 不承诺平台 exactly-once，
补发可能覆盖/重复呈现，取决于平台。尾部不完整记录忽略并提示；中间损坏拒绝读取。

## 数据来源和边界

持久化 `run_finished` 后，从本轮已持久化事件投影，不重扫/发送整个 session。
每 run 一个 trace：run → model request → actual attempt；tool 与 request 同属 run，
通过 request ID、step、tool call ID 关联。压缩过程含 purpose=compaction 的模型请求。
仅有完成记录的操作生成 span；缺少结束的旧操作报告 unknown 并省略，不伪造成功。
恢复出的工具结果若没有本轮 tool_started，不生成虚假的工具执行。
空白/旧日志缺少逻辑 request ID 时保留独立 attempt，报告无法准确归组。

自动导出在 run 结束点，包含 run 内压缩；结束后的 idle `/compact`、memory exit summary
属于会话维护，不纳入已结束 run 的 trace。SIGKILL/断电不能保证运行钩子，使用手动补发。

仅投影元数据，不上传 prompt、reasoning、源码、工具参数/输出、工作区路径、shell 命令、异常正文。
固定 span 名称和字段白名单；工具/模型等标识经过字符与长度限制。身份密钥只从环境读取。
官方 OTLP/HTTP exporter 在受管理子进程执行，同步 HTTP/SDK 有 timeout，父进程另设
timeout+2 秒总预算；超时终止并回收工作进程，避免后台 HTTP/DNS/重试拖住主进程。
不启用 BatchSpanProcessor 后台线程或无限队列。导出失败不修改已持久化业务结果，
只给本地提示和补发命令。SDK 内部有限重试也受工作进程总预算约束。

## 字段映射（2026-10-31 核实）

采用 OTel Semantic Conventions 1.44.0 的 GenAI 字段（仍有发展中字段），锁定实际 SDK 在 uv.lock。

| 事实 | 输出属性 |
|---|---|
| session/run/request/step/attempt/purpose | nexus.* |
| 请求与响应模型 | gen_ai.request.model / gen_ai.response.model |
| provider | gen_ai.provider.name；gen_ai.system 为 LangSmith 兼容映射 |
| 模型/工具操作 | gen_ai.operation.name = chat / execute_tool |
| 工具名称和调用标识 | gen_ai.tool.name / gen_ai.tool.call.id |
| reported input/output/cache tokens | gen_ai.usage.input_tokens / output_tokens / cache_read.input_tokens |
| 错误分类 | error.type 与 OTel ERROR；不导出异常正文 |
| 平台节点类型 | langsmith.span.kind = chain / llm / tool |

unknown usage 不发送，不补零；父 span 不重复发送 Token。实际 UTC 时间来自事件，
不使用导出时刻替代开始或结束。取消/失败 run 不标 success，失败重试 attempt 保留 ERROR。

参考：
- https://docs.langchain.com/langsmith/trace-with-opentelemetry
- https://opentelemetry.io/docs/languages/python/exporters/
- https://opentelemetry.io/docs/specs/semconv/gen-ai/

真实验收见 `evidence/stage7-otel/live.json`；云端 API 验证与网页人工验收分别记录。

第一轮验证：完整回归 1125 passed / 10 skipped；补充后 OTel 专项 14 passed。
ruff、mypy、uv lock --check、uv build、wheel 源码目录外导入通过。
初次完整回归因新增 trace CLI 局部导入遮蔽模块导入失败，已修复并完整重跑；未删除原有断言。
