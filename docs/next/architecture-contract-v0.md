# Nexus Next Architecture Contract

> Status: **Architecture Frozen for V0 Design**
>
> Purpose: This document defines only the architecture decisions already agreed for Nexus Next V0.  
> It intentionally avoids implementation-level over-design. Codex Astra should use this contract to produce a concrete development/design document first. **Do not start implementation until that document is reviewed and approved.**

---

## 0. V0 Goal

Nexus Next V0 must satisfy the following minimum product goal:

- **Extremely small implementation footprint**
- **Installable and usable via `pip` / `uv` with no external infrastructure required**
- **Smooth terminal interaction similar in spirit to Codex CLI**
- **Able to complete simple coding tasks end-to-end reliably and fluently**
- Complexity must be added only when later benchmark results or real task failures justify it

Core principle:

> **Minimal scaffold, evidence-driven complexity.**

Do not add architecture merely because it may be useful later.

---

# 1. Agent Runtime / Loop

Nexus Next uses a **single-agent, hand-written, message-driven Agent Loop**.

It **must not depend on LangGraph**.

The conceptual runtime is:

```text
User Task
   ↓
Agent Loop
   ↓
Model
   ├─ final response ─────────────→ END
   │
   └─ tool calls
          ↓
      execute tools
          ↓
      tool results
          ↓
        Model
```

The model itself is responsible for:

- planning
- repository exploration
- deciding what to edit
- deciding how to validate
- repair after failures
- replanning when necessary
- deciding when the task is complete

These concepts **must not become separate runtime nodes, graph stages, or workflow states**.

### Runtime state

Keep runtime state minimal. The primary state is the conversation messages.

Only basic execution bookkeeping is needed, such as:

- messages
- step count
- tool call count
- usage/token information
- current workspace/session execution context

Do not recreate large workflow state objects.

### Multiple tool calls

A model response may contain multiple tool calls.

V0 may execute them sequentially. Do not introduce a complex parallel executor unless later evidence justifies it.

### Termination

Conceptually:

```text
assistant final response → completed
tool calls               → continue loop
max step limit           → stop
user abort               → aborted
unrecoverable model/runtime error → failed
```

A failed shell command or failed test is **not automatically a runtime failure**. It is a tool observation returned to the model so the model can decide what to do next.

---

# 2. Native Tool System

V0 exposes only two native coding tools to the model:

```text
exec_command
apply_patch
```

## `exec_command`

`exec_command` is the general software-engineering interface.

It covers long-tail development behavior such as:

- reading files
- searching code
- listing files
- git operations
- running tests
- running linters
- building
- running Python / Node / Java / other project commands
- package manager commands
- repository-specific scripts

Do **not** add native tools such as:

```text
read_file
search_files
list_files
edit_file
write_file
run_tests
git_diff
```

unless later evidence shows a clear need.

Shell is a first-class capability, not a restricted fallback path.

## `apply_patch`

`apply_patch` is the structured file-modification boundary.

It should support normal patch-style file changes and provide structured modification/diff results suitable for trajectory and TUI rendering.

`apply_patch` must be confined to the current workspace.

---

# 3. Memory / Retrieval

V0 does **not** implement:

- long-term memory
- vector databases
- embeddings
- RAG
- repository pre-indexing
- semantic repository retrieval pipelines

Do not add these systems preemptively.

Conversation history is handled through the active session/messages.

Stable project instructions are handled separately through `AGENTS.md`.

---

# 4. Context Window

V0 uses **minimal Context Engineering**.

The primary context is:

```text
System Prompt
+ optional AGENTS.md
+ User / Assistant messages
+ Tool calls / Tool results
```

## Explicitly forbidden

Nexus must **not perform repository context building before the task starts**.

Do not:

- pre-scan repository code
- automatically select files before the model asks
- generate repository summaries
- build repository indexes
- inject guessed relevant code

Repository exploration is an **Agent behavior** performed through `exec_command`.

## V0 context capabilities

Only implement:

1. **Token accounting**
   - Prefer provider-reported usage when available
   - Use estimation only when needed

2. **Tool-output budget**
   - Prevent very large command output from flooding model context
   - Simple bounded/truncated output is sufficient for V0

3. **Threshold-triggered compaction**
   - Compact older conversation history only when approaching the model context limit
   - Keep recent interaction useful for continued execution

Do not implement MiniCode-style advanced context systems in V0, such as multiple compaction modes, context collapse, semantic context selection, or other complex policies.

Those may be added later only after real evidence.

---

# 5. Session Persistence / Resume

Nexus V0 supports lightweight local session persistence and resume.

## Storage

Use local **append-only JSONL** session/trajectory storage.

Do not require:

- PostgreSQL
- Redis
- SQLite
- external state services

Sessions are isolated by project/workspace.

`session_id` is internal metadata and must **not be the normal user-facing way to resume work**.

## Resume UX

Normal resume flow:

```text
/resume
```

or an equivalent CLI entry such as:

```text
nexus resume
```

should open a TUI selector showing human-readable information such as:

- task/title
- project/repository
- last updated time
- session status

The user selects the task/session visually.

## Resume semantics

Resume restores the conversation/session history.

V0 does **not** restore:

- graph node position
- planner state
- validation state
- repair state
- pending workflow instruction pointers
- complex durable workflow state

Resume means “continue the conversation/task context”, not “resume a workflow engine”.

---

# 6. Execution Environment / Minimal Boundary

V0 defaults to **trusted local execution**.

Do not rebuild the old Nexus command-control system.

## `exec_command`

Minimum runtime behavior:

- default working directory = current workspace
- may support explicit workdir
- timeout
- stdout / stderr
- exit code
- abort/cancellation
- tool-output budget

These are runtime reliability requirements, not a complex policy system.

## `apply_patch`

- structured patch execution
- workspace-confined modification
- structured diff/result

## Explicitly excluded from V0

Do not add:

- command allowlists
- command denylists
- semantic risk classification
- SAFE / WRITE / DANGEROUS levels
- Plan-based command authorization
- exact pre-authorized argv scopes
- mandatory approval workflows
- custom command-policy engines
- mandatory Docker for normal local usage

Benchmark isolation should be provided by the benchmark/harness environment when needed.

If strong sandboxing is later required, it should be introduced as an execution-environment capability, not by polluting Agent reasoning/control flow.

---

# 7. Project Instructions / Prompt

## System Prompt

Keep the Nexus System Prompt short.

It should only establish:

- Nexus is a coding agent
- available native tools
- basic working behavior
- continue using tools until the task is complete
- final response behavior

Do not place workflow orchestration contracts into the System Prompt.

## Project Instructions

V0 supports:

```text
repo-root AGENTS.md
```

If present, load it as stable project instructions.

V0 does not implement:

- auto-generated project summaries
- `NEXUS.md`
- nested directory instruction inheritance
- complex instruction precedence systems

Important distinction:

> Explicit user/project instructions may be loaded automatically.  
> Implicit repository knowledge must be discovered by the model through tools.

---

# 8. Observability / Eval Hooks

## Runtime Event Abstraction

Agent Runtime should expose a thin internal runtime-event mechanism.

The Agent Loop must not depend on a concrete observability backend.

Conceptually:

```text
Agent Runtime Events
      ├─ TUI consumer
      ├─ JSONL session / trajectory consumer
      └─ future OpenTelemetry consumer
```

Exact event class names and schemas are implementation details for Astra.

The event model only needs to support the currently required runtime/TUI/session/eval information.

## V0 Observability

V0 includes:

- Runtime Event abstraction
- TUI event consumption
- JSONL session/trajectory persistence
- basic execution facts such as model/tool activity, usage, latency, context compaction, and runtime outcome where available

Do not introduce a full observability platform in V0.

## OpenTelemetry

OpenTelemetry is a **future sink/adapter**, not a V0 core dependency.

The architecture should allow an OTel sink later without changing the Agent Loop.

OTel export failure must never affect Agent correctness.

## Eval V0

Eval is a developer/evaluation path and is **not part of normal user task execution**.

Keep Eval V0 minimal:

- ability to invoke Nexus Runtime in batch
- SWE-bench integration through the official SWE-bench harness/grader
- collect official benchmark grading results
- associate official results with Nexus trajectory
- collect basic metrics such as resolved result, tokens, latency, and tool calls

Do **not** implement in V0:

- custom universal evaluator
- custom SWE-bench grading logic
- LLM-as-Judge
- rubric framework
- automated failure taxonomy
- automated RCA pipeline
- multi-benchmark evaluation platform
- separate Eval Lab repository

For SWE-bench, the official SWE-bench harness owns:

- evaluation environment
- test execution
- FAIL_TO_PASS / PASS_TO_PASS grading
- official report/result generation

Nexus Eval V0 only integrates with it.

---

# 9. MCP / Skill / Sub-Agent

## MCP: Included

MCP enters V0 only as a **dynamic Tool Provider**.

Conceptually:

```text
MCP configuration
    ↓
connect to MCP server
    ↓
discover tool schemas
    ↓
adapt into Nexus Tool Registry
    ↓
model may call them like normal tools
```

MCP must not introduce:

- special MCP planning stages
- MCP routing workflow
- MCP-specific Agent states

MCP servers must be explicitly configured by the user.

Do not automatically trust repository-provided MCP configuration.

## Skills: Original V0 exclusion

2026-10-06 user-approved extension: [Lightweight Skills V0.1](skills-v0.1.md)
adds local Markdown discovery/loading and session selection through the existing loop.
The original V0 scope below is retained for historical context; it is superseded only
for that explicitly bounded extension. No old Skill routing architecture is restored.


V0 does not implement Skills / `SKILL.md`.

## Sub-Agent: V0.3 experimental extension

2026-10-09: the user explicitly authorized a depth-one, tool-based, read-only recursive
sub-context reusing the same loop. See [V0.3 final sprint](v0.3-final.md).
The original exclusion below remains historical V0 scope, superseded for this experiment.

### Original V0 exclusion

V0 does not implement Sub-Agent / Multi-Agent execution.

If later benchmark evidence shows that parallel independent investigation or context isolation improves performance, Sub-Agent may be introduced as a tool that reuses the same Agent Loop.

Do not create a separate Multi-Agent topology in V0.

---

# 10. Model Layer Constraints

These are implementation constraints, not a new orchestration layer.

- Agent Loop must not depend directly on provider-specific response formats
- Use a very thin Model Provider abstraction
- V0 supports **OpenAI-compatible API only**
- Normalize only the minimum model response information needed by the runtime
- Prefer provider-reported usage/token data
- Prompt caching is provider-side; Nexus should only keep stable prompt prefixes stable where practical
- Thinking/reasoning metadata must not become Agent workflow state
- Streaming should feed Runtime Events/TUI while the Agent Loop consumes the assembled model response
- Do not introduce LangChain or LiteLLM as core dependencies
- Do not add multi-provider routing, fallback chains, circuit breakers, or model routers in V0

---

# 11. CLI / TUI Product Constraints

Nexus V0 should aim for a **Codex-CLI-like terminal experience**.

## Default mode

```bash
nexus
```

starts an interactive session in the current workspace.

Startup must not scan/index the repository.

## One-shot mode

A one-shot task entry may also be supported for automation/eval use.

It must reuse the same Agent Runtime rather than creating a second execution path.

## TUI

Use a transcript-style streaming terminal UI.

The TUI should render runtime activity cleanly, including:

- model streaming/progress
- command execution
- command results
- patch/diff updates
- final response
- session resume selection

TUI rendering must consume Runtime Events.

Do not add Agent tools merely to make the TUI easier to render.

Exact UI framework and rendering implementation are left to Astra.

---

# 12. Installation / Configuration Constraints

V0 must be installable and usable without external infrastructure.

Target usage:

```bash
pip install <package>
# or
uv tool install <package>

nexus
```

## Local configuration

Use a user-level configuration location such as:

```text
~/.nexus/config.toml
```

for Nexus configuration and user-configured MCP servers.

API keys should be read from environment variables rather than hard-coded into configuration files.

Project-level behavior instructions come from:

```text
AGENTS.md
```

Do not introduce repository-local Nexus configuration in V0.

Do not create a feature-flag framework for features that do not exist.

---

# 13. Explicit V0 Non-Goals

The following must not be added to V0 unless this contract is explicitly revised:

```text
LangGraph runtime
Planner / Validation / Repair / Replan nodes
Multi-Agent orchestration
Sub-Agents
Skills
Long-term memory
Vector database
Embeddings
RAG
Repository indexing
Startup repository context building
PostgreSQL
Redis
Complex SQLite state model
Complex sandbox architecture
Command classification / allowlist / denylist
Complex approval workflows
LangSmith dependency
Full OpenTelemetry platform
LLM-as-Judge
Automated RCA pipeline
Custom SWE-bench evaluator
Complex provider routing
```

---

# 14. Guidance for Codex Astra

## Phase 1 — Design Only

Based strictly on this Architecture Contract, produce a **Nexus Next V0 Development Design**.

Do not implement code yet.

The design document should only fill in implementation details necessary to realize this contract, such as:

- minimal module/package layout
- minimal core interfaces/types
- concrete Agent Loop execution flow
- concrete JSONL/session strategy
- concrete Runtime Event wiring
- concrete OpenAI-compatible provider implementation approach
- concrete `exec_command` / `apply_patch` implementation approach
- concrete MCP tool-adaptation approach
- minimal CLI/TUI flow
- migration/replacement approach for the existing Nexus codebase
- minimal tests required to prove V0 works
- implementation sequence

Do **not** add new architectural capabilities.

If the contract leaves an implementation choice open, choose the simplest reasonable solution.

If an ambiguity truly blocks implementation, call it out explicitly instead of inventing a new subsystem.

## Phase 2 — Implementation

Implementation begins only after the V0 Development Design is reviewed and approved.

---

# 15. V0 Acceptance Standard

The V0 implementation is considered successful only when all of the following are true:

1. Nexus can be installed with normal Python packaging and started directly.
2. `nexus` can run inside a repository without external databases/services.
3. A user can give a simple coding task.
4. The Agent can inspect the repository through `exec_command`.
5. The Agent can modify code through `apply_patch`.
6. The Agent can run project commands/tests through `exec_command`.
7. Tool results feed back into the same model-tool loop until the model finishes.
8. The terminal interaction is streaming, readable, and smooth.
9. Session history is persisted locally and can be resumed through a user-friendly selector.
10. The implementation remains small enough that the core Agent Runtime can be understood quickly by reading the code.

The minimum product expectation is:

> **A small, understandable Coding Agent that installs easily and can fluently complete simple repository-level coding tasks.**
