# 开发、打包与发布

适用：Nexus 0.3.0。[发布说明](release-0.3.0.md) ·
[验收记录](release-0.3.0-validation.md) · [代码导读](engineering-walkthrough.md)

## 开发环境与检查

需要 Python 3.12+、uv；Bridge 功能测试需要 Node。使用系统临时目录，不在仓库根
建立 pytest basetemp，不修改用户代理配置来隐藏失败。真实服务测试需独立授权和凭据。

```sh
uv sync --frozen --dev
uv lock --check
uv run --frozen ruff check .
uv run --frozen mypy src tests
uv run --frozen pytest
git diff --check
```

平台类型可额外用 `uv run --frozen mypy --platform linux src tests` 检查，
但它不等于真实 Linux pytest。CI 在 ubuntu-latest 和 windows-latest 各运行完整套件。
10 项本地评测 Docker 隔离验收默认 opt-in；Linux 跳过 Windows 专用功能用例，
Windows 也不能证明 Linux Docker 真实隔离。跳过项按测试输出说明，不记作已验证。
Ruff 是当前仓库格式/静态风格门禁，不对历史源码做整仓格式重写。

本地八项集使用 `nexus eval --all`，需要源仓库 evaluation/ 数据、固定 Docker 镜像、
配置及真实模型，不能当普通 pytest 顺手运行。ABK FeatureBench 是独立项目与官方 verifier，
此发布准备不对正在运行的批次做任何修改。离线交互演示：

```sh
uv run --frozen python scripts/stage8_ui_smoke.py
```

演示是夹具，不代表真实多模型协作或人工终端验收。

## 干净构建与资源核验

从核准提交的干净 checkout 或 `git archive` 导出构建，避免用户未跟踪文件进入 sdist。
版本在 pyproject.toml、src/nexus/__init__.py、uv.lock 中一致。版本变更后运行 uv lock，
检查仅预期条目变化；不要借发版隐式升级全部依赖。

```sh
uv build
uv run --frozen python scripts/package_smoke.py artifacts
```

检查 wheel/sdist 中全部源码与包资源逐字节匹配：角色 Markdown、tokenizer 压缩数据与
Jinja 模板、Windows JS/授权协议文件、Patch worker、HTML 模板，以及 sdist 必需说明/脚本。
该检查打印 artifact SHA256，不调用模型、不读取个人配置或启动沙箱。

Linux 示例：在源码目录外的新环境验证 wheel（替换为核准构建的绝对路径）：

```sh
uv venv --python 3.12 /tmp/nexus-wheel-check
uv pip install --python /tmp/nexus-wheel-check /absolute/dist/nexus_coding_agent-0.3.0-py3-none-any.whl
cd /tmp
/tmp/nexus-wheel-check/bin/python -I -m nexus --version
/tmp/nexus-wheel-check/bin/python -I -m nexus --help
/tmp/nexus-wheel-check/bin/python -I /absolute/scripts/package_smoke.py installed --expected-version 0.3.0
uv pip check --python /tmp/nexus-wheel-check
```

Windows 使用本次独占的 `$env:TEMP` 子目录及其 `Scripts/python.exe`。
`-I` 避免源码/PYTHONPATH 伪装成已安装包；检查 runtime version、发行元数据和
site-packages 路径，实际加载两个 tokenizer 与模板。CI 还验证 `uv tool install`。
成功后只清理自己创建、已核实归属且非链接的临时目录；失败证据先归档。

CI 上传的 candidate-ubuntu-latest / candidate-windows-latest 只是 Actions 构建产物，
不是 GitHub Release 或 PyPI 发布。二者可能因平台换行产生不同字节，分别记录哈希。

## 工作流与发布边界

CI push 覆盖 main、next、codex/nexus-v0.3、codex/nexus-v0.3-*；PR 均检查。
质量检查成功不自动发布。`.github/workflows/release.yml` **仅在 Release published**
时进入 PyPI 流程，要求标签精确为 `v<pyproject version>`，随后检查测试、构建、资源、
已安装包版本及依赖。PyPI 使用 pypi environment 与 OIDC Trusted Publishing。

发布前：分阶段保留提交历史；Stage8 → V0.3 → main，优先快进，分叉正常合并；
核实 main 精确 SHA 与其双平台 CI。不得 squash 历史、force push，或提前创建标签。

正式发布需先停在 main 候选，待维护者检查并明确核准后才执行以下步骤。
0.3.0 已按此流程发布；结果见[发布验收记录](release-0.3.0-validation.md)。

1. 再 fetch 核对 main 与获准 SHA；若候选变化，说明差异并重新确认。
2. 从获准 SHA 创建并推送 `v0.3.0` 标签，Release 内容使用本页链接的 Release Notes。
3. 发布 GitHub Release；这一步会触发 PyPI，不是普通草稿准备操作。
4. 等待 release workflow、OIDC 上传成功，核对 GitHub 附件/版本与 PyPI 元数据。
5. 在新的源码外环境从公开 PyPI 索引安装精确 0.3.0，验证 version/help、资源与依赖。
6. 更新发布状态和证据。失败时保留日志，不覆盖/复用已上传的同版本文件。

不在准备阶段运行上述发布动作。失败检查按真实 PASS/FAIL/NOT RUN 记录，
不通过改预期、跳过断言、替换评测题目或修改预算制造通过。
