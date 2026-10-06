# Nexus 0.2.0 (Next)

[中文](README.zh-CN.md)

A small local coding agent: one hand-written message loop, native function calling,
and two native coding tools, `exec_command` and `apply_patch`, plus `update_plan`
for progress tracking and `load_skill` for optional local guidance. The model explores, edits,
runs tests, repairs failures, and decides when to finish. No database or repository
index is required. Explicitly configured stdio MCP servers can supply additional tools.

The Next rewrite keeps the `nexus-coding-agent` package name and `nexus` command. See
[acceptance evidence](docs/next/acceptance-evidence.md) for actual PASS/FAIL/NOT RUN
results. A model's `completed` outcome is not proof that tests passed or that a
benchmark instance was resolved. See the [0.2.0 release notes](docs/next/release-0.2.0.md)
for changes, migration and verification boundaries. The release checklist records publication status.

## Install

Python 3.12+; Windows PowerShell or Linux `/bin/sh`.

After 0.2.0 is available on PyPI, install it as an isolated command:

```sh
uv tool install "nexus-coding-agent==0.2.0"
nexus --version
```

Alternatively, use `python -m pip install "nexus-coding-agent==0.2.0"` in a dedicated
virtual environment. Before publication, build and install the local wheel:

```sh
uv build
uv tool install dist/nexus_coding_agent-0.2.0-py3-none-any.whl
```

When upgrading an existing uv tool installation, add `--force`. Check `Get-Command nexus -All`
(PowerShell) or `command -v nexus` (Linux) if an older installation shadows the new command.
Back up `~/.nexus/config.toml` before upgrading from 0.1.0; use the minimal configuration
below. Old database sessions and legacy Skills are not migrated. Model API calls are billed
by your provider; installing Nexus does not include API credits.

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
# request_timeout_seconds = 120  # Entire model response, 1..600 seconds
# output_token_parameter = "max_completion_tokens"  # Opt in if supported by your service
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

`output_token_parameter` selects `max_tokens` (compatible default) or
`max_completion_tokens`. For services where `max_tokens` excludes reasoning,
select a supported total-completion parameter so the provider cap includes both
reasoning and the answer. `max_output_tokens` supplies the same value to that
parameter and the Context reserve. Nexus does not guess from the model name or
retry with another parameter when a service rejects it. Verify enforcement with a
bounded provider probe: an accepted but ignored parameter is not a working cap.
See [live parameter evidence](docs/next/cost-hardening.md).

Use your service's actual context limit. `context_window` must exceed
`max_output_tokens + 1024`. `NEXUS_MODEL_NAME` and `NEXUS_MODEL_BASE_URL` override
only the corresponding model fields. Repository `.env` and `.nexus/config.toml`
are never loaded. Old Nexus configuration sections (including `observability`)
are unsupported: preserve a backup and remove those sections before using Next.

The current cost-first Bailian candidate is `qwen3.8-flash`, reasoning `low`,
16,384 total completion tokens, a 300-second request deadline, and 40 model steps.
One full round achieved 6/8 PASS and 8/8 autonomous completions at about CNY 1.54
in reported-token cost; this is not evidence of stable 8/8. Exact settings and
remaining failures are in the [hardening report](docs/next/suite-hardening-evidence.md).

The initial small-task acceptance used `deepseek-v4.1-flash`, Chat
Completions, `reasoning_effort = "high"`, `context_window = 1000000`, and
`max_output_tokens = 32768`. Configure the Base URL for your account/region and
`api_key_env = "DASHSCOPE_API_KEY"`; no endpoint or key is hard-coded into the runtime.
This historical profile is not a general reliability recommendation. Later Requests/Sphinx
runs used different output/reasoning settings and exposed edit/finish failures; see
[execution-budget evidence](docs/next/execution-budget-evidence.md).
The [subsequent model comparison](docs/next/model-delivery-evidence.md) records Qwen's
Requests PASS with autonomous completion and a Sphinx PASS interrupted by service access denial.
After service access was restored, broader low-cost model comparisons continued;
see [current eight-case results and limitations](docs/next/suite-hardening-evidence.md).
The earlier Max candidate is not the selected default or a reliability guarantee.

## Work and resume

```sh
nexus
nexus exec "Fix the empty-input bug and run the existing tests"
nexus exec "Explain this test failure" --json
nexus resume
```

Interactive commands: `/new`, `/resume`, `/skills`, `/skill <name>`, `/skill off`, `/help`, `/exit`. The selector shows
task title, project, update time and status; use arrows/Enter or Esc. Ctrl+C
aborts the active turn with bounded process cleanup; at an idle prompt it clears
input. Non-interactive callers must use `exec`. Exit codes are 0 completed,
1 failed, 2 configuration/arguments, 3 limited, and 130 aborted.

The default transcript omits per-round model-start messages. Ordinary successful tools
leave one summary line with status and duration; a terminal shows a temporary activity
line while a tool runs. Read/search/test/Git labels are display hints only; compound or
long commands use "Run shell command". No shell/MCP output is dumped. Failures show up
to four excerpt lines; patches show at most three
file summaries with short diff previews. Assistant progress and final answers remain
visible. This only changes presentation: model observations and session records retain
the tool-budgeted results. Use `nexus exec "task" --json` for detailed public events.

One conversation reuses its model client, tool registry, and MCP connections across
turns. `/new`, selecting a resumed conversation, or exiting closes those resources;
a new conversation connects lazily on its first task. Failed or interrupted MCP servers
stay disabled until a new conversation. Configuration is reloaded for new conversations,
not hot-reloaded between turns. Internal callers must await `Conversation.close()` in
the same async task that runs its turns.

Sessions are append-only JSONL under `~/.nexus/sessions/<workspace-key>/` with one
writer per session. Resume restores conversation and waits for a new user request.
Each ordinary input starts an isolated run: the model sees current system/repository
instructions, the current request and that run's messages. Previous runs stay in JSONL.
Use `/resume` or `nexus resume` to select a session whose latest run is unfinished
(interrupted, aborted, failed or limited); the next input continues that run's context.
A completed session's next input starts a new run. Run IDs remain internal metadata;
the selector resumes only the latest unfinished run. Context Runtime V0.2.1 retains run
isolation and safety compaction. At 85% of the projected input budget, it summarizes old
complete groups in the current run's active context, aiming for roughly 60%. Original
messages stay in session history and JSONL; resume reconstructs the saved projection.
If the provider reports a context limit before compaction was attempted at that boundary,
Nexus attempts compaction and retries once. Unrecoverable overflow returns
`limited/context_limit`. Tool observations stay FULL until a later validated assistant message
is appended. Consumed results outside a token-budgeted recent working set receive deterministic
`compact-v1` previews at request time. Session/JSONL retain original messages; safety snapshots
retain logical references, and HOT tool groups cannot be summarized. Projection diagnostics are
available in JSONL/profile metrics without extra TUI output. Soft compaction and further history
reduction remain future work. See [V0.2.1 evidence](docs/next/context-runtime-v0.2.1-evidence.md).
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

`apply_patch` uses the Nexus `*** Begin Patch` format with Add/Update/Delete File
operations and context-based `@@` chunks, without line numbers or hunk counts.
Legacy UTF-8 unified diffs remain accepted. No rename, binary or mode changes.
Ambiguous context is rejected. All files are preflighted; individual replacement
is atomic, but a multi-file I/O failure can leave an explicitly reported partial result.

`update_plan` replaces a run's progress list with pending/in_progress/completed items;
at most one item can be in progress, and an empty list clears it. The committed list
is restored on resume and projected into each request without accumulating history.
Plan completion does not prove correctness or end the Agent. The stagnation detector
can send one progress reminder per execution window; it does not force an edit.
The request also shows the remaining model-turn budget. These are model guidance,
not guarantees of progress or timely completion.

Public JSON events omit private continuation fields and redact configured known
credentials. Local session history contains user input and source/tool content;
it is not a general secret detector. When a provider returns `reasoning_content`,
the adapter retains it as private, service/model-bound continuation data in the local
session and sends it on subsequent requests. Public events omit it; changing the
service/model requires a fresh session rather than reusing those private fields.

## Develop and inspect

Opt in to a developer report after each run:

```sh
nexus --profile
nexus exec "Explain the repository structure" --profile
```

With the existing Windows development environment, use
`.\.venv\Scripts\nexus.exe --profile` or
`.\.venv\Scripts\nexus.exe exec "Explain the repository structure" --profile`.
The normal TUI is unchanged without the flag; `--profile` and `--json` are mutually
exclusive. The report is a derived human-readable summary; JSONL remains the canonical
full trajectory, and the report prints its actual path. Cumulative tokens include all
model calls, while active-context first/peak/final input uses successful normal calls
and excludes compaction calls. With incomplete usage, known token subtotals show `≥`
and usage coverage; a field with no reported values stays `unknown`. Successes, failed
attempts and explicit retries are counted separately. Metrics reset for
each run, including a resumed execution. See the
[offline example and metric definitions](docs/next/developer-run-profiler-v0.1-evidence.md).

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
sessions and terminal consumers. Five direct runtime dependencies: OpenAI SDK,
MCP SDK, prompt_toolkit, Rich and PyYAML (safe Skill metadata parsing). SDK transitive dependencies are listed in `uv.lock`.
For a code-based explanation of the design, tradeoffs and evidence boundaries, see
the [engineering walkthrough](docs/next/engineering-walkthrough.md).

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

Old Day workflows, graph nodes, Plan authorization, databases, retrieval, the legacy
Skill system and the old evaluation platform have been removed. The lightweight Skills
below are implemented through the same message loop and Tool Registry.


## Fixed development evaluation

From this source checkout, with Docker Desktop using Linux containers and the existing model configuration:

```sh
nexus eval pvlib__pvlib-python-1707
nexus eval --all
```

Evaluation automatically collects Profile metrics and prints a compact aggregate report including `FinalCtx` and `ToolResultBytes`. Artifacts live under `~/.nexus/evaluation/results/`. These are local Next Dev Set checks, not official SWE-bench scores. See [Evaluation V0](evaluation/next-dev-v0/README.md) for frozen environments and limitations, and [actual acceptance evidence](docs/next/evaluation-v0-evidence.md) for results.

[Autonomous hardening stage results and cost evidence](docs/next/suite-hardening-evidence.md).

## Lightweight Skills

Install trusted, self-contained Markdown at `~/.nexus/skills/<name>/SKILL.md`.
The [pytest regression example](docs/next/skills/pytest-regression/SKILL.md) can be
copied manually; examples are not automatically installed or activated.

```text
nexus skills list
nexus --skill pytest-regression
nexus exec "Fix the bug with regression coverage" --skill pytest-regression
```

Inside the prompt, `/skills` refreshes availability and shows selection/loading status;
`/skill <name>` selects or replaces the session default; `/skill off` clears that explicit
selection and restores automatic mode; `/new` clears the old session's selection.
The model can use the ordinary `load_skill` tool for task-specific loading. Automatic
loads last only for their run. Resume restores saved bodies, even if files change.
Skills do not execute scripts or grant permissions; their input cost is budgeted.
Fixed evaluation does not discover personal Skills.

The resume selector displays nine recent entries per page, showing the latest user
input, UTC update time and outcome. Use arrows/Enter or digits 1-9; Esc cancels.
See [Skills design, limits and rollback](docs/next/skills-v0.1.md).

## License

[MIT](LICENSE).
