# Stage 4C Linux Docker Sandbox 实施与验收

日期：2026-10-27（Asia/Shanghai）。分支：`codex/nexus-v0.3-stage4-sandbox`。
基线：`22baacb6fbdb316161388177c02d4e7a8f4015b8`。
最终实现 Commit：`33e456a26daeae0b91c6f7153fe43852c4b569a2`，包含实现提交
`0ff248e66b464e5f77a293f4a1348114ff33dbc0`；报告与原始证据另作交付提交。

结论：独立安装包在真实 WSL2 Ubuntu / Docker Desktop Linux Engine 中完成 **63/63** 项
自动化集成验收。最终代码全量 pytest **975 passed / 10 skipped**，Ruff、mypy、Wheel 构建通过。
这不是双平台最终人工验收，也不是所有 Linux 发行版的 Production Ready 声明。
Windows Native V1 按用户 Stage 4C 指令冻结，原有 Python 0700 问题仍存在。
未运行旧 SRT PoC、付费模型调用或 Benchmark，未合并 V0.3/main。

## 实现与信任边界

保留 Agent Loop → Tool Registry → Security Gateway/HITL → Backend 的链路。
轻量 selector 在 Windows 选择 Native，在 Linux/WSL2 选择 Docker，其他平台受限模式失败。
Local 继续原执行器，不创建、导入或探测 Docker Backend。

- 每个受限 Conversation 惰性创建一个唯一长驻容器，后续 Shell/Patch 复用。
  Labels 包含随机 Session ID、工作区、UID、machine/boot 标识、PID/starttime。
  每次复用校验实际归属、运行状态、镜像、用户、挂载、网络、资源及安全选项。
  不根据工作区路径复用外部容器。
- 工作区映射 `/workspace`，RO 或 RW bind，关闭递归子挂载；根文件系统只读，独占 `/tmp`
  tmpfs 可写。以当前 Linux 非 root UID/GID 执行；默认 2 CPU、1024 MiB、128 PID，
  cap-drop ALL、no-new-privileges、私有 PID/IPC/cgroup namespace，无 privileged/设备授权。
  不挂载 Docker Socket、Home、Nexus 会话、凭据目录，不继承模型 API Key。
- 用户配置位于工作区之外；拒绝敏感 bind 根、symlink 根/祖先、安装/CLI/配置位于写根内。
  V1 不支持 extra `writable_roots`，显式报错。只支持本机 unix Docker endpoint。
- 网络仅支持 `bridge`（默认可联网）和 `none`。旧域名策略显式拒绝，包括配置了空列表的情况。
  Windows 受限模式若配置 Docker 字段也报错，避免 `docker_network=none` 被误认为生效。
- Docker 命令由 argv 调用，不拼接宿主 Shell。Shell 在容器中执行，支持工作目录、流式输出、
  输出预算、退出码、Timeout/Cancel。可信 image worker 提供结束帧；宿主 CLI 丢失且没有结束帧时，
  不将其当作普通命令失败，而是销毁容器并报告 `interrupted_unknown`。

Docker 的 bind 源由 daemon 解释，资源/namespace 限制依赖 Engine。实现和验证依据：
[Docker run](https://docs.docker.com/reference/cli/docker/container/run/) 与
[bind mounts](https://docs.docker.com/engine/storage/bind-mounts/)。本轮验证的是 WSL ext4 工作区，
不是远程 daemon 或 Windows DrvFS bind 的等价性。

### Patch、HITL 和中断

复用 Patch Core/格式解析，Worker 来自可信镜像 `/opt/nexus`，不从工作区加载源码。
经用户明确确认，只有 Linux 开启可选写前屏障：

`planned patch_diff → 宿主 JSONL 持久化 → ACK → 重新检查冲突 → 写入 → patch_committed/ACK`

ACK 关联请求 ID、Tool Call、文件序号及事件类型；未确认 ACK 不创建目录或执行该文件写入。
中断结果区分已确认完成文件与计划后未确认文件，不自动重放。多文件 Patch 仍不是事务，
最后一次写入与 committed ACK 之间的中断可能产生未知状态。Windows/Local 默认顺序保持原状。
TUI 的写前 Diff 明确标为 Planned，不能把它当作已提交事实。

RO Patch 默认由 Gateway 拒绝。精确 Allow once 使用独立短命 RW 容器，仅执行获批 Patch，
结束即清理；原长驻 RO 容器挂载和后续 Shell 权限不变，授权不能复用。

Timeout/Cancel 或执行状态未知时销毁所属容器并检查其已消失，不仅杀 `docker exec` 客户端。
清理失败报告 `cleanup_incomplete`，会话不可继续复用；本版本需要新 Conversation。
初始化取消会等待有界 create/start 操作返回可清理的身份，避免丢失在途容器。
宿主强杀可能留下仍运行的容器和任务；doctor 只诊断孤儿，不自动重用或删除。

## Pi 参考与镜像

参考 [pi-container-sandbox](https://github.com/yandy/pi-packages/tree/e12ff41b849c8fbbf9050a0bb8c79b7ae2ca13c1/pi-container-sandbox)
的 runtime.ts、ops.ts、session.ts、docker/gh.Dockerfile：惰性长驻、`/workspace` bind、
docker exec、cap-drop/NNP。上游为 MIT；本轮独立 Python 实现，没有复制其源码。
未照搬固定 UID 1000、按工作区名称复用容器、宿主环境 Shell 展开、Shell passthrough 或重型工具镜像。
Windows 已有 Pi MIT/归属文件保持不变。

| 项目 | 实测/固定内容 |
|---|---|
| WSL | Ubuntu 24.04.1 LTS，kernel 6.18.33.2-microsoft-standard-WSL2 |
| 用户 | archer，UID/GID 1000:1000，普通用户 |
| Docker | Desktop 4.56.0，Linux Engine 29.1.3，unix:///var/run/docker.sock |
| 宿主 Linux Python | 3.12.3，独立 venv |
| 镜像 Python / pytest | 3.12.14 / 8.3.5；另含 bash、Git、ca-certificates |
| Base | `python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9` |
| Tag | `nexus-sandbox:2927d7ed4632f291e3b2` |
| Image ID | `sha256:3de2340e62c73194134e4951f2a628b6f75e604e355c3ac79abc976697f2b2cf` |
| Docker inspect Size | 83,458,549 bytes（不等同 Docker 总磁盘占用） |
| Worker protocol | `nexus-patch-ack-v2` |
| 源码/recipe 指纹 | `2927d7ed4632f291e3b265405f497de1537d3904dda2927d00296521e9298f04` |
| 最终 Wheel SHA256 | `8368871b244915baeabfbdf226e677902f27ab88106928f7530df9b15388ed7e` |

源码/recipe 指纹用于协议兼容校验，不声称 apt 仓库内容完全可重现。运行按已验证的不可变 Image ID
创建容器；自定义镜像也必须匹配协议和指纹，镜像及 Docker daemon 属于可信安装边界。
普通工具调用 `--pull=never`；只有显式 setup 可下载/构建，不修改 Docker 全局配置或宿主系统依赖。

## 测试与原始证据

全部记录位于 [evidence/stage4-linux-docker](../next/evidence/stage4-linux-docker/)。
真实脚本：[stage4_linux_acceptance.py](../../scripts/stage4_linux_acceptance.py)，使用独立安装 Python `-I`。

| 验证 | 结果 | 证据 |
|---|---|---|
| 最终真实 Docker | 63 PASS / 0 FAIL | `real-03.json`、`audit/linux-real-03.jsonl` |
| 最终 WSL 专项及 Patch/Native 通用回归 | 142 PASS / 2 SKIP（Windows 系统 PowerShell/Node 用例） | `linux-unit-final.log` |
| 最终 Windows 全量 pytest | 975 PASS / 10 SKIP（原有 opt-in evaluation Docker） | `pytest-delivery.log` |
| Windows Native 真实回归 | 50 PASS / 6 FAIL（既有 0700/pytest TEMP 限制） | `windows-native-01.json/.log`、`audit/windows-native-01.jsonl` |
| Local Linux 原执行器 | Shell 三次、Patch 均 PASS；未导入 Docker 模块 | `local-linux.json` |
| Ruff / mypy | PASS；118 source files 类型检查 | `ruff.log`、`mypy.log` |
| Wheel 与双平台独立安装 | PASS；13 个相关已安装源码与 checkout 哈希一致 | `*-wheel-manifest.json`、`*delivery*.log` |
| 最终只读 doctor | 正确镜像，剩余 Nexus 容器 0；实际 mount probe 明确 NOT RUN | `doctor-delivery.log` |

真实 63 项包含：RO/WW Shell 及持久文件创建/覆盖/删除；Python 0700、TemporaryDirectory、
pytest tmp_path/tmp_path_factory；Patch 创建/修改、宿主可见性和 UID/GID；Git init/add/commit；
nonroot、CapEff=0、NoNewPrivs=1、凭据环境过滤、无 socket、工作区外宿主文件不挂载；
bridge HTTPS 成功、none 同请求失败；容器复用和不同会话隔离；正常退出、Timeout/Cancel 子孙进程
延迟写入不出现；缺 ACK/审计 sink 失败不写；多文件取消的完成事实；一次性 Patch Timeout/Cancel；
实际容器选项被改后的 fail-closed；缺镜像/不兼容镜像；初始化取消；杀自身 docker exec CLI 后清理；
独立宿主进程强退后准确诊断并核对 labels 清理该测试孤儿。

Docker 不可用测试使用该 Backend 专用的不存在 unix endpoint，没有停掉用户 Docker。
Patch timeout 测试把宿主 deadline 缩至 0.7 秒以覆盖等待 ACK 中断，仍运行真实 Worker/容器；
产品内部默认 deadline 为 120 秒。测试只创建独立工作区，不修改系统目录 ACL。
最终 runtime 实测后仅增加 Windows Docker 字段拒绝标志和对应测试；Linux Worker/执行路径及镜像指纹未变。
最终 Wheel 已重新安装并核验，最终全量与专项测试使用最新代码。

### 保留的失败和迭代

- `unit-01.log`：误指定不存在的 `tests/test_patch.py`，未运行测试；改用实际测试文件后通过。
- `real-01.json`：48/49 PASS，取消断言误写为 `cancel`，实际既有错误码为 `cancelled`；
  原结果已显示 cleanup_incomplete=false，测试确认容器消失/无延迟写入。仅修正契约断言，未忽略错误。
- `real-02.json`：57/57 PASS；随后补充 Shell 完成帧、CLI 意外丢失和更多生命周期验收，形成最终 63/63。
- Windows 6 项失败为 RO 第 0/2 轮及 WW 第 1 轮的 pytest 与 Python mkdir(0700) 子文件写入，
  保留 PermissionError/SDDL 原始记录，未修补 Token/ACL 原语、放宽检查或伪装为通过。
- 首次真实执行的自动审批曾超时，重试后执行；这是工具调度失败，不算一次隔离验收。
  写文件遇到当前工具沙箱拒绝时以已授权普通用户重试，没有管理员提权或 ACL 修复。
- WSL Git 无法解释 Windows worktree `.git` 指针：未改 Git 元数据；Windows 读取提交 SHA，
  Linux 对照该 manifest 和同一源码逐文件验证。一次只读镜像元数据查询误写 tag，已改为 manifest 实际 tag。

## 性能（毫秒）

来自 `real-03.json`，单机小样本；当时 Windows Native 回归同时运行，不能作为稳定基准或吞吐承诺。

| 步骤 | Workspace-write | Read-only |
|---|---:|---:|
| Docker 环境检查 | 329.45 | 354.73 |
| 镜像验证 | 44.10 | 40.59 |
| 容器创建/启动 | 227.25 | 249.86 |
| 冷 Shell 总计 | 725.69 | 760.34 |
| 热 Shell 三次 | 154.60 / 135.71 / 152.51 | 142.93 / 157.64 / 232.51 |
| Patch 创建 / 修改 | 288.22 / 236.67 | 默认拒绝；Allow once 独立容器 |
| 正常清理 | 308.65 | 299.04 |

Local Linux 在 Native 回归结束后测得 Shell 1.37/0.99/0.98 ms、Patch 0.52 ms。
冷调用成本主要是 Docker 检查和首次创建/启动；热调用实际复用容器，但仍进行身份/配置校验及 docker exec。
没有为提速去掉这些校验。镜像首次构建约 58 秒，后续构建复用缓存，构建时间不计入工具延迟。

## 安装、人工验收与清理

本机已准备独立候选安装和人工工作区，未修改用户模型配置/密钥：

```bash
# 从 Windows 进入 WSL；下列其余命令在 Ubuntu 中执行
wsl -d Ubuntu
cd /home/archer/nexus-stage4c-20261027/work
/home/archer/nexus-stage4c-20261027/venv/bin/nexus sandbox doctor
/home/archer/nexus-stage4c-20261027/venv/bin/nexus --permission-mode workspace-write
```

交互启动仍需用户在 WSL 按 Nexus 原方式配置 `~/.nexus/config.toml` 模型及对应 API Key 环境变量。
宿主模型调用不进入容器；`network=none` 仅限制工具容器。
其他 Linux 机器需先具备非 root Docker 访问权限、安装同一 Wheel，再显式运行 `nexus sandbox setup`。
不要在 Nexus 安装目录运行受限工作区。doctor 只做挂载路径资格检查，实际 bind 由惰性创建校验；
若 Docker 或镜像缺失，工具失败并给出 setup 提示，不回退 Local。

用户级配置示例（Linux）：

```toml
[security]
mode = "read-only"
docker_network = "bridge" # none 禁止工具常规外网
docker_cpus = 2.0
docker_memory_mb = 1024
docker_pids = 128
```

人工验收建议：`/permissions` 检查 Linux Docker、初始化状态和网络；WW 执行 `pwd`、`id`、
Shell/Patch 编辑、Git，再执行 `python -m pytest -q -p no:cacheprovider test_temp.py`。
示例 test_temp.py 已放在上述独立人工工作区，验证 0700/TEMP/pytest。
切换 RO 后验证读成功及创建/覆盖/删除拒绝；批准一次精确 Patch 后，再次确认后续 Shell 仍不可写。
Timeout/Cancel 后确认旧会话不可继续执行，退出后 doctor 不应列出该会话容器。
Windows PowerShell 的候选入口为 `D:\WorkSpace\nexus-stage4c-windows-20261027\Scripts\nexus.exe`，
继续使用原 Native 用户配置；不要加入 Linux Docker 配置字段，也不要把已知 Python 限制视为修复。

正常退出由 Backend 移除容器。崩溃后先运行 doctor，再人工检查准确 ID：

```bash
docker inspect EXACT_CONTAINER_ID
# 核对 io.nexus.sandbox.* 标签、UID、机器/boot、PID/starttime、workspace 和 owner 已退出后：
docker rm -f EXACT_CONTAINER_ID
```

不要批量 prune，不删除无法确认归属的容器，不复用崩溃容器。日志可通过 Nexus JSONL、doctor 和
docker inspect 交叉核验。外部用户停止 Engine 时，无法确认清理就是不确定状态，不声称已终止任务。

本轮两个专用 pytest TEMP 根已验证路径及无 reparse point 后删除，见 `cleanup.json`。
真实失败/最终审计工作区、Windows crash TEMP 和独立安装保留用于复查，见 `retained-artifacts.json`；
JSONL 已复制入版本化 evidence。最终没有遗留测试容器。历史构建镜像保留，不对用户 Docker 做全局清理。

## 适用范围与剩余验收

- 实测平台是本机 WSL2 Ubuntu + Docker Desktop Linux Engine；原生 Linux、其他内核、DrvFS、
  rootless daemon 和非本机 endpoint 没有获得本轮真实通过声明。macOS 受限模式不支持。
- Docker daemon、宿主调用者、安装和镜像可信；这不是多租户 VM 边界。bridge 可访问可达网络及宿主服务，
  不提供域名策略。工作区里本来就存在的凭据文件仍会随工作区暴露，环境过滤不能替代文件隔离。
- 宿主强杀/Engine 故障后的任务清理由诊断和人工处理补足；不实现 Durable Execution 或自动恢复重放。
- 原 Windows partial enforcement、网络不受限、持久 ACL 影响与 Python 0700 限制见
  [Windows Hardening 报告](stage4-windows-native-hardening.md)，本轮未扩大其修复范围。
- **Stage 4 双平台最终人工验收待用户执行。** 自动化通过不替代这一结论。
