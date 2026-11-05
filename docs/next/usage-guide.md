# Nexus 0.3 usage reference

[Quick start](../../README.md) · [中文](usage-guide.zh-CN.md) ·
[Release notes and limitations](release-0.3.0.md)

This describes Nexus 0.3.0. Historical designs and experiments remain available;
their old defaults and outcomes are not current recommendations.

## Configure

Only `~/.nexus/config.toml` is loaded (Windows: `%USERPROFILE%\.nexus\config.toml`).
There is no `--config` flag. Project `.env` and `.nexus/config.toml` are not loaded.
The first interactive wizard configures the main model; add the compaction model separately.
Back up and merge your configuration rather than overwriting model aliases, MCP or permissions.
Unknown fields are rejected.

Replace the placeholder models/endpoints with values supported by your service:

```toml
[model]
name = "your-coding-model"
provider = "openai"
base_url = "https://your-service.example/v1"
api_key_env = "NEXUS_MODEL_API_KEY"
context_window = 128000
max_output_tokens = 8192
request_timeout_seconds = 120
include_usage = true
# reasoning_effort = "high"
# output_token_parameter = "max_completion_tokens"

[compaction_model]
name = "your-compaction-model"
provider = "openai"
base_url = "https://your-compaction-service.example/v1"
api_key_env = "NEXUS_COMPACTION_MODEL_API_KEY"
context_window = 128000
max_output_tokens = 4096
# enable_thinking = false

[runtime]
max_steps = 40
response_language = "en"

[execution]
output_limit_bytes = 32768

[security]
mode = "local"

[memory]
enabled = true

[multi_agent]
enabled = false
allowNestedSubagents = false
max_agents = 3

[web]
enabled = false

[tracing]
enabled = false
```

Keys belong in named environment variables, never TOML or chat. Both configured model keys
must be available to the launching process. Restart old terminals after changing Windows
user variables. Reopen Nexus after editing configuration.

Supported providers are `openai`, `deepseek`, `dashscope`, through LiteLLM with no automatic
model fallback. Use `openai` for compatible custom endpoints. Provider acceptance of
`reasoning_effort`, `enable_thinking` and the selected output parameter is service-specific.
Nexus does not infer support from model names or silently switch parameters on rejection.
`request_timeout_seconds` is a model-response deadline, not a whole-task/ABK budget.

The main `context_window` is the managed input window; `max_output_tokens` independently
caps output rather than being subtracted from that window. Leave room for both within
your provider's real total capacity. Compaction also reserves its own output/safety budget.
A missing compaction model never falls back to the main model: tasks within capacity can
continue, but unavailable compaction/overflow is reported explicitly.

`NEXUS_MODEL_NAME/BASE_URL` and `NEXUS_COMPACTION_MODEL_NAME/BASE_URL` override corresponding
fields. `runtime.response_language` accepts `en`, `zh-CN` or `auto`.
Add `[models.alias]` with the same model fields; `/model` opens a keyboard menu,
`/model alias` switches directly, and `default` selects the startup model.
This does not replace the separate compactor or existing child model snapshots.
See [model implementation and evidence](stage7-observability-models.md).

### Install and upgrade

Install from PyPI in a dedicated Python 3.12+ environment:

```sh
python -m pip install --upgrade nexus-coding-agent==0.3.0
```

Alternatively, use `uv tool install nexus-coding-agent==0.3.0` or follow the
[source installation](../../README.md#quick-start). Back up config, sessions and memory before upgrading.
Verify `python -m nexus --version` and the executable selected by `Get-Command nexus`
(PowerShell) or `command -v nexus` (Linux); an old global executable may still run 0.2.

Keep compatible 0.2 model settings, add compaction, and explicitly opt in to new capabilities.
Legacy `observability`, SRT-only and domain-policy settings are not current tracing/platform
configuration. Old sessions without durable evidence remain conservatively recoverable;
upgrading does not make unknown side effects replayable.
For 0.1 migration, see the [historical migration notes](release-0.2.0.md#install-and-migrate).

## Commands and input

| Action | Command / key |
| --- | --- |
| Interactive / one task | `nexus` / `nexus exec "task"` |
| Public events / profile | `nexus exec "task" --json` / `nexus --profile` |
| Resume this workspace's saved session | `nexus resume` or `/resume`, then select |
| New conversation / compact | `/new` / `/compact` |
| Model / permissions | `/model [alias]` / `/permissions` |
| Memory | `/memory [status\|on\|off]` |
| Skills | `/skills`, `/skill name`, `/skill off` |
| Help / exit | `/help` / `/exit` |

`--json` and `--profile` are mutually exclusive. Resume has no positional Session-ID
argument. Override permissions for one invocation with
`nexus exec "task" --permission-mode read-only`; choices are
`local`, `read-only`, `workspace-write`.

While running, **Enter sends guidance; Tab queues a subsequent root task**. No guidance
command is required. Delivery waits for a complete tool-batch boundary; received and
delivered are distinct. `/pending`, `/edit ID text`, `/withdraw ID` apply to pending
input for the selected agent. Missing arguments/unknown IDs give feedback without stopping
input. Delivered input cannot be edited. Queued text is a task even if it begins with "/".

F4, Alt+Enter or Ctrl+J insert a newline. F2 toggles long-paste capture; F3 expands a paste
block. Bracketed paste is supported; completion menus take precedence over Tab queueing.
Ctrl+C cancels the active run and waits for tool cleanup. PgUp/PgDn browse history;
Ctrl+End returns to latest output. New output does not force a reader to the bottom.
Native terminal/IME behavior still needs manual acceptance.

## Optional Multi-Agent

`multi_agent.enabled` defaults to false. Enable it explicitly for new root sessions.
The default capacity is **3 active descendants**, excluding root; waiting for decisions
and cancellation cleanup still consume capacity. Nesting defaults off; when enabled,
the maximum depth is root → child → grandchild. Every instance reuses the same Agent Loop.

scout defaults to low reasoning, worker/reviewer to high. Models inherit the creator's
current model unless overridden by `[multi_agent.roles.scout]` (or worker/reviewer) with
`model = "alias"` and `reasoning_effort`. scout/reviewer lack apply_patch, but Shell
access is not a read-only security boundary. All roles retain Gateway enforcement.

Children have Fresh context: task/background/scope, not the parent's whole conversation.
They keep their own histories and share the workspace; assigned scope is not a file lock.
Avoid overlapping edits. MCP is absent from children by default; `mcp_servers` grants only
the intersection of role configuration and parent permission.
See [full configuration and role/nesting rules](stage8-usage.md).

F6 or `/agents` opens the task/role/short-ID/status selector.
`/agents ID` and `/agent ID` enter a child conversation; Esc or `/agent root` returns.
Enter guides the selected child; Tab still queues root work. Completed children are
read-only, not automatically revived. The current tree keeps active/actionable agents;
archived finished agents appear under `/agents history`.
`/agent detail ID` shows trajectory; `/agent cancel ID` cancels the subtree.
Cumulative token display covers **the entire session, root plus all historical children**.

## Context and memory

Runs share continuous session history until compaction; `/new` creates a fresh session.
Compaction triggers near 90% of the managed input window, retains roughly 20K recent tokens
in complete message/tool groups, and can occur before requests, during finalization,
through `/compact`, or once after provider overflow. Raw JSONL remains unchanged.
Semantic summaries can be wrong even when structurally valid; deterministic Runtime Facts
preserve execution receipts without upgrading a zero exit code to task success.
See [current context behavior](context-runtime-v0.3.md) and
[negative fidelity evidence](compaction-simplification.md).

Memory defaults on. Markdown in `~/.nexus/memory/` (MEMORY.md, SCRATCHPAD.md, daily/) is
the authority; QMD is a rebuildable retrieval index, and recovery/ preserves recoverable
deletions. Local first use may download user-local Node/QMD and retrieval models.
`nexus memory status` is read-only; `nexus memory off` affects future sessions and keeps
files/indexes/models. Interactive `/memory off` starts a new session so old memory content
cannot leak into its context. Memory-bearing sessions cannot be resumed with memory off.
[Setup and platform evidence](memory-setup-and-controls.md) distinguishes real bootstrap
validation from offline fixtures.

## Tools, Skills and MCP

Native tools share one Registry and Gateway. Install trusted Skills at
`~/.nexus/skills/<name>/SKILL.md`; `nexus skills list` lists them.
Use `nexus --skill name` or `nexus exec "task" --skill name` to select explicitly.
`/skill off` clears explicit selection; automatic loading remains available.
Skills do not grant permissions or automatically execute bundled scripts.

Only explicitly configured stdio MCP servers are connected:

```toml
[mcp.servers.example]
command = "/absolute/path/to/already-installed-server"
args = []
env_from = { SERVICE_TOKEN = "MY_SERVICE_TOKEN" }
```

Values of `env_from` are current-process environment variable names, not secrets.
Text, structured content and embedded text resources are bounded; sampling/elicitation
are unsupported. Host MCP tools are exposed only in Local, not disguised as sandbox tools.
Opt-in `[web] enabled = true` adds Local-only DeepSeek web_search/web_fetch with a separate
key. This is not a generic web capability of every model provider.
See [web configuration](stage7-web-tools.md).

## Permissions and recovery

Local (default) uses current-user permissions. Windows restricted modes use Native
tokens/ACLs/Jobs with **partial enforcement and unrestricted network**. Existing file read
permissions can expose credential files. Workspace grants can leave persistent ACL changes;
eligible roots require explicit `windows_acl_roots`. Dependencies belong outside writable roots.

Linux/WSL restricted modes use non-root Docker containers: RO/RW workspace bind, default
2 CPU / 1024 MiB / 128 PID, bridge or none network. `nexus sandbox doctor` diagnoses;
`nexus sandbox setup` explicitly prepares an image. Missing/invalid backends fail closed,
never silently revert to Local. macOS restricted execution is not validated in this release.

Allow once is bound to an exact denied Patch/call/scope, not arbitrary Shell or future
permissions. Tree-wide approvals are serialized with agent ownership and draft retention.
Restricted host memory requires explicit permission and does not automatically inject
QMD/snapshots; trusted Skills are separately controlled.
See [platform boundaries and known limitations](stage4-platform-backends.md).

Durable PREPARED calls may continue through current permission checks. DISPATCHING without
a complete result is UNKNOWN and is not replayed. COMPLETED restores the saved result.
Incomplete Patch recovery checks actual before/after hashes without inventing success.
Root resume reconstructs the agent tree, not old OS tasks; explicit resume_agent continues
a child's own history without reusing old approvals.
See [durable execution](stage6-durable-execution.md).

## Logs, reports and observability

Root sessions are under `~/.nexus/sessions/`; child JSONL files sit beneath the root log
directory at `<session_id>/subagents/<agent_id>.jsonl`.
They contain conversation/code/tool content: review before sharing. Metadata-only export
does not make local logs secret-free. JSONL uses an exclusive writer lock; damaged-tail
recovery preserves the original and creates a new copy.

Local eight-case evaluation creates `~/.nexus/evaluation/history.html` and
`~/.nexus/evaluation/results/<run-id>/report.html`.
Rebuild offline with `python -m nexus.evaluation.history`, optionally
`--home /path/to/evaluation`. These are neither automatic HTML for every interactive session
nor ABK reports. See the [evaluation guide](../../evaluation/next-dev-v0/README.md).

Use `--profile` for run metrics and `exec --json` for public events. Distinguish root and
child endings; a child's final event is not the root outcome. Tree-wide usage counts actual
model requests, retains unknown values, and does not add cached input twice.
Parallel child durations are not summed into root wall time.

Tracing defaults off. Opt in with `[tracing]`: enabled, full traces endpoint, api_key_env,
project. Export contains identifiers/parent-child relationships, model/tool correlation,
state, timing and usage, not prompts, reasoning, source, tool output, workspace paths or
communication bodies. `nexus trace export /path/session.jsonl --run-id RUN_ID` explicitly
re-exports a saved run. Destination deduplication is not guaranteed.
Failures leave local business outcomes unchanged; missing end events do not acquire fake
completion timestamps. See [OTel configuration](stage7-observability-models.md).

## Development and evidence

[Development/build/release](development-and-release.md) ·
[Architecture walkthrough](engineering-walkthrough.md) ·
[Candidate checks](release-0.3.0-validation.md).
Frozen designs and negative experiments remain historical evidence.
