# Stage 4 Local 8-case 回归快速分析

日期：2026-10-28。仅分析已有产物，未重跑评测、调用模型或修改项目实现。

## 结论

本轮 **6/8 PASS**，未发现明显的 Local Agent Runtime 系统性退化或新增合并阻塞。两个 FAIL 均为任务补丁未满足独立验证器的正确性要求，不能因 Agent 正常结束或自测通过而记为成功。

结合用户已完成的 Windows/Linux Sandbox 与 HITL 人工验收，本轮结果支持进入 V0.3 合并流程；残余 UI 细节可按用户安排在发版前调整。这是小样本回归判断，不是“所有能力无退化”的证明。本次未执行合并。

## 数据来源与可比性

- Run：`20261028T104039Z-57e6d22e`
- 产物目录：`C:\Users\Archer\.nexus\evaluation\results\20261028T104039Z-57e6d22e`
- 代码：`6853e70f93bf03df2bf835f32f16ce6a56e1d21a`，Stage 4 分支本次验收版本。
- 读取 `manifest.json`、`summary.json`、8 个完整 session JSONL（共 3,277 条记录），并核查两个失败用例的 `patch.diff` 和 `validator.log`。
- 主模型 `deepseek-flash`；压缩模型 `Qwen/Qwen3.5-35B-A3B`；每任务最多 40 步。8 个 `run_started.execution_mode` 均为 `local`。
- 与下表两个近期完整回归逐项比较：用例顺序、模型及压缩模型配置、预算、上下文策略、用例镜像与任务/验证器/报告器哈希均一致。Nexus 代码版本不同。排除中止及未完成批次。

| Run | PASS | 失败用例 | 模型调用数 | 执行总耗时约 |
| --- | --- | --- | ---: | ---: |
| 20261024T055859Z-d3c0919a | 6/8 | Matplotlib、Django | 176 | 1,987 秒 |
| 20261024T075115Z-56b25ce9 | 6/8 | Django、Sphinx | 192 | 2,010 秒 |
| 本轮 | 6/8 | Matplotlib、Django | 225 | 2,044 秒 |

总通过率保持一致；相对最近一轮，Sphinx 转为通过，Matplotlib 转为失败，不能将单个任务的变化完全归因于代码改动。模型调用数增加约 17.2%，总耗时增加约 1.7%；存在效率波动，但单次结果不足以认定性能回归。

## 八个用例

“工具失败”指执行期间 `tool_finished.ok=false` 的次数，不等同于最终用例失败。

| 用例 | 独立验证结果 | 模型调用 | 工具调用 | 工具失败 |
| --- | --- | ---: | ---: | ---: |
| pvlib__pvlib-python-1707 | PASS | 25 | 41 | 3 |
| matplotlib__matplotlib-22835 | FAIL | 20 | 31 | 3 |
| django__django-11734 | FAIL | 30 | 53 | 6 |
| psf__requests-6028 | PASS | 24 | 45 | 3 |
| sphinx-doc__sphinx-10323 | PASS | 37 | 48 | 5 |
| click__path-generic-type | PASS | 28 | 40 | 6 |
| pytest__doctest-optionflag-leak | PASS | 28 | 37 | 2 |
| rich__double-width-wrap | PASS | 33 | 46 | 6 |
| 合计 | 6/8 | 225 | 341 | 34 |

## 两个失败的实质

### Matplotlib：消除了异常，但数字格式语义错误

补丁在 `BoundaryNorm` 分支直接设置 `g_sig_digits = 3`。验证器要求 `format_cursor_data(0.9)` 返回 `[0.9]`，实际返回 `[0.900]`；`test_format_cursor_data_BoundaryNorm` 失败，验证结果为 **1 failed、26 passed、2 skipped**。

Agent 新增的自测直接断言 `[0.900]`，因此其自测通过不能证明格式正确。这是本轮最值得关注的质量问题：测试重复了实现假设，未独立验证预期行为。该案例在此前完整回归中也失败过；本次证据未指向 Sandbox、HITL 或工具执行损坏。

### Django：修复覆盖不完整

补丁在 `split_exclude()` 中将 `OuterRef` 再包一层，并新增 `exclude` / `~Q` 自测。Agent 报告选定测试通过，但独立验证器的 `test_subquery_exclude_outerref` 仍失败：预期 `qs.exists()` 为 False，实际为 True。验证器运行 **372 个测试，1 failure、9 skipped、2 expected failures**。

这说明补丁未完整满足关联子查询的语义要求；“完成运行”不等于“完成修复”。两个近期对照批次中的 Django 也均失败，因此未显示这是 Stage 4 新引入的失败类别。本次不进一步展开任务级修复研究。

## 运行链路与异常

- 8/8 `run_finished.outcome=completed`；模型调用开始/结束数量一致，341 个工具调用的开始/结束 call ID 多重集一致；各 JSONL 的 seq 连续。未见本轮日志断链或挂起收尾问题。
- Click 在第 9 步首次模型请求出现 `APIConnectionError` / `model_transport`，约 15.2 秒后记录重试，最终用例 PASS。该用例 28 次模型尝试只有 27 次具备 usage，因此累计 token 只能按已知下界解释，不据此计算精确成本。
- 工具错误共 34 次：`command_exit_nonzero` 24、`path_escape` 5、`invalid_patch` 4、`patch_conflict` 1。运行随后均继续完成；命令非零退出包含测试/探测失败，不能直接视作执行器故障。
- 抽查 pvlib 的 `path_escape` 为 apply_patch 使用 `/workspace/pvlib/iam.py` 绝对路径，工具要求工作区相对路径。这类格式/路径错误增加了调用开销，但不是已证实的越权写入。
- Django、pytest、Rich 各出现一次 stagnation nudge，随后均正常结束；没有证据据此认定死循环。最高模型步数为 Sphinx 的 37/40。
- 本轮没有压缩事件，不覆盖真实上下文压缩；Local 回归也不重新证明 Windows/Linux 隔离、HITL 或终端 resize 的正确性，这些依赖各自独立验收。

## 后续建议

不需要为这两个历史任务失败阻塞 Stage 4 合并，也不建议在本轮扩大修复范围。保留原始失败证据；后续提升 Agent 任务质量时，优先关注“测试预期独立于补丁假设”和“复杂查询边界覆盖”。发版前按既定计划处理剩余 UI 细节。
