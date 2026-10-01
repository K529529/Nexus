# Nexus Next V0

[中文](README.zh-CN.md)

A small local coding agent: one hand-written message loop, native function calling,
and two native tools, `exec_command` and `apply_patch`. The model explores, edits,
runs tests, repairs failures, and decides when to finish. No database or repository
index is required. Explicitly configured stdio MCP servers can supply additional tools.

This branch implements the approved Next V0 design. See
[acceptance evidence](docs/next/acceptance-evidence.md) for actual PASS/FAIL/NOT RUN
results. A model's `completed` outcome is not proof that tests passed or that a
benchmark instance was resolved. Version `0.2.0` is a local candidate, not a claim
that this version has been published to PyPI.

## Install

Python 3.12+; Windows PowerShell or Linux `/bin/sh`.

```sh
uv build
python -m pip install dist/nexus_coding_agent-0.2.0-py3-none-any.whl
# Or install the same wheel as an isolated command:
uv tool install dist/nexus_coding_agent-0.2.0-py3-none-any.whl
```

For development: `uv sync --frozen --dev`, then `uv run --frozen nexus`.
Start in the project root: the starting directory is the workspace. Nexus only
automatically reads that directory's `AGENTS.md`; it does not find the Git root,
load nested instructions, scan files, or build a repository summary at startup.

## Configure

Run `nexus` in a terminal for first-use setup, or create `~/.nexus/config.toml`.
The wizard asks for an environment variable **name**, never the key value.
It preserves other TOML sections and does not overwrite invalid TOML.

```toml
[model]
name = "your-tool-capable-model"
base_url = "https://your-service.example/v1"
api_key_env = "NEXUS_MODEL_API_KEY"
context_window = 32768
max_output_tokens = 8192
include_usage = true
# reasoning_effort = "high"

[runtime]
max_steps = 40

[execution]
output_limit_bytes = 32768
# shell = "C:/Program Files/PowerShell/7/pwsh.exe"

# [mcp.servers.example]
# command = "/absolute/path/to/already-installed-server"
# args = []
# env_from = { SERVICE_TOKEN = "MY_SERVICE_TOKEN" }
```

Set the key in the same terminal before starting:

```powershell
$env:NEXUS_MODEL_API_KEY = '<key>'
nexus
```

```sh
export NEXUS_MODEL_API_KEY='<key>'
nexus
```

Use your service's actual context limit. `context_window` must exceed
`max_output_tokens + 1024`. `NEXUS_MODEL_NAME` and `NEXUS_MODEL_BASE_URL` override
only the corresponding model fields. Repository `.env` and `.nexus/config.toml`
are never loaded. Old Nexus configuration sections (including `observability`)
are unsupported: preserve a backup and remove those sections before using Next.

The tested cloud profile is Alibaba Cloud's direct `deepseek-v4.1-flash`, Chat
Completions, `reasoning_effort = "high"`, `context_window = 1000000`, and
`max_output_tokens = 32768`. Configure the Base URL for your account/region and
`api_key_env = "DASHSCOPE_API_KEY"`; no endpoint or key is hard-coded into the runtime.

## Work and resume

```sh
nexus
nexus exec "Fix the empty-input bug and run the existing tests"
nexus exec "Explain this test failure" --json
nexus resume
```

Interactive commands: `/new`, `/resume`, `/help`, `/exit`. The selector shows
task title, project, update time and status; use arrows/Enter or Esc. Ctrl+C
aborts the active turn with bounded process cleanup; at an idle prompt it clears
input. Non-interactive callers must use `exec`. Exit codes are 0 completed,
1 failed, 2 configuration/arguments, 3 limited, and 130 aborted.

The default transcript shows short operation summaries and status, without dumping
shell/MCP output. Failures show up to four excerpt lines; patches show at most three
file summaries with short diff previews. Assistant progress and final answers remain
visible. This only changes presentation: model observations and session records retain
the tool-budgeted results. Use `nexus exec "task" --json` for detailed public events.

Sessions are append-only JSONL under `~/.nexus/sessions/<workspace-key>/` with one
writer per session. Resume restores conversation and waits for a new user request.
It never reruns old commands. Missing results after interruption become
`interrupted_unknown`; inspect actual files/process state before retrying. A
corrupt final line is recovered into a new file while preserving the original;
middle corruption is rejected. Each complete record is flushed; this is not a
durable transaction or an exactly-once execution guarantee.

Execution is trusted local execution with your account's permissions. Shell/MCP
can access files and networks available to that account. Patch writes are confined
to the workspace, which does not sandbox the other tools. Shell output has a shared
head/tail budget, and timeout/nonzero exit/patch conflicts are observations for the
model. `exit_code` is the shell's exit code (PowerShell does not always return its
child process's numeric code). A cleanup failure stops the turn visibly.

`apply_patch` accepts UTF-8 unified diffs only, with `a/` and `b/` file headers and
`/dev/null` for create/delete. No rename, binary or mode changes. All hunks are
preflighted; individual replacement is atomic, but a multi-file I/O failure can
leave an explicitly reported partial result.

Public JSON events omit private continuation fields and redact configured known
credentials. Local session history contains user input and source/tool content;
it is not a general secret detector. Current tested provider continuation works
without retained reasoning. The model adapter handles protocol fields, not the loop.

## Develop and inspect

```sh
uv run --frozen ruff check .
uv run --frozen mypy src tests
uv run --frozen pytest
uv lock --check
uv build
git diff --check
```

CI runs offline behavior tests on Windows/Linux with Python 3.12. Git is needed
for the evaluation collector tests, not for ordinary Nexus startup. Cloud live
tests are separate from paid CI. Read `src/nexus/core/agent.py` for the loop,
`core/model.py` for the wire adapter, `tools/` for execution, and `app/` for local
sessions and terminal consumers. Four direct runtime dependencies: OpenAI SDK,
MCP SDK, prompt_toolkit and Rich. SDK transitive dependencies are listed in `uv.lock`.

## Official SWE-bench integration

The optional developer script has only `predict` and `collect`; it does not grade.
Install the official harness in a separate Linux/Docker environment. Prepare clean
inference workspaces externally, at each task's exact base commit with dependencies.
Tasks JSONL fields: `instance_id`, `repo`, `base_commit`, `problem_statement`,
`workspace`. Do not expose official patches/tests/answers to the agent.

An environment JSON record must contain `dataset`, `dataset_revision`, `network`
and `contamination`. Record access to future Git history and mounted answers; a
clean Git status alone does not establish an uncontaminated benchmark environment.

```sh
python -m pip install swebench==5.0.2

uv run --frozen python scripts/swebench_v0.py predict \
  --tasks tasks.jsonl --output /outside-inference/artifacts \
  --environment-record environment.json

python -m swebench.harness.run_evaluation \
  --dataset_name SWE-bench/SWE-bench_Lite \
  --predictions_path /outside-inference/artifacts/predictions.jsonl \
  --instance_ids <verified-instance-id> --max_workers 1 --run_id <new-run-id>

uv run --frozen python scripts/swebench_v0.py collect \
  --runs /outside-inference/artifacts/runs.jsonl \
  --harness-root logs/run_evaluation/<new-run-id> --harness-version 5.0.2 \
  --output official-results.jsonl
```

Select the official dataset containing your verified instance. The collector
compares final files against the original base commit using a temporary Git index,
including model-committed changes and unignored new files. It preserves the user's
index. Reports are linked by instance, run, and exact patch hash; missing reports
remain unknown. Keep raw reports and use a fresh harness run ID for every new patch.
See the [official evaluation guide](https://www.swebench.com/SWE-bench/guides/evaluation/).

Harness 5.0.2 requires the `image` and evaluation fields in the current official
`SWE-bench/SWE-bench_Lite` dataset; the older `princeton-nlp` copy lacks those fields.
Record the exact dataset revision. For a pinned local run, the official harness also
accepts an unmodified JSON export of its dataset rows as `--dataset_name`; verify
the instance, original base commit and problem text against the inference task.
The script does not manufacture missing grader fields. A patch containing a known
configured credential is not exported, and the run records a collection error.

## Design authority

- [Frozen architecture](docs/next/architecture-contract-v0.md)
- [Approved development design](docs/next/01-development-design.md)
- [Delivery and acceptance checklist](docs/next/02-delivery-and-acceptance.md)

Old Day workflows, graph nodes, Plan authorization, databases, retrieval, Skills
and the old evaluation platform have been removed from this development branch.
