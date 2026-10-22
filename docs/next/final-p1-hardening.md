# V0.3 Stage 1 Final P1 Hardening

日期：2026-10-22。基线 `68cc2676d58c6313d836ae6ea64990e3584d53b0`。
交付分支：`codex/nexus-v0.3-stage1`；不合并 main 或 `codex/nexus-v0.3`。

本轮仅修复 Token 估算、窗口默认值、压缩输入容量预检和 Provider Overflow 分类。
单 Agent Loop、连续历史及 Checkpoint、A+B 职责、Compaction Prompt/Schema、
128000 主窗口、90% 阈值、20000 Recent History、32768-byte Tool Result、
Plan/Stagnation/工具执行语义均保持不变。未运行 Nexus 8 Case 或 FilePilot Dogfooding。

## 1. 证据范围和方法

模型配置来自冻结的 `context-runtime-v0.3.toml`，不读取或修改本机另一套全局模型配置：

| 用途 | Provider / Model | 参数 |
| --- | --- | --- |
| Main | DeepSeek 官方 `https://api.deepseek.com` / `deepseek-flash` | `reasoning_effort=high` |
| Compaction | SiliconFlow `https://api.siliconflow.cn/v1` / `Qwen/Qwen3.5-35B-A3B` | `enable_thinking=false`，JSON Object |

先离线核对已有 A/B 冻结压缩请求：6 份独立输入、每份 2 次历史 Usage，**不是 12 个独立样本**。
另发现旧 runtime probe 的主模型 `21491/8235`、`21607/8330` 和压缩模型
`17307/7046`、`17401/7103`（估算/上报）显示明显高估；由于未保存完整的原始 wire 请求，
这些仅作问题定位，不纳入新旧配对指标。百炼渠道会话原始 Usage 没有混入 DeepSeek 官方统计。

之后先冻结 24 次请求及 SHA256，再执行一次：两套模型各 12 个样本。
每套包含英文、中文、Python、JSON、重复日志、工具定义、完整工具调用/结果连续请求、
短工具结果、两段真实混合历史重放，以及 119K/124K 两个合成长输入边界。
**真实历史重放**是同一真实会话的两个不同片段，作为历史数据放入请求，
不是重跑 Coding Task，也不是原生产会话全部原样 wire 消息；工具协议另有独立样本。
不能用这些少量、主动挑选的样本推导生产平均误差或生产 Overflow 频率。

计数探针 `max_tokens=16`、stream=true、n=1、include_usage=true；不执行模型工具输出。
这是受控的低成本输入计数实验，**不是完整的 4096-token 摘要质量实验**。
Qwen 的原生完整压缩 Prompt 另由历史冻结样本覆盖。
API Key 只从环境变量取得，无密钥明文写入 TOML/证据。

23 次成功取得 Usage，1 次 Qwen 124K 因 TPM 限流失败（HTTP 429/code 50602）；
失败原文保留，未补跑、未算作 Token 误差成功样本。
成功请求合计输入 **502892**、输出 **318** Tokens；成功调用总耗时 **61.219 秒**，
另有限流失败 **1.484 秒**。以 Provider 总 `prompt_tokens` 为准，包含缓存命中 Tokens。

原始请求和全部流式响应位于本机 `artifacts/final-p1/manifest.json`、`responses.jsonl`。
为避免发布真实会话原文，真实历史请求仅保留在本机；Git 中交付其 SHA256、Usage 和误差，
以及可公开的受控请求/边界 fixtures。原始响应、失败和指标见
[`evidence/final-p1`](evidence/final-p1/measurements.json)。
复算命令：`uv run python scripts/token_accuracy.py artifacts/final-p1`（离线，不调用 API）。
仅 clone 仓库无法恢复未公开的真实历史请求；受控请求和历史压缩请求可由 pytest 独立复核。

## 2. Tokenizer 选择与实现

Qwen 官方 tokenizer 加官方 chat template，对历史 6 份独立压缩输入的 12 次 Usage 全部精确匹配。
DeepSeek V4 Flash 官方 tokenizer 和编码模板的正文分词也匹配本轮官方 `deepseek-flash`；
普通文本请求观察到额外 26 Tokens 的固定服务端包装差值。
工具输入的残差较小且略有不同，因此没有声称公开模板就是官方服务端完整实现。

采用本地固定版本的两套官方 tokenizer、DeepSeek 官方编码模块和 Qwen 原生模板，
无需 runtime 下载、网络分词服务或额外模型调用。资源约 5.4 MB（gzip），
版本、源文件 SHA256 和许可证见 [`tokenizer_data/NOTICE.md`](../../src/nexus/core/tokenizer_data/NOTICE.md)。
新增 `tokenizers` 与 Jinja2 两个直接依赖；不引入 Transformers/Torch。
第三方编码模块原样保存，其 lint/type 例外仅覆盖 `_vendor`。

协议包装修正：DeepSeek 加固定 26 Tokens；Qwen 普通/压缩请求不加偏置，工具模板加 5。
这些是实测的**加法包装残差**，不是按上下文长度放大的安全余量。
DeepSeek +26 来自前五种无工具受控输入，后续真实混合历史和长边界保持匹配；
工具请求保守多估 10～16 Tokens。Qwen +5 来自工具定义样本，后续工具调用样本一致。
这些参数的拟合与复核都基于本轮有限数据，不构成独立生产分布泛化证明。

选择器仅命中已实测的 hostname+model 精确组合；其它 Provider/Model 保留明确标识
`fallback` 的旧轻量估算，不把当前误差承诺扩展到未知模型。
Role、工具 Schema、工具调用及结果、reasoning continuation、消息模板均进入计数。

主/压缩模型分别持有校准器，不共享比例。采用最近三个有效 Usage 比例的中位数，
限制在 `[0.85, 1.15]`；原始输入小于 256 Tokens 不训练比例，缺失 Usage 清空反馈。
不扣除 cached_input_tokens。System/工具 Schema/消息形态变更重置该模型的校准；
压缩成功只清空主模型的旧历史校准。整数比值采用 Fraction，避免浮点向上取整多出 1 Token。
该范围约束异常反馈的影响，**不是新增 15% 预留**；冷启动因子为 1，并可向上或向下修正。
若 Provider 漂移超过此范围，诊断中的原始/校准/上报差异应被调查，不能保证反馈消除任意漂移。

## 3. 修复前后误差

以下为每模型 10 个短/中等及混合样本，不含边界；各行相同固定输入配对。
平均使用绝对相对误差，防止高估和低估抵消；P90 使用 nearest-rank。
“旧校准”离线按冻结顺序回放基线单比例逻辑，绝不使用当前样本 Usage 反算当前估算。
该序列用于暴露内容变化/首次请求问题，并不模拟生产会话的采样概率。

| 指标 | 旧原始 | 旧单比例校准 | 新冷启动 | 新有界校准 |
| --- | ---: | ---: | ---: | ---: |
| Main 平均绝对相对误差 | 38.48% | 31.38% | 0.95% | **0.74%** |
| Main P90 绝对相对误差 | 82.91% | 55.41% | 3.18% | **2.13%** |
| Compaction 平均绝对相对误差 | 39.59% | 32.08%* | 0.00% | **0.00%** |
| Compaction P90 绝对相对误差 | 77.57% | 53.02%* | 0.00% | **0.00%** |
| 最大高估倍数（含成功边界） | 2.335× | 2.335×* | 1.042× | **1.032×** |
| 最大低估倍数（含成功边界） | 2.243× | 2.243×* | 1.000× | **1.000×** |

\* 基线压缩预检事实上只用原始估算；这里的旧校准列是把同一基线校准算法应用于
压缩模型样本的离线参照，不冒充旧 runtime 已具有独立压缩模型校准。
高估倍数=estimate/reported，低估倍数=reported/estimate，未出现时记 1。
新有界校准 20/20 短/中等/混合样本处于 ±15%，成功边界 3/3 同样满足。
带符号平均误差：Main 旧原始 +18.22%、旧校准 −4.58%、新 +0.74%；
Qwen 分别 +17.56%、−5.26%、0%。这些比绝对误差小，正说明不能只看带符号均值。

真实历史重放单列：Main 两段旧原始平均绝对误差 19.72%、旧校准 21.83%、新 0%；
Qwen 对应 24.45%、25.30%、新 0%。只有两个相关片段，不能宣称生产零误差。
旧估算不只是高估：例如 Main 工具定义估 140、实际 314（低估 2.243 倍）；
Qwen 重复日志估 2754、实际 3826。新计数分别为 324、3826。

历史真实压缩请求独立对照（每行两次 Usage 一致）：

| 输入 | 旧估算 | Provider prompt_tokens | 新计数 |
| --- | ---: | ---: | ---: |
| A / ABF-original | 17803 | 7497 | 7497 |
| A / CDE-initial | 1383 | 857 | 857 |
| A / DE-update | 1910 | 1209 | 1209 |
| B / ABF-original | 18685 | 8115 | 8115 |
| B / CDE-initial | 1526 | 948 | 948 |
| B / DE-update | 2214 | 1419 | 1419 |

## 4. 容量统一与边界

自动阈值、主模型安全检查、固定前缀开销、Recent History 分割、压缩前后计数和
压缩后的安全检查统一使用主模型计数器；压缩输入预检及反馈只用压缩模型计数器。
Semantic Summary 仍保留 4096 预算，Runtime Facts 仍保留已有的 2048 估算预算与裁剪规则；
Runtime Facts 的独立内容预算单位未迁移，整个真实请求（含 Facts）仍用对应模型重新计数。
Recent History 目标 20000，完整工具批次不拆分，用户边界的既有 1.25 倍容忍保持不变。

| 冻结边界 | 旧估算 | 新计数 | Provider Usage | 128K 压缩容量判断（预留 4096+1024） |
| --- | ---: | ---: | ---: | --- |
| Qwen ~119K | 277834 | 119000 | 119000 | 允许；旧逻辑误拒绝 |
| Qwen ~124K | 288901 | 124000 | 无：429 TPM | 拒绝；仅本地边界验证，不能声称 Provider Overflow |
| DeepSeek ~119K | 272580 | 119026 | 119026 | 分词/包装在长输入下吻合 |
| DeepSeek ~124K | 284875 | 124026 | 124026 | 分词/包装在长输入下吻合 |

Main 的预留为 8192+1024，安全输入是 118784；因此上述 Main 119K/124K
探针被 Provider 接受，不代表运行时应该突破自己的安全输入限制。
Compaction 安全输入是 122880。新预检并未更改 Provider 本身窗口或增加动态模型路由。

新增边界回归验证：精确容量等号允许、差 1 Token 拒绝；拒绝产生 `compaction_failed`
并附输入估算/容量，模型零调用；原始消息保持相等，JSONL 可 replay，失败前 checkpoint
不变；成功摘要后 append/flush checkpoint，原始历史不截断。
另有冻结 119K/124K 输入测试，验证旧误拒绝与新容量边界。

证据支持修复明显高估和本轮样本的低估，但有限探针不能证明生产中永不 Overflow，
也没有测量其长期发生频率。仍保留已知 Overflow 后固定压缩、原主模型重试一次的恢复链。

## 5. 默认窗口与 Overflow

`parse_model()` 缺省窗口改为 128000，两段独立解析、覆盖；
仍要求 `context_window > max_output_tokens + 1024`，明确配置保持原行为。

DeepSeek [官方错误码说明](https://api-docs.deepseek.com/quick_start/error_codes/)
没有穷举上下文错误 JSON。针对官方项目中的一手公开故障记录
[#3399](https://github.com/deepseek-ai/deepseek-harness/discussions/3399) 补充兼容：
嵌套/扁平的 `quota_limit_reached` + `Input token exceed the limit`，以及明确的最大上下文文案。
该记录最初经 OpenAI-compatible 代理，不能将其称为本轮直连抓包或服务端规范。
文字兼容仅限 DeepSeek 官方 hostname 的 HTTP 400；通用明确 `context_length_exceeded`
和 `context_length_error` 继续支持。额度/月用量、余额、参数、未知模型等普通 400 均不误判。

固定 HTTP 错误经过真实 ChatModel adapter → Agent Loop → 一次独立压缩 → 同一主模型重试。
若第二次仍 Overflow，正常停止，不第二次压缩、不换模型。
新增真实百万 Token 超限抓包被自动审批以成本/授权风险拒绝，未执行；
恢复链证据来自固定响应测试，不包装为本轮真实 Overflow API 验收。

## 6. 验证与交付状态

- 本地全量 pytest：**573 passed，10 skipped**（10 项 opt-in Docker 隔离验收未启用）。
- Ruff、Mypy、`uv lock --check`、`uv build`：通过。
- 最初全量执行因既有 `%TEMP%/pytest-of-Archer` 权限失败；另一次暴露旧无界校准假设。
  两次日志保留。修复后使用独立系统临时父目录通过，不改 ACL、不在仓库根目录创建临时文件。
- 修复了缺失 Usage 仍应保留当前原始估算诊断的回归；老测试中的夸张假 Usage 改为
  有效校准样本或真实内容容量场景，没有改变 Plan/Skills/Stagnation 实现。
- 此前用户要求的 pytest 临时目录策略一并保留：系统临时目录、通过用例删除、失败最多保留 3 轮。
- Windows/Linux CI：推送后核对，状态与 run 链接记录在交付回复。

结论：本轮所覆盖模型、渠道和输入上，估算显著改善，已有原始 Usage 支撑，
不是把 `/3` 换成 `/4` 或扩充安全余量。仍有样本有限、渠道升级漂移、未知模型回退、
Qwen 124K 限流和真实 Overflow 抓包未执行的边界。本轮到此停止优化，进入正式验收准备。
