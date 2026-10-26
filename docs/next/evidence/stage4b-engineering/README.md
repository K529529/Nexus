# Stage 4B evidence index

本目录仅记录正常工程验收；无模型调用、付费 Benchmark 或逃逸 PoC。
真实测试使用独立临时项目与外部 wheel 安装，不操作真实 Nexus/FilePilot 项目内容。
`scripts/stage4b_srt_acceptance.py` / `scripts/stage4b_hitl_acceptance.py` 是 opt-in 实机入口，
不由普通 pytest 自动运行。

## 自动化记录

- `pytest-01.txt`：4 failed / 892 passed / 10 skipped。旧故障注入 fixture 返回空初始化结果，
  与新增 `initialized=true` 握手不符；修正 fixture，保留原故障注入断言。
  `pytest-01.raw.txt.gz` 保存未经改写的原始字节；可读 txt 仅规范换行和去除行尾空白。
- `pytest-02.txt`：899 passed / 10 skipped。
- `pytest-03.txt`：901 passed / 10 skipped，新增 holder/其他资源关闭验证。
- `pytest-04.txt`：902 passed / 10 skipped，新增 Patch 完成后立即关闭的 ACK 竞态回归。
- `pytest-05.txt`：902 passed / 10 skipped，审批 scope 收敛回归。
- `pytest-06.txt`：最终 903 passed / 10 skipped，新增 RO per-exec 拒绝契约回归。
- `focused-final.txt`：最后初始化预算调整后，Security / SRT Bridge / HITL 相关 60 项通过。
- `ruff-final.txt`、`mypy-final.txt`：最终静态检查；mypy 107 source files。
- `build-final.txt`、`install-final.txt`、`wheel-install.json`、`wheel-help.txt`：
  离线构建、独立安装、包内资源/哈希、CLI 帮助；没有替换全局 nexus。
- `local-performance.json`：每组 5 个实际 Shell 样本，direct 与 Gateway 的中位数对照；
  非正式统计实验，不以其推断普遍性能优势。

## 实机失败尝试保留

尝试编号连续保留，缺少 JSON 的尝试明确说明，不填造“原始文件”：

| 尝试 | 结果 |
| --- | --- |
| SRT 01 | 初始候选 wheel 缺 tomlkit，启动即失败，没有 JSON；终端错误用于安装修复 |
| SRT 02 | initialize 后受限 PowerShell 安装路径不可读，HRESULT access denied |
| SRT 03 | 默认 cwd 落到 C:\，项目 Get-Content 失败 |
| SRT 04 | AppData 祖先遍历拒绝；旧脚本错误后仍继续；Python/Patch 超时、Schannel 失败，终端 Unicode 编码失败；JSON 保留 |
| SRT 05 | 改为 D: 独立夹具，RO/WW/Python/pytest 成功，Patch 超时 |
| SRT 06 | ACK 文件 IPC + asyncio Worker 仍超时 |
| SRT 07 | 请求文件 IPC 但 asyncio Worker 仍超时；close 抛错导致无 JSON，不能宣称完整证据 |
| SRT 08 | 无事件循环 Worker 暴露真实 stdin 为空：Invalid request size |
| SRT 09 | 请求文件 + 同步 ACK Worker 成功，git/pytest/HTTPS 成功；旧 taskkill/T 无法清理另一账户进程，timeout cleanup_incomplete |
| SRT 10 | timeout/cancel/之后重新执行成功且 holder 正常退出；末尾宿主读取 patched.txt 失败，JSON 中 close 仍保留；原因是 mkdtemp 的 owner-only Windows ACL |
| HITL 01 | 用户拒绝未执行，用户批准已产生 patch_diff；最终 reset/exit 未确认，没有把已写文件称为完整成功 |
| HITL 02 | 有界诊断定位：`ENOTEMPTY`，IPC 目录残留 ACK 文件；结果返回和 shutdown 取消并发导致重新写 ACK |
| HITL 03 | ACK 生命周期修复后，SRT initialize 超过旧 120 秒预算；批准操作返回 backend_unavailable，未执行；不是网络策略拒绝的成功测试 |
| HITL 04 | 真实 deny / allow once / 不可复用 / 恢复 RO / 默认网络拒绝 / 正常关闭全部通过 |
| SRT 11 | 普通开发、host 读新文件、Git config/hooks 保护、HTTPS 通过；WW→RO 后再回 WW 被旧的 kept 根 deny 拒绝，整轮未通过 |
| SRT 12 | 正常开发、Git 保护、HTTPS、宿主读取，以及同目录 WW→RO→WW 全部通过；本轮明确不重复 stop 测试 |

HITL 02 终端诊断的具体目录是 `C:\Users\Archer\AppData\Local\Temp\nexus-srt-ipc-0kWBoa`，
仅残留 `0758ff9138b6486a8707d079288c74e6.ack.json`。这是实际终端观察的补充记录，
不是伪装成程序输出的 JSON。HITL 01/02 JSON 保留审批与 patch_diff，后续异常只有 interrupted_unknown。

固定版本 SRT 的 `dist/sandbox/windows-sandbox-utils.js` 中
`grantWindowsAclPersistent()` 自身允许 600000 ms；Nexus 最后将 initialize 总预算对齐为 600 秒。
工具执行 timeout 独立、不因该变化延长。失败时无 Local 回退，也不会自动重放工具。
SRT 11 后将 Read-only 整根 deny 从 initialize 转到官方 per-exec customConfig；
拒绝范围不变，RO 仍无 write grant，命令退出释放该 deny，不把它保留到下个 WW 会话。
旧失败夹具的 kept deny 保留，未运行 acl forget/recover；SRT 12 使用新的独立夹具验证。
HITL 04 之后还将 capability_scope 中重复的原始 Patch 收敛为工具名和摘要；原操作仍在
审批预览与原 tool-call 中，授权绑定不变。历史 JSON 不回写成新格式。

## 真实证据的解释规则

- `rows[].result` 是真实调用结果；单个脚本进程退出 0 **不等于全部检查通过**。
- `policy_denied` 是 Gateway 的可信策略拒绝，不能冒称 Windows OS 给出的拒绝分类。
- `command_exit_nonzero` 只有结合本条固定测试命令和 stderr 才能用于验收某个预期拒绝，
  产品不会把它泛化为可提权依据。
- `close.confirmed` 表示 reset 返回且 Node holder 正常退出。上游 Windows reset 的 ACL 恢复
  是 best-effort，并保留 write roots；该字段不能证明全部 ACL 已撤销。
- HITL 的 allow/deny 由脚本写入真实 TaskInput；SRT Backend 不 Mock，但没有人工/付费模型验收声明。
- 某轮 `NOT RUN` 不能拿该轮解释为测试通过；超时/取消的真实成功依据是 SRT 10。

## 临时目录

`temp-cleanup.json` 记录已核对范围、无 reparse 点后删除的通过测试目录。
失败 pytest 目录、失败实机夹具和候选安装保留，未修改 ACL 强行删除。
最终盘点记录随报告更新。SRT kept grants 按上游语义保留；未运行 acl forget/recover/windows-install。
早期未再使用的 C: wheel 环境已在验证边界和 reparse 点后删除，实际候选安装仍保留在 D:。
`temp-retained-final.json` 是最终保留目录清单；`process-cleanup-final.json` 记录 0 残留进程。
`hitl-02-residual-ack.json` 保留了失败 ACK 的原始内容，确认无 reparse 点后已删除原文件和空目录。

独立复核命令（无需 SRT 或模型调用）：

```powershell
.venv/Scripts/python.exe scripts/stage4b_verify_evidence.py docs/next/evidence/stage4b-engineering
```

实际输出 `verification-final.json` 为 PASS。该核对只证明文件中记录的功能事实，不是 OS 安全认证。
