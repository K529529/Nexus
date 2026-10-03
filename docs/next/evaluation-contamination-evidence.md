# Evaluation V0：目标源码污染隔离修复

范围：仅修复评测容器的 answer leakage，不实现 Context V0.2.1，不改 Agent Loop、Context、测试材料或 validator 判分逻辑。没有调用模型或重新执行八项模型评测。

## 确认的发现

原先的虚拟环境和 `PYTHONPATH` 只控制默认导入位置，不能限制 Agent 直接读取其他环境。Agent 虽然没有网络、Docker socket 和隐藏测试挂载，仍可通过绝对路径读取镜像继承的源码。

| Case | 清理前实际发现 | 准备后的扫描 | 实际 import 来源 |
| --- | --- | --- | --- |
| pvlib | 原 base 的 pvlib dist-info；继承的 conda 包缓存。未发现额外 pvlib 实现目录 | PASS | `/workspace/pvlib/__init__.py` |
| Matplotlib | `/opt/miniconda3/envs/testbed/lib/python3.9/site-packages/matplotlib`，版本 3.9.4；另有准确 base 的生成文件副本 | PASS | `/workspace/lib/matplotlib/__init__.py` |
| Django | Jedi 中的 Django 类型存根和适配文件；conda 缓存 | PASS | `/workspace/django/__init__.py` |
| Requests | 主 conda 环境 Requests 2.31.0、testbed 中 2.32.3；多个 pip vendored requests；conda 展开目录及 `.conda` 包；类型存根 | PASS | `/workspace/requests/__init__.py` |
| Sphinx | testbed 中 Sphinx 4.5.0 及其元数据、命令入口、类型存根 | PASS | `/workspace/sphinx/__init__.py` |
| Click | 未发现额外 Click 源码；仍按同一规则检查归档及缓存 | PASS | `/workspace/src/click/__init__.py` |
| pytest | 预装 pytest 8.4.2 的 `pytest` / `_pytest` 与元数据，虽然不是未来版本，仍清除独立源码副本 | PASS | `/workspace/src/pytest/__init__.py` |
| Rich | pip `_vendor/rich`；ensurepip 内 pip wheel 中也包含 vendored Rich | PASS | `/workspace/rich/__init__.py` |

扫描不是把所有发现都认定为“未来版本答案”：元数据、旧版本和类型存根单独说明。清理采用更严格的单一目标源码原则，消除不必要的额外副本。

## 最小实现

- `Environment.prepare()` 在工具可被 Agent 使用之前调用 `environments/isolate_target.py`。清理失败、扫描残留或不可读归档均使环境准备失败。
- 遍历容器可见文件系统，覆盖非激活 Python 环境、`site-packages`、vendored 目录、目标命名源码/字节码/元数据、conda/pip/source 缓存；检查 wheel/zip/egg/tar 成员，删除含目标的归档。清空继承的 conda 包缓存和临时构建材料。
- `/workspace` 保留既有准确 blob/mode 验证、单一合成 commit；`/proc`、`/sys`、`/dev` 属于虚拟文件系统，不作为镜像文件树扫描。已安装但指回 `/workspace` 的 editable 元数据及 pytest import-only 命令入口可保留。
- Matplotlib 必需扩展先从准确 base 的 `/opt/nexus-generated` 复制到 workspace，再删除生成文件副本。pytest 清理后继续使用原来的 workspace editable 安装，然后重新扫描。
- 使用相同冻结镜像 ID，清理仅作用于一次性容器，宿主机镜像和源码缓存不被删除。**直接启动原始镜像仍可能看到污染；隔离保证适用于正式 `Environment.prepare()` 完成后的容器。** 无 Docker socket、额外挂载和网络的既有边界保持不变。
- 固定测试材料、`targets.json`、F2P/P2P 判分语义、Agent 和 Context 源码均未修改。

## 证据与复验

- [八项 contamination qualification](../../evaluation/next-dev-v0/contamination-qualification.json)：原始镜像 ID、发现路径、准备后的扫描计数/错误/残留、导入来源、源 tree 和 Git 状态。八项均零扫描错误、零目标残留，初始 Git 状态为空。
- 本地详细证据：`artifacts/evaluation-contamination/before.json`、`after.json`，以及 `controls/` 中保存的独立 validator 输出。无模型 API 请求。
- 新增离线回归：非激活解释器与 vendored 副本、wheel 内嵌副本、缓存、workspace 排除、editable 元数据指向、损坏归档拒绝。
- `tests/test_evaluation_docker.py::test_real_target_contamination_scan` 为八项参数化复验入口。可用 `NEXUS_TEST_DOCKER=1` 和 `pytest tests/test_evaluation_docker.py -k contamination` 重新执行，不调用模型。
- 清理后的八项 validator 正负对照全部通过：所有规定 F2P 在 base 上失败、所有 P2P 在 base 上通过，八项参考修复全部通过。复用已有控制面参考补丁，没有重新生成答案；检查命令和断言未更改。具体计数与正对照结果已合并到 contamination qualification JSON。
- 离线回归：197 项均通过。首次沙箱执行 195 passed、2 项 Windows 进程清理失败；这两项在允许进程树清理并使用独立临时目录后复跑 2 passed。默认跳过 10 项 opt-in Docker 测试；八项实际扫描由本次证据脚本另行完成。Ruff 与 mypy（43 文件）通过。
- 现有 Docker 命令边界回归另外实际执行：2 passed，覆盖补丁收集、超时/取消、网络/挂载隔离，以及新验证容器拒绝空 Rich 补丁。

## 对已有模型基线的影响

`20261002T193618Z-96e0877c` 及此前单 case 的原始产物继续保留，原记录中的 PASS/FAIL 不被改写。但原环境存在污染路径，不能再把这些结果解释为无答案泄漏的编码能力基线，或用于干净的 Context Before/After 对照。后续重新建立基线须统一使用本次隔离逻辑；本次没有重跑模型。

本次资格检查针对固定八个镜像、正常可见包/源码树和归档，不声称检测任意恶意混淆或改名嵌入的代码。新增或替换镜像后需要重新资格检查。
