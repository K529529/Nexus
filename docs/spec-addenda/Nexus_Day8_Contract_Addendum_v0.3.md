# Nexus Day 8 Contract Addendum v0.3

**Addendum Version:** `v0.3`
**Applies To:** `Nexus V1 Product Requirements & 10-Day Engineering Specification v1.1.1`
**Milestone:** Day 8 — Observability
**Implementation Branch:** `feature/day08-observability`
**Audited Tree:** `1d82f2e28f6424d3bc37b22ba0e5cd0f326954e1`
**Status:** **APPROVED — IMPLEMENTATION AUTHORIZED**
**Authority:** Final Architect-approved lifecycle clarification over v0.2

This document uses v0.2 as its complete parent and incorporates the final lifecycle-ordering
clarification accepted by the second Architect Review. Every other v0.2 decision remains unchanged.
Implementation is authorized only for the frozen Day 8 scope; database migration and Day 9/Day 10
work remain unauthorized.

### v0.3 revision record

V0.3 freezes one ordered per-execution FIFO lifecycle worker/control queue for each enabled
telemetry sink. Each queue contains `TraceStart`, zero or more `TelemetryEvent` records, and
`TraceFinish` in that order. Start/record/finish cannot race, resume receives a fresh lifecycle
queue, and tracer I/O remains outside the Agent critical path. The substantive lifecycle patch is
limited to sections 7, 17, 18, 22, 26, and 27; approval-state labels and section 28 are updated
throughout for consistency. All other v0.2 contracts are preserved.

### v0.2 revision record

V0.2 preserves all unaffected v0.1 sections and applies the first-review directions:

- preserves precise RuntimeStatus and TerminalStatus while separating execution-segment outcome;
- freezes one RunExecutionContext created at each NexusRuntime run/resume boundary;
- removes tracer/logger/network I/O and backpressure from the Agent critical path;
- limits changed-file kinds to ADDED/MODIFIED;
- makes ValidationFinished duration nullable/defaulted for compatibility;
- assigns all real ModelGateway instrumentation/accounting to ObservedModelGateway and replaces
  implementation-history phase naming with DIRECT_RESPONSE;
- preserves the approved token, checkpoint, no-migration, allowlist, tracing-layer, Tool-source,
  and failure-isolation decisions;
- freezes remote LangSmith configuration as a trusted user/environment-only security exception;
- narrows the proposed direct dependency to `langsmith>=0.11.1,<0.12`; and
- keeps human live progress as the default CLI UX while making ConsoleTracer explicitly opt-in for
  structured acceptance/debug traces.

All v0.2 decisions, together with the v0.3 lifecycle clarification, are approved for Day 8
implementation subject to the exact authorization boundaries in section 28.

---

## 1. Purpose

Day 8 must make a Nexus coding Run explainable, correlated, and traceable through safe typed
events and telemetry while preserving execution semantics and never exposing private
chain-of-thought.

This Addendum:

1. audits the merged Day 1–Day 7 implementation and inherited public contracts;
2. identifies the gaps between that baseline and complete Day 8 observability;
3. records exact Nexus-owned Tracer, event-distribution, telemetry, correlation, accounting,
   privacy, fallback, composition, CLI, resume, test, and acceptance contracts as revised by the
   first Architect Review;
4. records the Architect-review disposition and approved resolution of each architecture decision;
   and
5. records Day 8-only implementation authorization while stopping before implementation work.

The simpler implementation path is to retain the existing business events, add only the missing
safe RuntimeEvent types, create one versioned telemetry envelope around them, and assemble
Console/LangSmith adapters at the Composition Root. A second graph, telemetry database, global
LangChain auto-tracing mode, or observability platform is unnecessary and out of scope.

---

## 2. Frozen Baseline inheritance

The parent Specification remains authoritative, especially sections 3, 4, 5.5, 6, 9.1, 10.2,
11, 12, and all 18 items of Day 8.

The following inherited decisions remain frozen:

- CLI-first and async-first architecture;
- the existing Day 4 graph nodes, edges, node responsibilities, and sequential Tool execution;
- `NexusRuntime.run()` / `resume()` as the public runtime entry points;
- Nexus-owned domain and provider boundaries; domain must not import LangGraph, LangSmith,
  LangChain, SQLAlchemy, MCP, Typer, or a concrete model vendor;
- all repository reads, writes, commands, validation, policy, approval, and sandbox behavior;
- `ModelGateway` as the provider-neutral model boundary;
- `ToolRuntime` as the sole Tool invocation/policy/approval boundary;
- Session/Run business persistence, approval persistence, and LangGraph checkpoint ownership as
  separate concepts;
- existing RuntimeEvent meanings and ordering guarantees;
- `RuntimeEvent.to_dict()` as the existing public event serialization boundary;
- the Day 4 terminal rule: exactly one normal `FinalResult` or one handled `ErrorOccurred`;
- the counter semantics in Specification section 6.4;
- safe summaries only: no raw provider object, stack trace, credential, unrestricted environment,
  source body, prompt, patch body, stdout/stderr, or private reasoning in telemetry; and
- concrete dependency assembly only in `src/nexus/infrastructure/bootstrap/`.

Day 8 instrumentation observes these behaviors. It must not decide a Plan, approve an action,
classify risk, select a Tool, alter an edge, retry a model/tool call, change validation, or choose
a terminal outcome.

---

## 3. Current implementation audit

The audit was performed on a clean `feature/day08-observability` branch. `HEAD`, local `main`, and
`origin/main` all resolve to the merged Day 7 commit
`1d82f2e28f6424d3bc37b22ba0e5cd0f326954e1`.

### 3.1 Runtime and existing event contract

`src/nexus/domain/runtime_events.py` currently defines 14 concrete RuntimeEvent types:

```text
TaskStarted
FinalResult
RunInterrupted
ErrorOccurred
ApprovalRequested
ToolStarted
ToolFinished
RepositoryExplored
ContextBuilt
PlanCreated
ReplanOccurred
ValidationStarted
ValidationFinished
RepairStarted
```

Every event already carries `run_id`, nullable `session_id`, a timezone-aware timestamp, and a
RuntimeStatus. `to_dict()` produces `{type, run_id, session_id, timestamp, status, payload}`.

Current gaps:

- no schema version, event ID, per-execution sequence, trace ID, span identity, source, severity,
  or redaction metadata;
- no model-call start/finish event, agent-step completion event, approval-resolution event,
  changed-file event, or observability-warning event;
- `to_dict()` recursively serializes values but performs no telemetry allowlisting or redaction;
- existing payloads can include the task, final response, diff, validation evidence, Plan command
  summaries, and paths, so `to_dict()` cannot be sent directly to a remote tracer;
- `TaskStarted` and runtime-generated terminal errors are yielded directly by `NexusRuntime` and
  do not pass through the current graph/tool emitter callback; and
- `RuntimeEventBuffer` is an in-process drain-on-completion buffer, not a Day 8 event bus.

### 3.2 Live progress and event ordering

`NexusRuntime.run()` yields `TaskStarted`, awaits the entire graph invocation, drains buffered
events, and only then yields the remaining timeline. The interrupt path similarly drains only
after the graph returns an interrupted state.

Therefore the CLI is already a safe RuntimeEvent consumer, but the current architecture does not
provide coherent live progress. Events generated inside exploration, planning, tools,
validation, and finalization are normally rendered after graph completion/interruption rather
than when they occur.

Existing per-invocation Tool ordering is frozen and correct:

```text
ToolStarted -> optional ApprovalRequested -> Tool execution/denial -> ToolFinished
```

The Day 8 publisher must preserve that order and existing terminal ordering while making delivery
live. It must not create parallel Agent Tool execution.

### 3.3 Graph lifecycle coverage

| Frozen graph phase | Existing evidence/event | Gap for Day 8 |
|---|---|---|
| `initialize_run` | outer `TaskStarted` | not routed through one publisher/tracer lifecycle |
| `explore_repository` | `RepositoryExplored`; Tool pairs | no phase latency summary |
| `build_context` | `ContextBuilt` | no phase latency; raw paths not remote-safe by default |
| `create_plan` | `PlanCreated`; `ReplanOccurred` | model invocation and latency not observable |
| `approval_gate` | `ApprovalRequested` | no typed approval decision/resolution event |
| `agent_step` | counters only | no safe step-completed/model-call event |
| `execute_tool` | `ToolStarted` / `ToolFinished` | tool source absent; changed file only inferred later |
| `observe` | checkpoint state only | Tool outcome exists, but no safe observation transition |
| `replan` | `ReplanOccurred` / `PlanCreated` | no model usage/latency for regenerated Plan |
| `validate` | `ValidationStarted` / `ValidationFinished`; Tool pairs | aggregate duration absent |
| `repair_plan` | `RepairStarted` | repair guidance model call/latency absent |
| `finalize*` | `FinalResult` | no telemetry-safe aggregate finish envelope |

The v0.2 minimum does not add graph nodes or edges. Missing coverage is emitted at the
existing ownership points.

### 3.4 Model instrumentation

The current Nexus-owned model values expose content only:

```python
ModelResponse(content: str)
ModelChunk(content: str)
```

`OpenAICompatibleModelGateway` discards provider usage/response metadata after normalizing
content. Planner, repair planner, Agent decision, and Skill selection increment
`llm_call_count` at caller sites immediately before `ModelGateway.complete()`. The legacy graph
calls the gateway without the shared ledger. No call ID, phase, model latency, success/failure
event, token usage, finish reason, or streaming usage aggregation is currently exposed.

Consequences:

- elapsed time can be measured around the port without provider coupling;
- exact provider-reported tokens cannot be recovered by a wrapper after the adapter has discarded
  them;
- reading prompts, completions, or reasoning to estimate telemetry is forbidden;
- a single gateway-level accounting/instrumentation seam is safer than maintaining coverage at
  every present and future caller, but moving existing call accounting requires approval because
  the counter contract is frozen; and
- streaming and non-streaming usage normalization require an explicit public decision.

### 3.5 Tool, policy, approval, and sandbox instrumentation

`ToolRuntime.execute()` increments `tool_call_count` before registry resolution, measures duration
with `perf_counter()`, and emits a start/finish pair for SAFE, WRITE, DANGEROUS, missing, denied,
native, and MCP invocations. Denied and missing invocations therefore already count as actual Tool
invocations. `ToolFinished` carries success, final risk, policy decision, approval decision,
duration, and safe error code without arguments or results.

Current gaps:

- Tool source (`native` / `mcp` / `unknown`) is not represented in ToolInvocation, ToolResult,
  RuntimeEvent, or ToolRegistry metadata;
- plan approval persistence records the final decision, but no `ApprovalResolved` RuntimeEvent is
  emitted when a Plan decision is consumed;
- successful editing updates `AgentState.changed_files`, but no event is emitted at the moment a
  changed-file reference is recorded; and
- MCP/native failure codes are normalized through ToolResult but source cannot be correlated
  without composition-owned metadata.

The v0.2 resolution does not change CommandPolicy, ApprovalPolicy, SandboxExecutor,
ToolInvocation, ToolResult, or the frozen Tool contract. Composition builds an immutable
`tool_name -> source` map for telemetry enrichment; an unresolved/missing tool is `unknown`.

### 3.6 Persistence and resume

The existing `runs` table already contains `started_at`, `finished_at`, `model_metadata`,
non-null `token_count`, non-null `tool_call_count`, `changed_file_refs`, and JSON
`final_outcome`. Day 4 persists `step_count`, `llm_call_count`, `replan_count`, and
`repair_count` under `final_outcome.metrics`; `token_count` is currently initialized to zero and
never updated.

No telemetry table, trace ID column, span table, input/output token columns, or event-log table
exists. Approval rows already correlate `run_id` and `session_id`. Checkpoint state preserves
Agent counters across process resume.

The v0.2 V1 contract uses transient event delivery plus Console/LangSmith sinks, reuses the
existing Run fields, and requires no migration. When some provider calls do not report complete
usage, `token_count` stores the reported subtotal and `final_outcome.metrics.token_usage` records
explicit completeness as defined in section 13.2; this is carried across resume in defaulted
checkpoint state.

### 3.7 CLI, logging, configuration, and composition

`interfaces/cli/renderer.py` renders selected safe RuntimeEvents and ignores unknown types. It
does not construct infrastructure. No structured logger or tracer exists.

`bootstrap/composition.py` already constructs one RuntimeEventBuffer, ToolExecutionLedger,
ModelGateway, ToolRuntime, ValidationRunner, graph runtime, and NexusRuntime. It combines the
buffer and one optional external emitter for graph/tool events only. This is the correct location
to assemble a publisher, event enricher, redactor, structured logger, ConsoleTracer,
LangSmithTracer, and no-op/fallback behavior.

`RuntimeConfig` has no observability or LangSmith fields. `pyproject.toml` has no direct
`langsmith` dependency. `uv.lock` currently contains transitive `langsmith==0.11.1` through the
LangChain stack, but a transitive package is not an approved or stable application dependency and
must not be imported by Nexus without an approved direct dependency declaration.

---

## 4. Existing contract impact matrix

| Existing contract | Day 8 impact proposed | Compatibility requirement |
|---|---|---|
| `NexusRuntime.run/resume` signatures | unchanged | existing callers still consume `AsyncIterator[RuntimeEvent]` |
| graph topology/node responsibility | unchanged | emit only at current ownership points |
| `RuntimeEvent` base fields | unchanged | do not reinterpret existing events |
| existing RuntimeEvent subclasses | retained | additive defaults only if a field must be added |
| `RuntimeEvent.to_dict()` | retained as business-event serialization | remote telemetry uses a separate allowlisted envelope |
| `RuntimeEventBuffer` | private implementation replaced or adapted | no public reliance on drain-at-end behavior |
| `ModelGateway` | proposed additive usage metadata / observed decorator | no provider object crosses the port |
| `ModelResponse` / `ModelChunk` | proposed additive defaulted usage field | existing `content` construction remains valid |
| counter semantics | unchanged | instrumentation cannot double-count or omit calls |
| `ToolRuntime.execute` | unchanged public entry | existing order, policy, approvals, and duration remain authoritative |
| Tool/registry contracts | unchanged | source enrichment comes from Composition Root metadata |
| approval persistence | unchanged | resolution event observes the persisted decision |
| `Run` / database schema | no migration recommended | existing columns/JSON carry final summary only |
| LangGraph checkpointer | unchanged | not an observability event store |
| CLI commands/options | unchanged | renderer gains safe live summaries only |
| configuration precedence | inherited | new local/user/env fields follow approved precedence; secrets are env-only |
| dependencies | direct LangSmith dependency is approval-gated | only the frozen `langsmith>=0.11.1,<0.12` change is authorized |

---

## 5. Observability architecture boundary

**FROZEN V0.3 architecture — APPROVED:**

```text
Runtime / existing graph node / ModelGateway decorator / ToolRuntime
    -> Nexus-owned RuntimeEvent
    -> in-process EventPublisher (ordered, live, no external I/O)
       -> NexusRuntime AsyncIterator -> CLI safe renderer
       -> non-blocking telemetry queue handoff
          -> isolated TelemetrySubscriber worker(s)
          -> EventEnricher + strict Redactor
          -> versioned TelemetryEvent
          -> ConsoleTracer / optional LangSmithTracer
          -> StructuredLogger
```

Boundary rules:

1. RuntimeEvent is the business/action notification contract. TelemetryEvent is a versioned,
   allowlisted observability envelope derived from it. They are not aliases.
2. Only Runtime, the existing behavior owner, or a transparent decorator around an existing port
   may emit the corresponding event.
3. EventPublisher assigns delivery order but does not decide business outcomes or synthesize Tool,
   approval, validation, or terminal success.
4. EventEnricher adds correlation, sequence, phase, source, timing, and redaction metadata. It may
   omit or summarize payload fields; it may not mutate the RuntimeEvent.
5. Tracers and loggers receive TelemetryEvent only. They must not inspect AgentState, model
   messages, Tool arguments/results, environment variables, provider responses, or repository
   files.
6. CLI receives RuntimeEvent and renders a safe human summary. It does not call LangSmith or own
   trace lifecycle.
7. Runtime publication never calls or awaits a tracer/logger/remote adapter. No telemetry sink may
   throw into, cancel, retry, delay, or apply backpressure to the Agent control path.
8. OpenTelemetry remains a future adapter behind the same Tracer port; Day 8 does not implement it.

---

## 6. Tracer contract

The following exact public shape incorporates both Architect Reviews and is **FROZEN IN V0.3 /
APPROVED**:

```python
from collections.abc import Mapping
from typing import Protocol


class Tracer(Protocol):
    async def start_run(self, start: TraceRunStart) -> None: ...
    async def record(self, event: TelemetryEvent) -> None: ...
    async def finish(self, finish: TraceRunFinish) -> None: ...
```

### 6.1 Run execution context

```python
@dataclass(frozen=True, slots=True, kw_only=True)
class RunExecutionContext:
    trace_id: str
    execution_id: str
    run_id: str
    session_id: str | None
    is_resume: bool
    started_at: datetime
```

Identity is created and propagated as follows:

1. `NexusRuntime.run()` creates exactly one `execution_id` at entry, alongside the new `run_id`,
   and captures `started_at` once. It constructs exactly one immutable RunExecutionContext after
   session establishment succeeds, or with the requested/nullable session identity for a
   pre-graph handled failure.
2. `NexusRuntime.resume()` first resolves the existing durable Run, then creates exactly one new
   `execution_id` and RunExecutionContext before publishing the resumed TaskStarted event.
3. `trace_id` is exactly the durable `run_id`; resume never generates another trace/run identity.
4. NexusRuntime binds the immutable context to the current async execution scope. Publisher,
   telemetry enrichment, and ObservedModelGateway consume that bound value; none may generate,
   infer, replace, or repair any identity field.
5. Concurrent Runs use context-local propagation and cannot read or overwrite another Run's
   context. Tasks created for graph execution inherit only their creating Run's context.
6. Telemetry sink workers receive RunExecutionContext copied into the queued observation; they do
   not read ambient context after asynchronous handoff.
7. The binding is reset in `finally` on completion, failure, interruption, or cancellation.
   Missing/mismatched context at an emission boundary is `TELEMETRY_EVENT_INVALID` for export and
   never causes identity inference.
8. `started_at` is timezone-aware UTC; IDs are canonical UUID strings. The context contains no
   task, prompt, model content, provider object, Tool payload, or business decision.

The concrete context-local mechanism is private implementation. `contextvars.ContextVar` is the
recommended Python mechanism because it isolates interleaved async Runs without changing every
frozen business method signature.

### 6.2 Start and finish values

`TraceRunStart` contains only:

```text
context: RunExecutionContext
task_character_count: non-negative integer
model_provider: safe configured identifier or None
model_name: safe configured identifier or None
```

`TraceRunFinish` contains only:

```text
context: RunExecutionContext
finished_at: timezone-aware UTC datetime
execution_outcome: COMPLETED | FAILED | INTERRUPTED
runtime_status: existing Nexus RuntimeStatus
terminal_status: existing Nexus TerminalStatus or None
duration_ms: non-negative monotonic elapsed integer
step_count / llm_call_count / tool_call_count / replan_count / repair_count
token_usage: TokenUsage
changed_file_count: non-negative integer
validation_status: PASS | FAIL | UNKNOWN | NOT_RUN
error_code: safe Nexus code or None
```

The three outcome layers have exact, non-collapsing semantics:

| Business event/path | `execution_outcome` | `runtime_status` | `terminal_status` |
|---|---|---|---|
| `FinalResult` with successful TerminalStatus | `COMPLETED` | event's `COMPLETED` | exact `SUCCEEDED` |
| `FinalResult` with truthful non-success TerminalStatus | `FAILED` | event's `FAILED` | exact value, including `STOPPED_MAX_STEPS`, `STOPPED_MAX_REPLANS`, `STOPPED_MAX_REPAIRS`, approval denial, validation failure/unknown, or other existing value |
| handled runtime/service/model/persistence `ErrorOccurred` | `FAILED` | `FAILED` | `None` |
| durable `RunInterrupted` | `INTERRUPTED` | `INTERRUPTED` | `None` |

`execution_outcome` is the coarse execution-segment result and is FAILED for both a truthful
unsuccessful FinalResult and a handled failure before FinalResult. `runtime_status` is copied from
the exact terminal RuntimeEvent without remapping, while `terminal_status` preserves the more
precise FinalResult reason. No existing RuntimeStatus or TerminalStatus value is renamed, merged,
or inferred.

Lifecycle:

- one tracer instance may serve concurrent Runs and must isolate state by `execution_id`;
- one `start_run` precedes records for an execution segment;
- zero or more `record` calls follow in publisher sequence;
- exactly one logical `finish` is queued for every COMPLETED, FAILED, or INTERRUPTED execution
  outcome;
- `finish` is idempotent for the same `execution_id`; a duplicate does not create a second root;
- a resume creates a new execution segment under the same logical Nexus trace as section 20;
- `async` is mandatory because adapters may flush bounded asynchronous work;
- all values are Nexus-owned; the port imports no LangSmith type; and
- adapter failures are handled by the safe dispatcher in section 17, never by business callers.

Rejected in v0.2: returning a provider-specific span/run handle from
`start_run`. It leaks adapter lifecycle into Runtime and complicates concurrent resume. The
adapter may keep private state keyed by `execution_id`.

---

## 7. Event publisher/subscriber contract

The approved contract has two independent delivery lanes: a required live Runtime/CLI lane and
isolated asynchronous telemetry sink lanes. The following shape and lifecycle ordering are
**FROZEN IN V0.3 / APPROVED**:

```python
class EventSubscriber(Protocol):
    async def on_event(self, event: RuntimeEvent) -> None: ...


class EventSubscription(Protocol):
    async def aclose(self) -> None: ...


class EventPublisher(Protocol):
    def subscribe(self, subscriber: EventSubscriber) -> EventSubscription: ...
    async def publish(self, event: RuntimeEvent) -> None: ...
```

Required semantics:

1. Composition installs subscriptions before a Run starts. Dynamic subscribe/unsubscribe during a
   Run is unsupported in V1.
2. `publish()` requires the bound RunExecutionContext, validates matching Run/session identity,
   and assigns the next sequence atomically per execution segment.
3. The required lane appends the RuntimeEvent to the execution's in-process live queue. The public
   async iterator yields from that queue while graph work continues; events are not held until a
   terminal drain.
4. Publishing may await only bounded in-process bookkeeping and the required live queue. It never
   invokes or awaits Console, logger, LangSmith, SDK flush, filesystem, or network I/O.
5. Every enabled sink owns exactly one ordered FIFO worker/control queue for each `execution_id`.
   That single queue contains both lifecycle controls and records in this exact order:

   ```text
   TraceStart control
   → TelemetryEvent record*
   → TraceFinish control
   ```

   The start/finish controls carry TraceRunStart/TraceRunFinish respectively. `start_run`,
   `record`, and `finish` never use independent asynchronous paths and cannot race.
6. A fresh sink queue and lifecycle state are created for every execution segment. Resume creates
   new queues for its new `execution_id`; queues/state are never shared across run/resume segments.
7. `TraceStart` is placed in each enabled sink's queue before its first TelemetryEvent. The single
   worker must successfully process or safely fail `Tracer.start_run()` before invoking
   `Tracer.record()` for that execution.
8. `publish()` hands off each immutable RuntimeEvent observation and copied RunExecutionContext to
   the isolated telemetry-ingress queue with a non-blocking enqueue. Off the Agent critical path,
   TelemetrySubscriber performs enrichment/redaction and non-blocking fan-out of the resulting
   TelemetryEvent record into each enabled sink FIFO. Every record accepted by a healthy sink is
   processed in publisher sequence. There is no global order across Runs and different sinks need
   not finish I/O together.
9. The terminal RuntimeEvent is enriched/redacted into its TelemetryEvent and accepted into the
   sink FIFO before `TraceFinish` is queued. `TraceRunFinish` is never queued ahead of, or on an
   independent path from, the terminal observation.
10. The worker invokes `Tracer.finish()` only after every earlier accepted record has been
    processed, or deliberately discarded because that sink already failed or overflowed. A normal
    finish is attempted only while the sink remains healthy; disabled sinks may perform only the
    bounded adapter-private cleanup allowed by section 17.
11. If `start_run()` fails, the sink is disabled for that execution, its queued records may be
    discarded, exactly one safe warning is emitted, and neither `record()` nor normal `finish()` is
    attempted on that failed sink.
12. If `record()` fails, the sink is disabled, later records/control items are discarded under the
    existing failure contract, and only bounded adapter-private cleanup may occur. Business
    execution is unchanged.
13. If `finish()` fails, the already-produced business outcome is preserved and the existing safe
    trace warning is emitted.
14. The CLI stream is required and lossless for events accepted before terminal close. Sink queues
    are bounded. If a queue cannot immediately accept the next item, that sink is disabled for the
    execution; queued-but-unwritten items may be discarded, and exactly one
    `TRACE_SINK_OVERFLOW` warning goes to the live CLI lane and other healthy sinks.
15. Subscriber/enricher/redactor/tracer failures and worker cancellation are contained. A failed
    sink cannot delay Runtime, Graph, ModelGateway, ToolRuntime, ValidationRunner, another sink, or
    the CLI stream, and no sink applies backpressure to Agent execution.
16. A failed observer produces at most one warning per sink/execution. The warning is placed
    directly on the Runtime/CLI lane and healthy observer queues; it is never recursively sent to
    the failed sink.
17. Terminal RuntimeEvent publication closes the required live lane only after the terminal event
    is queued. Telemetry lifecycle completion proceeds independently under rules 9–13.
18. `EventSubscription.aclose()` is idempotent. Composition requests bounded drain/flush during
    teardown; timeout/failure cannot replace an existing business outcome. Run cancellation remains
    business/runtime cancellation; telemetry worker cancellation cannot cancel the Run.

Creating one graph producer task, one live-event consumer, and isolated telemetry workers is
observability plumbing, not parallel Agent/Tool execution. The graph continues awaiting exactly
one Agent Tool invocation at a time.

`RuntimeEventBuffer` may be privately replaced by this publisher/stream mechanism. Its current
constructor/emitter callback is not a frozen Day 8 public contract.

---

## 8. Telemetry schema

`TelemetryEvent` is **FROZEN IN V0.3 / APPROVED** as a slotted,
keyword-only dataclass with exact schema version `1.0`:

```text
schema_version: "1.0"
event_id: UUID string
event_type: TelemetryEventType
timestamp: timezone-aware UTC datetime from the RuntimeEvent
sequence: positive integer, scoped to execution_id
trace_id: UUID string
execution_id: UUID string
run_id: UUID string
session_id: UUID string or None
span_id: UUID string or None
parent_span_id: UUID string or None
phase: RuntimePhase
severity: DEBUG | INFO | WARNING | ERROR
payload: JSON object from an event-specific allowlist
redaction: RedactionMetadata
```

`RedactionMetadata` contains only:

```text
policy_version: "1.0"
redacted: bool
omitted_fields: tuple[str, ...]       # field names/categories, never omitted values
truncated_fields: tuple[str, ...]
```

`RuntimePhase` is frozen as the following exact public enum:

```text
RUN
REPOSITORY
CONTEXT
PLAN
APPROVAL
AGENT
MODEL
TOOL
REPLAN
VALIDATION
REPAIR
CHANGE
ERROR
OBSERVABILITY
```

The exact TelemetryEventType-to-RuntimePhase mapping is:

```text
run.started              -> RUN
run.interrupted          -> RUN
run.finished             -> RUN
repository.explored      -> REPOSITORY
context.built            -> CONTEXT
plan.created             -> PLAN
approval.requested       -> APPROVAL
approval.resolved        -> APPROVAL
agent_step.completed     -> AGENT
model_call.started       -> MODEL
model_call.finished      -> MODEL
tool.started             -> TOOL
tool.finished            -> TOOL
replan.occurred          -> REPLAN
validation.started       -> VALIDATION
validation.finished      -> VALIDATION
repair.started           -> REPAIR
changed_file.recorded    -> CHANGE
error.occurred           -> ERROR
observability.warning    -> OBSERVABILITY
```

`RuntimePhase` and `ModelCallPhase` are separate dimensions. Model call telemetry always uses
`RuntimePhase.MODEL`; its RuntimeEvent payload retains the exact semantic `ModelCallPhase` value
(`PLAN`, `REPLAN`, `REPAIR`, `AGENT_STEP`, `SKILL_SELECTION`, or `DIRECT_RESPONSE`). Model-call
telemetry is never mapped to a RuntimePhase named after its ModelCallPhase.

The Day 8 V1 severity mapping is frozen as:

```text
INFO     all normal lifecycle/action events
WARNING  observability.warning
ERROR    error.occurred; model_call.finished when success == false
DEBUG    reserved; not actively emitted by Day 8 V1 unless separately approved
```

`tool.finished(success=false)` remains INFO because controlled Tool/policy outcomes are not
tracer/system failures. `run.interrupted` remains INFO because it is a truthful durable runtime
state rather than an observability failure.

The Day 8 event type registry is frozen in v0.2 as:

```text
run.started
repository.explored
context.built
plan.created
approval.requested
approval.resolved
agent_step.completed
model_call.started
model_call.finished
tool.started
tool.finished
replan.occurred
validation.started
validation.finished
repair.started
changed_file.recorded
run.interrupted
run.finished
error.occurred
observability.warning
```

The minimum new RuntimeEvent shapes frozen to make that coverage explicit are:

```text
ModelCallStarted:
    model_call_id: UUID
    phase: ModelCallPhase
    provider: str | None
    model: str | None

ModelCallFinished:
    model_call_id: UUID
    phase: ModelCallPhase
    success: bool
    duration_ms: int
    usage: TokenUsage
    error_code: str | None

AgentStepCompleted:
    step_count: int
    decision_kind: TOOL_ACTION | TASK_READY | CONTINUE
    model_call_id: UUID

ApprovalResolved:
    approval_id / subject / invocation_id / plan_id / plan_version
    decision: APPROVED | DENIED
    actor_category: USER | POLICY

ChangedFileRecorded:
    invocation_id: UUID
    relative_path: str
    change_kind: ADDED | MODIFIED
    changed_file_count: int

ObservabilityWarning:
    code: TRACE_SINK_FAILED | TRACE_FLUSH_FAILED | TELEMETRY_EVENT_INVALID |
          TRACE_REDACTION_FAILED | TRACE_SINK_OVERFLOW
    sink: CONSOLE | LANGSMITH | LOGGER | TELEMETRY
    operation: START | RECORD | FINISH | FLUSH | ENRICH | REDACT
    fallback: CONSOLE | LANGSMITH | NOOP | NONE
```

These events use the inherited base correlation/timestamp fields and `RuntimeStatus.STARTED`; they
do not terminalize the Run. ModelCallFinished failure is followed by the existing business error
path and does not replace it. Existing event types remain source-compatible. The separately
frozen v0.2 `ValidationFinished.duration_ms: int | None = None` is a defaulted nullable additive
field. Existing Day 1–Day 7 constructors remain valid; every new Day 8-observed validation path
must populate a non-negative integer.

Unknown schema versions or event types fail closed at the telemetry subscriber: they are not sent
remotely, produce a bounded safe warning, and do not fail the business Run. Payload validation
occurs before any logger/tracer receives the event.

---

## 9. Correlation and trace identity

The Architect Reviews preserve the following V1 identity scheme. It is **FROZEN IN V0.3 /
APPROVED**:

- `run_id` remains the existing durable Nexus logical Run identity;
- `session_id` remains the existing business conversation identity;
- `trace_id = run_id` for V1; this gives deterministic durable correlation without a new column;
- `execution_id` is created exactly once at each `NexusRuntime.run()` or `resume()` execution
  boundary and is never generated by a downstream publisher, enricher, gateway, or tracer;
- the root span ID equals `execution_id`;
- child model spans use a generated `model_call_id` as `span_id`;
- child Tool spans use the existing `invocation_id` as `span_id`;
- validation uses a generated validation span ID, with validation Tool invocations as children;
- phase-only events may have no child span and use the root as parent;
- a child never changes its business `run_id` or `session_id`; and
- per-execution sequence starts at 1. Cross-resume order is `(execution start time, execution_id,
  sequence)` and does not pretend to be one persisted global sequence.

RunExecutionContext in section 6 is the single carrier of these identities. This scheme avoids
database migration and survives process restart because `run_id` is durable.
It intentionally allows one logical Nexus trace to contain multiple execution segments. A
LangSmith adapter may represent segments as linked roots tagged with the same `nexus.trace_id` and
`nexus.run_id`; a non-interrupted acceptance Run remains one root trace.

Alternative requiring approval: persist a distinct trace/root ID and sequence cursor to continue
one provider root across process resume. That requires schema/checkpoint/provider lifecycle
decisions and is not recommended for Day 8 V1.

---

## 10. Model instrumentation

### 10.1 Normalized usage value

```python
class UsageAvailability(StrEnum):
    REPORTED = "REPORTED"
    PARTIAL = "PARTIAL"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class TokenUsage:
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    availability: UsageAvailability
```

All known values are non-negative integers. If all three are known,
`total_tokens == input_tokens + output_tokens`. Unknown values remain `None`; Nexus never derives
tokens from prompt text, completion text, character count, or private reasoning.

**FROZEN V0.3 additive model values — APPROVED:**

```python
ModelResponse(content: str, usage: TokenUsage | None = None)
ModelChunk(content: str, usage: TokenUsage | None = None)
```

For streaming, the adapter emits aggregate invocation usage on at most one terminal chunk; usage
is not a per-chunk delta. If the provider never reports usage, every chunk has `usage=None` and the
invocation finishes `UNAVAILABLE`.

### 10.2 Instrumentation ownership and event fields

A Nexus-owned `ObservedModelGateway` decorator is the **APPROVED single model-instrumentation seam
for v0.3**. It is assembled once around the concrete gateway
and supplied to Planner, repair planning, Agent decisions, Skill selection, and the direct-response
graph. It:

1. consumes the exact bound RunExecutionContext plus ModelCallPhase from the current async Run;
   it never invents or infers execution identity;
2. increments `llm_call_count` exactly once immediately before delegating a real gateway call;
3. emits `ModelCallStarted` with call ID, phase, provider, and model name but no messages;
4. measures monotonic elapsed time;
5. emits `ModelCallFinished` with success, safe error code, duration, and normalized usage;
6. re-raises the unchanged existing ModelError/ConfigurationError behavior; and
7. never logs or exports prompt, completion, tool schema text, raw response, headers, or reasoning.

`ModelCallPhase` is limited to stable business/runtime meanings: `DIRECT_RESPONSE`, `PLAN`,
`REPLAN`, `REPAIR`, `AGENT_STEP`, and `SKILL_SELECTION`. `DIRECT_RESPONSE` identifies the approved
minimal direct model-response lifecycle when that runtime path is used; names describing
implementation history such as `LEGACY_RESPONSE` are forbidden public telemetry values.

ObservedModelGateway owns `llm_call_count` for every real ModelGateway invocation. Existing
Planner, repair, Agent, Skill selector, and direct-response caller-side increments are removed so
one call cannot double-count. The increment occurs exactly once immediately before delegation,
including provider failure. Budget rejection before a real gateway call remains zero calls and
emits no model-start pair.

---

## 11. Tool instrumentation

Existing ToolStarted/ToolFinished events and ToolResult remain authoritative. Day 8 must not emit
a second pair around `ToolRuntime.execute()` or count an underlying sandbox/MCP transport call as
another Nexus Tool invocation.

The telemetry allowlist is:

| Event | Allowed payload |
|---|---|
| `tool.started` | invocation ID, tool name, source, proposed risk, current tool-call count |
| `tool.finished` | invocation ID, tool name, source, success, final risk, policy decision, approval decision, duration ms, safe error code |

Rules:

- `source` is `NATIVE`, `MCP`, or `UNKNOWN`; it comes from immutable Composition Root metadata,
  never string-prefix guessing inside domain policy;
- an invocation counts once when ToolRuntime accepts it, including missing tools, policy denial,
  approval denial, Plan-scope denial, MCP failure, and native failure;
- a ToolStarted risk is the proposed classification; ToolFinished risk is the authoritative final
  risk after resource checks and possible escalation;
- denied Tools have a normal finished event with `success=false`; denial is not a tracer failure;
- Tool duration begins before registry resolution and ends after normalized ToolResult creation,
  matching the existing implementation;
- MCP manager transport details, server stderr, protocol payload, native output, stdout/stderr,
  arguments, commands, file content, and environment are excluded;
- validation Tool calls retain their normal Tool events and are children of the active validation
  span; and
- changed-file recording is emitted only after a successful editing ToolResult has produced the
  existing validated ChangedFile evidence.

No ToolSource field is added to ToolInvocation/ToolResult in the approved v0.3 resolution. Any
future proposal to make source a general Tool-domain capability rather than telemetry metadata is
a separate public Tool contract change and requires prior specification approval.

---

## 12. Approval, Replan, Validation, and Repair instrumentation

### 12.1 Approval

Existing `ApprovalRequested` remains unchanged. Add the following safe event only after the
existing approval service has persisted the decision:

```text
ApprovalResolved:
    approval_id
    subject: TOOL | PLAN
    invocation_id: UUID or None
    plan_id: UUID or None
    plan_version: int or None
    decision: APPROVED | DENIED
    actor_category: USER | POLICY
```

No user reason, raw command, patch, Tool arguments, or unrestricted resource summary enters remote
telemetry. The existing CLI may continue showing the already-approved safe Plan scope digest.
Instrumentation never requests or persists an approval itself.

### 12.2 Replan

`ReplanOccurred` remains the single event for an actual regenerated Plan. Its existing
`replan_count` and version transition are authoritative. Remote telemetry allows the Plan ID,
versions, and count but omits the free-text reason. It includes `reason_present=true` only.

### 12.3 Validation

`ValidationStarted` and `ValidationFinished` remain authoritative. The Day 8 addition is
`duration_ms: int | None = None`, measured monotonically around ValidationRunner. The default
preserves all Day 1–Day 7 constructors; Day 8-observed validation must provide a non-negative
integer. `None` means the event came from a compatibility/unobserved path, not zero elapsed time.

Allowed telemetry includes check IDs/kinds, aggregate status, confidence, executed count,
repair count, and duration. It excludes command argv/cwd, Tool output, stdout/stderr, diff, test
names derived from sensitive paths, and ValidationResult summaries.

### 12.4 Repair

`RepairStarted` remains the event for entry into a new repair attempt; its count remains the
existing ledger count. Remote telemetry omits `failure_summary` and includes
`failure_summary_present=true`. The model call that creates repair guidance is separately recorded
with phase `REPAIR`. No new repair-completed graph transition is introduced.

### 12.5 Agent step and changed file

Add safe events at existing state-ownership points:

```text
AgentStepCompleted:
    step_count
    decision_kind: TOOL_ACTION | TASK_READY | CONTINUE
    model_call_id

ChangedFileRecorded:
    invocation_id
    relative_path
    change_kind: ADDED | MODIFIED
    changed_file_count
```

AgentStepCompleted excludes raw model output, decision rationale, Tool arguments, and reasoning.
ChangedFileRecorded is emitted only when the current `_record_change` logic accepts a successful
`apply_patch` or `write_file` result. `DELETED` is not a Day 8 V1 value because Nexus V1 has no
authorized delete capability. Paths are normalized workspace-relative paths; absolute
workspace/user paths are forbidden.

---

## 13. Token, latency, and counter semantics

### 13.1 Counter inheritance

The Specification section 6.4 meanings are unchanged:

| Counter | Exact increment point |
|---|---|
| `step_count` | immediately before one model-driven `agent_step` decision cycle |
| `llm_call_count` | immediately before every real ModelGateway invocation, including a failed invocation |
| `tool_call_count` | at entry to every actual ToolRuntime invocation, including denied/missing/failed invocations |
| `replan_count` | when a replacement Plan is actually generated from a material replan reason |
| `repair_count` | upon entering one new repair attempt after validation failure |

One model response containing multiple requested Tool actions still increments one step, while
each ToolRuntime invocation increments the Tool count. Validation and repository-exploration Tool
calls count because they are real ToolRuntime invocations. Tracer retries, logger writes, and
LangSmith API calls never affect business counters.

### 13.2 Token aggregation

For each call, sum only provider-reported normalized values. The Run aggregate carries:

```text
input_tokens_reported
output_tokens_reported
total_tokens_reported
calls_with_reported_usage
calls_with_partial_usage
calls_with_unavailable_usage
usage_complete: bool
```

`usage_complete=true` only when every attempted ModelGateway call reports a complete internally
consistent usage triple. If any call is partial/unavailable, reported subtotals remain truthful
but must never be labelled exact total usage.

**FROZEN V0.3 persistence without migration — APPROVED:**

- `runs.token_count = total_tokens_reported` (a reported subtotal when incomplete);
- `final_outcome.metrics.token_usage` stores the complete aggregate including `usage_complete`;
- user/trace output always displays `reported tokens` plus completeness, never silently displays
  zero as exact usage when calls were unavailable.

Changing `runs.token_count` to nullable or adding input/output/completeness columns is an
alternative schema change and is not recommended for Day 8 V1.

### 13.3 Latency

All elapsed durations use a monotonic clock; UTC timestamps are correlation/display metadata and
are not subtracted for duration.

```text
run duration        TaskStarted publication -> terminal/interrupt outcome publication
model duration      immediately before gateway delegation -> response/stream completion/failure
tool duration       existing ToolRuntime measurement, including lookup/policy/approval/execution
validation duration before ValidationStarted -> after aggregate ValidationResult is constructed
```

Plan, Replan, Repair, and Agent model latency are derived from ModelCallFinished phase values.
Run latency is not the sum of child latencies and may include persistence, context work, approval
wait, and other runtime overhead. An interrupted segment finishes with INTERRUPTED; approval wait
between process segments is not attributed to either execution duration.

Durations are non-negative integer milliseconds. Sub-millisecond work rounds down to zero. No
wall-clock correction, percentile analytics, or cross-run aggregation is in Day 8 scope.

---

## 14. Redaction and privacy contract

The first Architect Review approves the section 14 design as written. Day 8 uses deny-by-default,
event-specific allowlists. Redaction happens before both local structured logging and any remote
adapter. Regex replacement after arbitrary object serialization is defense in depth, not the
primary control. The contract is frozen in v0.3 and approved.

### 14.1 Always forbidden

The following never enter TelemetryEvent payload, tracer name, tags, metadata, inputs, outputs,
or exception fields:

- model/system/user/assistant messages, complete task text, prompts, completions, reasoning,
  chain-of-thought, rationale, or provider response objects;
- API keys, authorization/cookie headers, tokens, database credentials/URLs, proxy credentials,
  environment variable values, and SecretStr values;
- Tool arguments/results, patch bodies, file bodies, retrieved chunks, Skill bodies, MCP payloads,
  stdout/stderr, process environment, or unrestricted commands;
- final response content, exact diff, ValidationResult evidence, raw exception text, stack traces,
  locals, HTTP bodies/headers, or SDK request/response objects; and
- absolute workspace paths, user-home paths, configuration paths, or repository remote URLs.

### 14.2 Allowed safe summaries

- stable UUID correlation IDs and per-execution sequence;
- finite enum values, booleans, non-negative counts/durations, and safe Nexus error codes;
- configured provider/model identifiers, excluding base URL and credentials;
- Tool registry name and Composition-owned source;
- Plan ID/version/kind and step count, but not step summary text;
- validation check IDs/kinds and aggregate status/confidence;
- selected Skill IDs and counts, but no body or selection-reason text;
- task character count, not task text/hash;
- workspace-relative changed-file path and change kind; and
- redaction category names and truncation markers without original values.

### 14.3 Path and string limits

Workspace-relative changed-file paths are the only path values allowed remotely in the v0.2
contract. They use `/`, contain no `.`/`..`/empty component, are at most 512 Unicode
code points, and are revalidated against workspace containment. Other repository path lists are
reported as counts.

Allowed identifiers and safe error codes are capped at 256 code points; model/provider/tool names
at 256; payload depth at 4; sequence values at configured runtime integer bounds; and a serialized
TelemetryEvent at 32 KiB. Overflow omits/truncates the field, sets RedactionMetadata, and never
fails the business Run.

### 14.4 Defense in depth

LangSmith client configuration must hide/process inputs, outputs, and metadata even though only
pre-redacted TelemetryEvent values are supplied. Global LangChain auto-tracing is forbidden for
Day 8 because it may capture messages and Tool payloads outside the Nexus redaction boundary.

The LangSmith documentation explicitly supports client-level hiding/transformation of inputs,
outputs, and metadata and requires flushing buffered runs before exit. Implementation must verify
those behaviors against the approved direct SDK version rather than relying on ambient global
environment tracing.

---

## 15. ConsoleTracer behavior

ConsoleTracer is a Tracer adapter over the structured logger, not the human CLI renderer.

The CLI Renderer is the default human-readable live progress surface. Normal `nexus chat` and
`nexus session resume` do not print full JSONL telemetry in addition to that progress.

**FROZEN V0.3 behavior — APPROVED:**

- `console_tracing_enabled` defaults to `false`;
- when explicitly enabled through resolved configuration, ConsoleTracer writes one structured
  local trace alongside the separate human renderer;
- writes one UTF-8 JSON object per safe TelemetryEvent to stderr;
- uses the exact TelemetryEvent schema and no additional RuntimeEvent serialization;
- emits start/record/finish in publisher sequence;
- never writes ANSI styling, task/prompt/result content, diff, or Tool evidence;
- flushes on finish/interruption and application teardown;
- is safe under concurrent Runs because each line is one complete JSON object;
- logger level maps from TelemetryEvent severity only; and
- formatting/output failure is handled under section 17.

ConsoleTracer is explicitly enabled for the Day 8 Console trace demo, acceptance/debug runs, and
its deterministic tests. No new CLI command or option is introduced. Safe Console/local enablement
may follow normal Nexus configuration precedence, including repository configuration, because it
does not select a remote destination or export data.

This separation prevents structured trace format from becoming the normal CLI public contract and
prevents duplicate business decisions in the tracing layer.

---

## 16. LangSmithTracer behavior

LangSmithTracer is an infrastructure adapter implementing only the Nexus Tracer port.

### 16.1 Mapping

**FROZEN V0.3 manual sanitized mapping — APPROVED:**

| Nexus value | LangSmith representation |
|---|---|
| execution segment | one root `chain` run named `nexus.run` |
| model call | child `llm` run with phase tag and token/duration metadata |
| Tool invocation | child `tool` run with safe name/source/outcome metadata |
| validation | child `chain` run named `nexus.validation`; validation Tools are children |
| Plan/Replan/Approval/Repair/ChangedFile | safe events/metadata on the current root or phase span |
| terminal finish | root completion/error/interrupted status with aggregate safe metrics |

Every root is tagged with schema version, `nexus.trace_id`, `nexus.execution_id`, `nexus.run_id`,
and nullable session correlation. Inputs/outputs supplied to the SDK are empty or safe aggregate
objects only. Raw RuntimeEvent and AgentState are never supplied.

For resume, each execution segment may be a separate provider root linked by the stable Nexus
trace/run metadata. Day 8 does not promise cross-process continuation of one provider-native root.

### 16.2 Enablement and configuration

LangSmith is disabled by default. It is enabled only by resolved trusted configuration assembled
at the Composition Root. The Architect Reviews freeze and approve these RuntimeConfig fields for
v0.3:

```text
console_tracing_enabled: bool = false
langsmith_tracing_enabled: bool = false
langsmith_project: str = "nexus"
langsmith_endpoint: str | None = None
langsmith_workspace_id: str | None = None
langsmith_api_key: SecretStr | None = None
```

This is an explicit security exception to the general configuration precedence contract.

Exact trusted sources:

- API key is accepted only from `NEXUS_LANGSMITH_API_KEY`; never CLI or repository config;
- enable, project, endpoint, and workspace ID are accepted only from user-level configuration or
  `NEXUS_LANGSMITH_TRACING_ENABLED`, `NEXUS_LANGSMITH_PROJECT`, `NEXUS_LANGSMITH_ENDPOINT`, and
  `NEXUS_LANGSMITH_WORKSPACE_ID` environment variables;
- repository `.nexus/config.toml` has no authority to enable, disable, select a project/workspace,
  or redirect a LangSmith endpoint;
- any repository `[observability.langsmith]` table or equivalent remote LangSmith key is rejected
  with safe `TRACE_CONFIGURATION_INVALID`, including values that appear to disable tracing; it is
  not silently ignored or allowed to shadow trusted sources;
- no LangSmith API key is accepted from user or repository TOML;
- Console/local `observability.console.enabled` may follow normal repository/user/environment
  precedence because it cannot export to a remote destination;
- enabling without required credentials is a visible configuration error before a Run;
- no `LANGSMITH_TRACING=true` global side effect is set by Nexus; and
- Nexus passes configuration to its private client explicitly.

The exact user-level TOML keys are `[observability.langsmith] enabled`, `project`, `endpoint`, and
`workspace_id`. The API key has no TOML key. Repository configuration may use only safe local
`[observability.console] enabled`; the remote LangSmith table or equivalent keys are invalid there.

No CLI flag is added. The model, repository instructions, Skills, Tools, MCP servers, and Agent
state cannot mutate these values.

### 16.3 Dependency

`langsmith` must be a direct project dependency before Nexus imports it. The current transitive
`uv.lock` entry alone does not authorize use. The approved Day 8 dependency is the reviewed minor
line `"langsmith>=0.11.1,<0.12"`, followed by lock/wheel verification. This documentation-only turn
does not modify `pyproject.toml` or `uv.lock`; that exact dependency change is authorized for the
subsequent Day 8 implementation and no broader dependency change is authorized.

---

## 17. Tracer failure and fallback behavior

Tracer failures are observability failures, not business failures.

**FROZEN V0.3 deterministic behavior — APPROVED:**

1. The human CLI Renderer is the always-available progress surface. ConsoleTracer and
   LangSmithTracer are independently optional structured sinks selected by trusted configuration.
2. SafeTracerDispatcher gives every enabled sink exactly one isolated FIFO lifecycle queue and one
   worker per execution. `TraceStart`, all accepted TelemetryEvent records, and `TraceFinish` share
   that queue; independent start/record/finish tasks are forbidden.
3. The worker processes start before any record, records in publisher order, and finish only after
   all earlier accepted records. Resume constructs a new queue/state for the new `execution_id`.
4. On `start_run()` failure, disable the sink for the execution, discard queued sink records if
   necessary, emit exactly one safe warning, and make no `record()` or normal `finish()` call to it.
5. On `record()` failure or queue overflow, disable the sink and discard later records/control
   items. Bounded adapter-private cleanup is allowed, but no normal lifecycle call may race with the
   failed worker and business execution is unchanged.
6. On `finish()` failure, preserve the already-produced business outcome and emit exactly one safe
   warning. A finish failure cannot reopen, replace, or reclassify that outcome.
7. If LangSmith fails, continue CLI and Console if Console is enabled/healthy. If Console fails,
   continue CLI and LangSmith if LangSmith is enabled/healthy. A failed sink becomes NoOp for the
   remainder of that execution without affecting healthy sinks.
8. If all tracers fail, Runtime still reaches its original business outcome and CLI receives one
   warning through the non-tracer event stream/emergency stderr path.
9. Warning fields are only sink kind, operation (`start|record|finish|flush`), stable code
   `TRACE_SINK_FAILED`, `TRACE_SINK_OVERFLOW`, or `TRACE_FLUSH_FAILED`, and fallback kind. No raw
   exception message is included.
10. No hidden retry occurs during a Run. SDK-internal transport buffering/retry may operate only
   within the approved adapter timeout and cannot change business counters.
11. Teardown/flush and any adapter-private cleanup have a bounded timeout. Timeout produces
    `TRACE_FLUSH_FAILED` but does not replace
    an already-produced FinalResult/ErrorOccurred.
12. A tracing configuration error detected before Runtime construction is a normal safe
    ConfigurationError; fallback does not silently override an explicitly invalid enabled setup.

The warning path must be tested for recursion: a failed tracer must never receive the warning
about its own failure.

---

## 18. Composition Root integration

Only `src/nexus/infrastructure/bootstrap/` assembles concrete observability dependencies.

**FROZEN V0.3 order — APPROVED:**

```text
resolved RuntimeConfig
-> StructuredLogger
-> ConsoleTracer
-> optional LangSmith client + LangSmithTracer
-> SafeTracerDispatcher / NoOp fallback
-> EventEnricher + Redactor
-> TelemetrySubscriber isolated from the live Runtime/CLI lane
-> one per-execution FIFO lifecycle queue + one worker for each enabled sink
   [TraceStart -> TelemetryEvent record* -> TraceFinish]
-> InProcessEventPublisher
-> ObservedModelGateway using shared ToolExecutionLedger
-> existing ToolRuntime / ValidationRunner / Graph
-> NexusRuntime live event stream
```

The same publisher instance is supplied to Runtime, graph, ToolRuntime, and ValidationRunner. The
same observed gateway instance is supplied to Planner, Agent, Skill selector, and the approved
direct-response graph path. The same ledger owns all inherited counters.

Composition/Lifecycle coordination creates a fresh FIFO and worker for each enabled sink and each
`execution_id`. It queues TraceStart before telemetry fan-out begins, routes every accepted
TelemetryEvent record and TraceFinish through that same worker, and queues TraceFinish only after
the terminal TelemetryEvent has been accepted. It never creates separate asynchronous
start/record/finish paths. These workers, their I/O, and bounded cleanup remain outside the Agent
critical path.

Forbidden construction locations include CLI, graph nodes, domain models, Tool implementations,
Planner, Agent adapter, ContextManager, Skill loader/selector, and persistence repositories.

Bootstrap owns telemetry-ingress closure, per-execution lifecycle queue/worker closure, bounded
tracer cleanup/flush, LangSmith client closure, MCP cleanup, checkpoint cleanup, and database
cleanup. A tracing cleanup failure is secondary to the original business/bootstrap error and
follows section 17.

---

## 19. CLI rendering boundary

CLI commands and arguments do not change in Day 8.

The renderer may add safe concise output for:

```text
exploration/context completed
Plan created / Replan count
approval requested / resolved
agent step completed
model call phase completed with duration and reported-token availability
Tool started / finished with source, status, and duration
validation started / finished
repair attempt started
changed-file relative path
observability fallback warning
terminal status and aggregate safe metrics
```

Rules:

- render events as they arrive from the live async iterator;
- preserve the existing Plan display and approval prompt behavior unless Architect Review
  explicitly narrows it for privacy;
- do not display raw telemetry JSON by default; ConsoleTracer writes structured JSON to stderr;
- never display prompt/completion/reasoning, Tool arguments/results, file content, raw exception,
  secret, or remote tracer URL containing credentials;
- an unknown additive RuntimeEvent remains nonfatal and is ignored safely;
- tracer warning does not change the CLI success exit code; and
- CLI does not subscribe directly to LangSmith or own fallback.

The first Architect Review resolves the Console UX decision: the human Renderer is on by default;
ConsoleTracer is off by default and, when explicitly enabled for acceptance/debugging, writes JSONL
to stderr. Day 8 adds no log-file lifecycle and no CLI command or option.

---

## 20. Resume and checkpoint tracing semantics

**FROZEN V0.3 behavior — APPROVED:**

1. Initial `run()` and every `resume()` retain the same durable `run_id`, `session_id`, and
   `trace_id=run_id`.
2. Every process entry receives a new `execution_id`, root span, duration, and sequence beginning
   at 1.
3. An approval interrupt finishes the current execution segment as `INTERRUPTED`; it does not
   finish the durable business Run.
4. Resume starts a new segment with `is_resume=true`. The existing checkpoint restores graph state
   and counters; observability does not own checkpoint data.
5. Aggregate counters after resume use the restored ToolExecutionLedger/AgentState values and do
   not reset or double-count.
6. Token usage accumulated before interruption is carried in an additive, defaulted Nexus-owned
   `TokenUsageAggregate` field in AgentState checkpoint state. Existing checkpoints without the
   field load the default empty aggregate.
7. The final aggregate is persisted through existing Run fields as defined in section 13.2. No
   Day 8 database migration is introduced.
8. Re-emitted PlanCreated/ApprovalRequested events on a no-input resume retain existing semantics;
   the new execution sequence may contain them again and adapters must not claim they are new
   business Plan/approval records.
9. A process crash may lose unflushed transient Console/LangSmith records; Day 8 does not introduce
   durable event replay. Business resume correctness remains checkpoint-owned.

Alternative: persist every event/span or a provider root ID in PostgreSQL. This expands schema and
event-sourcing responsibility and is not recommended for V1.

---

## 21. Error semantics

Existing NexusError, ModelError, ToolError, ValidationError, ContextError, ConfigurationError,
FinalResult, and ErrorOccurred meanings remain unchanged.

Day 8 adds only safe observability codes:

| Code | Meaning | Business Run effect |
|---|---|---|
| `TELEMETRY_EVENT_INVALID` | event cannot satisfy schema/allowlist | warn, omit from sinks, continue |
| `TRACE_SINK_FAILED` | tracer start/record/finish failed | disable sink, fallback, continue |
| `TRACE_SINK_OVERFLOW` | isolated sink queue cannot accept the next observation | disable that sink, warn once, continue without Agent backpressure |
| `TRACE_FLUSH_FAILED` | bounded flush/close failed | warn, preserve business outcome |
| `TRACE_CONFIGURATION_INVALID` | tracing explicitly enabled with invalid trusted config | fail bootstrap before Run |
| `TRACE_REDACTION_FAILED` | payload cannot be proven safe | drop payload/event, warn, continue |

Raw exceptions are chained only inside implementation for developer debugging and are never
serialized. A model/tool/business failure still produces its existing event/outcome plus a
successful observability record if a sink is healthy. It must not be remapped to a trace code.

Telemetry schema/enrichment failure is fail-closed for export and fail-open for Agent execution:
unsafe/invalid telemetry is dropped rather than forwarded, while the business event remains
available to the safe CLI path.

---

## 22. Tests required

No implementation may be considered complete without deterministic coverage for the following.

### 22.1 Domain and schema

- exact Tracer signatures, lifecycle control items, and async behavior;
- UUID, UTC timestamp, sequence, phase, severity, parent/span, payload, and schema-version
  validation;
- immutable/copy-on-construction JSON payloads;
- TokenUsage complete/partial/unavailable invariants and negative/inconsistent rejection;
- TraceRunStart/Finish terminal and counter invariants; and
- additive construction compatibility for ModelResponse/ModelChunk and approved RuntimeEvents.

### 22.2 Publisher and live runtime

- per-execution order under one and multiple subscribers;
- two interleaved Runs remain independently sequenced/correlated;
- TaskStarted is observable before graph work and graph events are yielded before graph terminal;
- Tool start/optional approval/finish order remains exact;
- FinalResult/ErrorOccurred remains the sole terminal business event;
- terminal RuntimeEvent is converted and accepted as the final sink record before TraceFinish is
  queued;
- interrupt/resume ordering and deliberate Plan/approval re-emission;
- subscriber failure isolation, one warning, no recursion, no event loss for CLI; and
- sink cancellation/close/latency/queue saturation never backpressure Agent execution, create
  parallel Tool execution, or alter graph routing.

### 22.3 Model instrumentation

- all phases use one observed gateway;
- exactly one call-count increment and one start/finish pair per real attempted call;
- zero call/event for pre-call budget rejection;
- successful/empty/ConfigurationError/ModelError/unexpected provider failure mapping;
- monotonic duration under an injected fake clock;
- complete, partial, unavailable, and inconsistent provider usage;
- streaming usage appears at most once and aggregates once;
- no prompt/completion/provider object in events, repr, logs, or mock adapter calls; and
- direct-response, Planner, Replan, Repair, Agent, and Skill selection paths have coverage.

### 22.4 Tool, approval, validation, repair, and changed files

- SAFE, WRITE, DANGEROUS, missing, denied, Plan-scope denied, native failure, and MCP failure;
- denied/missing calls increment Tool count exactly once;
- source mapping is native/MCP/unknown without changing policy;
- approval requested/resolved only around the persisted lifecycle, including denial;
- Replan count/version event remains unchanged and free-text reason is omitted remotely;
- Validation start/finish duration and nested validation Tool correlation;
- Repair start/model phase/count; and
- ChangedFileRecorded occurs only after accepted edit evidence, once per state update, with a
  contained normalized relative path.

### 22.5 Redaction and serialization

Use adversarial sentinel values in every forbidden category:

- model API key, LangSmith key, database password, proxy credential, and environment value;
- task/prompt/completion/reasoning/rationale;
- Tool args/results, command argv, stdout/stderr, MCP payload, patch/diff, file/Skill/chunk content;
- absolute Windows/POSIX paths, home directory, remote URL, raw HTTP/provider object; and
- exception message, traceback, locals, nested/oversized objects.

Assert sentinels are absent from TelemetryEvent, Console JSON, LangSmith mock calls, warning,
exception repr, and teardown output. Assert redaction metadata names categories only.

### 22.6 Tracers, logging, fallback, and configuration

- Console JSON Lines schema/order/concurrency and stderr behavior;
- LangSmith mapping using an SDK/client mock with empty/safe inputs and outputs;
- each enabled sink uses one per-execution FIFO worker/control queue with exact observable call
  order `start_run -> record* -> finish`, including delayed async calls that would expose a race;
- TraceStart is queued before the first record, terminal telemetry is recorded before TraceFinish,
  and no independent start/record/finish task exists;
- start failure produces no record/normal-finish call and exactly one warning;
- record failure disables the sink, discards later records/control items, permits only bounded
  adapter-private cleanup, and produces exactly one warning;
- finish failure preserves the business outcome and produces exactly one warning;
- queue overflow disables only that sink without Agent backpressure and preserves healthy-sink
  ordering;
- start/record/finish/flush failure at every point;
- LangSmith -> Console, Console -> LangSmith/NoOp, and all-sink failure;
- exactly one visible warning, stable codes, no raw exception, no business-status change;
- invalid explicit enablement fails bootstrap; disabled mode requires no credential/client;
- user/environment/repository precedence and secret-source rejection;
- API key/repr/database/endpoint sanitization; and
- Composition Root is the only concrete construction location.

### 22.7 Persistence and resume

- existing schema remains unchanged under the v0.2 resolution;
- reported token subtotal and completeness metadata persist truthfully;
- final counters equal AgentState/ledger semantics;
- interruption restores counter/token aggregate exactly without duplication;
- trace/run/session identity is stable, execution ID and sequence reset per segment; and
- resume creates a fresh lifecycle queue/state for every enabled sink, with no prior execution's
  controls or records entering the new queue; and
- checkpoint failure remains a business error, while trace failure remains a warning.

### 22.8 End-to-end and regression gates

- one deterministic nontrivial coding fixture produces, in order, run, exploration/context, Plan,
  approval if configured, agent/model, Tool/edit, changed file, validation, and terminal evidence;
- one normal unsuccessful task and one handled runtime failure are traced truthfully;
- prior Day 1–Day 7 unit/integration tests remain passing;
- full non-live pytest, Ruff, strict Mypy, lock check, dependency diff, and wheel verification pass;
- PostgreSQL integration and checkpoint-resume coverage pass separately; and
- the real LangSmith acceptance in section 23 is reported separately from mocks/local tests.

---

## 23. Real LangSmith acceptance path

Real acceptance is opt-in, secret-safe, and must not run merely because a credential happens to be
present.

Prerequisites after contract/implementation approval:

1. approved direct `langsmith` dependency and lockfile;
2. trusted non-repository API key, project, endpoint/workspace configuration;
3. a dedicated test marker such as `langsmith_e2e` excluded from default local tests;
4. a dedicated disposable Nexus fixture repository with no secrets or personal content;
5. explicit operator authorization to send the approved safe telemetry fields externally; and
6. LangSmith account/project access sufficient to query the produced trace.

Acceptance procedure:

1. run one nontrivial fixture task through Plan, at least one Tool/edit, validation, and terminal
   result;
2. capture Nexus run/trace/execution IDs without printing credentials;
3. flush the adapter within its bounded teardown path;
4. retrieve the resulting LangSmith root through the approved client/API;
5. verify one root plus correlated model, Tool, and validation children or mapped events;
6. verify plan/replan if applicable, approval if applicable, changed-file count/path, validation,
   counters, latency, token availability, and terminal status;
7. run the redaction sentinel assertion against the retrieved remote representation;
8. verify no raw prompt, completion, Tool args/results, file/diff content, environment, credential,
   or private reasoning exists remotely; and
9. record PASS/FAIL/NOT RUN, SDK version, sanitized project, trace/run IDs, and timestamp in the
   Observability Guide/run trace demo without the API key.

The acceptance artifact may include a redacted LangSmith URL or trace ID only if it contains no
secret. A screenshot is supporting evidence, not the sole machine-verifiable assertion.

Implementation must verify current SDK behavior against primary LangSmith documentation,
including [sensitive-data masking](https://docs.langchain.com/langsmith/mask-inputs-outputs) and
[explicit tracing configuration](https://docs.langchain.com/langsmith/trace-without-env-vars).
Those documents support client-level hiding/transformation and explicit configuration; they do
not replace the stricter Nexus allowlist in section 14.

---

## 24. Public contract impact matrix

Both Architect Review dispositions are incorporated below. Every row is **FROZEN IN V0.3 /
APPROVED FOR DAY 8 IMPLEMENTATION**.

| Public/additive contract | Impact | approved v0.3 resolution |
|---|---|---|
| `Tracer.start_run/record/finish` | new domain port | exact async signatures in section 6; provider handles remain adapter-private |
| `RunExecutionContext` | new Nexus-owned execution context | constructed once per `NexusRuntime.run/resume`; context-local propagation; consumers cannot invent identity |
| TraceRunStart / TraceRunFinish | new observability values | safe exact fields; execution outcome, precise RuntimeStatus, and TerminalStatus remain distinct |
| EventPublisher / EventSubscriber / EventSubscription | new application/domain boundary | ordered live Runtime/CLI lane plus isolated asynchronous telemetry sink lane |
| TelemetryEvent v1.0 / enums / RedactionMetadata | new external/log schema | separate strict-allowlist wrapper, never a RuntimeEvent alias |
| trace/execution/span identity | new correlation contract | `trace_id=run_id`; one new execution segment per run/resume |
| TokenUsage / UsageAvailability | new provider-neutral domain values | exact REPORTED/PARTIAL/UNAVAILABLE semantics; no estimation |
| ModelResponse/ModelChunk usage | additive defaulted fields | preserves existing content-only constructors |
| ObservedModelGateway / ModelCallPhase | new instrumentation seam | sole real-call event/count owner; uses RunExecutionContext; semantic `DIRECT_RESPONSE`, never `LEGACY_RESPONSE` |
| ModelCallStarted/Finished | new safe RuntimeEvents | exact content-free fields |
| AgentStepCompleted | new safe RuntimeEvent | decision enum/count only |
| ApprovalResolved | new safe RuntimeEvent | emitted only after persistence |
| ChangedFileRecorded | new safe RuntimeEvent | contained workspace-relative path; kind is only ADDED or MODIFIED |
| ValidationFinished.duration_ms | additive defaulted field | `int | None = None`; Day 8-observed paths populate non-negative monotonic duration |
| ObservabilityWarning | new non-terminal safe event | failure/overflow/fallback semantics without business-status change |
| Runtime live event distribution | private runtime mechanism with observable timing change | publisher-driven live iterator with unchanged public signature/order and no tracer backpressure |
| tool source enrichment | telemetry-only metadata | immutable Composition Root map; no Tool-domain/security change |
| RuntimeConfig observability fields | additive public configuration | exact names/defaults; remote LangSmith sources are trusted user config/environment only |
| direct `langsmith` dependency | dependency/lock change | exact range `langsmith>=0.11.1,<0.12` authorized for later Day 8 implementation; unchanged in this documentation turn |
| Run token persistence semantics | clarification using existing schema | reported subtotal plus explicit completeness |
| AgentState TokenUsageAggregate | additive defaulted checkpoint state | carries aggregate across resume; no database migration |
| CLI live summaries / ConsoleTracer | additive rendering and opt-in structured sink | human Renderer on by default; ConsoleTracer off by default and explicitly usable as stderr JSONL for acceptance/debug; no new CLI command |

No graph, Tool security, database schema, CLI command, retrieval, Skill, MCP, or Evaluation contract
change is proposed.

---

## 25. Explicit deferred scope

Day 8 does not define or authorize:

- OpenTelemetry exporter/SDK, OTLP, metrics backend, analytics warehouse, dashboard, alerting,
  sampling platform, distributed trace propagation, or trace search UI;
- private chain-of-thought, hidden reasoning, prompt/completion capture, session replay, screen/UI
  capture, or repository-content export;
- durable event/span/telemetry tables, event sourcing, audit-log replacement, provider root-ID
  persistence, or database migration;
- automatic global LangChain/LangGraph tracing, callbacks that bypass Nexus redaction, or direct
  Runtime imports of LangSmith;
- changes to graph topology, node responsibilities, Tool scheduling, retry behavior, policy,
  approvals, sandbox, validation, retrieval/ranking, Skills, MCP, or persistence ownership;
- Day 9 Evaluation cases/runner/reports, `nexus eval`, metric gates, or baselines;
- Day 10 release work, VSCode/Web UI, Multi-Agent, remote observability platform, or telemetry
  marketplace; and
- unrelated refactor, log rotation platform, retention policy, user analytics, billing/cost
  estimation, token-price calculation, or performance SLO.

Future adapters may implement the Tracer port only after their own approved scope. The word
`OpenTelemetryTracer` in architecture context is not Day 8 implementation authorization.

---

## 26. Architecture decision disposition

The second Architect Review accepts every v0.2 decision and freezes the final per-sink lifecycle
ordering clarification below. There is no known unresolved architecture choice in the approved
Day 8 contract.

| ID | Decision | v0.3 frozen resolution | Review status |
|---|---|---|---|
| AD-8-01 | RuntimeEvent vs TelemetryEvent | preserve separate business and versioned allowlisted telemetry layers | APPROVED |
| AD-8-02 | exact Tracer API | async Nexus port; provider handles adapter-private | APPROVED |
| AD-8-03 | publisher/subscriber lifecycle | ordered live Runtime/CLI lane plus isolated telemetry ingress and one FIFO lifecycle worker/control queue per sink/execution | APPROVED |
| AD-8-04 | live progress mechanism | in-process producer/queue without graph topology change | APPROVED |
| AD-8-05 | event coverage | minimum safe additions; ChangedFile kind ADDED/MODIFIED; nullable/defaulted validation duration | APPROVED |
| AD-8-06 | model usage contract | REPORTED/PARTIAL/UNAVAILABLE, no estimation, truthful subtotal/completeness | APPROVED |
| AD-8-07 | model call/counter owner | ObservedModelGateway is the single real-call instrumentation and `llm_call_count` owner | APPROVED |
| AD-8-08 | trace/resume identity | `trace_id=run_id`; new execution segment per run/resume; no migration | APPROVED |
| AD-8-09 | token usage across resume | additive defaulted AgentState TokenUsageAggregate in checkpoints | APPROVED |
| AD-8-10 | persisted unknown-token meaning | existing `token_count` is reported subtotal; final_outcome carries completeness | APPROVED |
| AD-8-11 | redaction policy | common strict event allowlist plus defense-in-depth SDK hiding | APPROVED |
| AD-8-12 | changed-file path privacy | contained workspace-relative changed-file path only; other paths are counts | APPROVED |
| AD-8-13 | Tool source contract | immutable Composition Root metadata map; UNKNOWN if unresolved | APPROVED |
| AD-8-14 | LangSmith mapping | manual sanitized Nexus adapter; global auto-tracing forbidden | APPROVED |
| AD-8-15 | disabled/invalid LangSmith behavior | disabled creates no client; invalid explicit trusted config fails safely before Run | APPROVED |
| AD-8-16 | tracer failure/latency | ordered per-sink failure handling; disable failed/overflowed sink; one warning; no Agent backpressure | APPROVED |
| AD-8-17 | Console UX/destination | human Renderer default; ConsoleTracer opt-in stderr JSONL for acceptance/debug; no CLI addition | APPROVED |
| AD-8-18 | structured logger boundary | sink-only; cannot enrich, redact, or decide | APPROVED |
| AD-8-19 | observability configuration | remote LangSmith key env-only; enable/project/endpoint/workspace trusted user config or env; repository has no remote authority | APPROVED |
| AD-8-20 | LangSmith dependency | direct `langsmith>=0.11.1,<0.12`; dependency change authorized only inside this frozen scope | APPROVED |
| AD-8-21 | schema/persistence | no telemetry database/migration; transient sinks plus existing Run fields/checkpoint aggregate | APPROVED |
| AD-8-22 | terminal semantics | separate execution_outcome, exact RuntimeStatus, and applicable TerminalStatus; preserve STOPPED_MAX_* | APPROVED |
| AD-8-23 | execution context | one RunExecutionContext per run/resume boundary; context-local propagation; fresh sink lifecycle state on resume | APPROVED |
| AD-8-24 | per-sink lifecycle ordering | exactly one FIFO worker/control queue per sink/execution: TraceStart -> TelemetryEvent record* -> TraceFinish; no lifecycle races | APPROVED |
| AD-8-25 | RuntimePhase and severity | exact section 8 enum, event mapping, separate ModelCallPhase dimension, and Day 8 V1 severity mapping | APPROVED |

Any later change to these public/provider/tool/database/security/retrieval/CLI/dependency/graph or
lifecycle contracts requires an approved specification/Addendum change.

---

## 27. Approval checklist

Final Architect Review record:

- [x] every v0.2 architecture decision is accepted and preserved;
- [x] parent Specification sections and all Day 8 18 items remain inherited;
- [x] graph topology, sequential Tool execution, Tool/security policy, and business semantics remain unchanged;
- [x] RuntimeEvent and TelemetryEvent remain separate, with exact async Tracer interfaces;
- [x] RunExecutionContext and concurrent async/resume propagation are frozen;
- [x] execution outcome, exact RuntimeStatus, and TerminalStatus are separate and STOPPED_MAX_* remains observable;
- [x] live Runtime/CLI delivery is isolated from asynchronous telemetry sink I/O/backpressure;
- [x] every enabled sink/execution has one FIFO queue and worker containing TraceStart, records, and TraceFinish;
- [x] RuntimePhase values, exact event mapping, separate ModelCallPhase dimension, and severity mapping are frozen;
- [x] start completes or safely fails before record; accepted records preserve publisher order; terminal record precedes finish;
- [x] start/record/finish cannot race; their failure/discard/cleanup behavior is frozen exactly by sections 7 and 17;
- [x] resume creates fresh per-sink lifecycle queues/state and tracer I/O remains outside the Agent critical path;
- [x] ChangedFile kind and ValidationFinished compatibility corrections are frozen;
- [x] ObservedModelGateway owns real-call instrumentation/counting and uses semantic phases;
- [x] token availability, no-estimation, subtotal/completeness, checkpoint aggregate, and no-migration decisions are preserved;
- [x] the strict allowlist and forbidden sensitive-data categories are approved by the first review;
- [x] manual sanitized LangSmith mapping, remote trust boundary, exact dependency proposal, and no global auto-tracing are frozen;
- [x] human Renderer default and opt-in ConsoleTracer stderr JSONL acceptance/debug behavior are frozen;
- [x] independent tracer isolation, sink-only logger, no OpenTelemetry, and no Day 9 work are preserved; and
- [x] deterministic, regression, PostgreSQL/resume, safety, lifecycle-race, and separately reported real LangSmith gates remain required;
- [x] Observability Guide and reproducible run trace demo remain required;
- [x] all deferred boundaries in section 25 remain unchanged; and
- [x] Day 8 implementation is authorized only within this approved contract.

Quality gates remain separate:

1. Acceptance Criteria;
2. Code & Architecture Review;
3. tests/CI plus separately reported live LangSmith evidence; and
4. Product Owner Knowledge Review covering Observability versus Evaluation, future UI value of
   typed events, safe correlation, and data that must never be shown.

No merge occurs before all gates and explicit review approval.

---

## 28. Implementation authorization status

```text
Architect Review:        APPROVED
Contract approval:       APPROVED
Implementation Plan:     AUTHORIZED
Production code:         AUTHORIZED FOR DAY 8 SCOPE
Test code:               AUTHORIZED FOR DAY 8 SCOPE
Database migration:      NOT AUTHORIZED / NOT REQUIRED
Dependency changes:      AUTHORIZED ONLY AS FROZEN BY DAY 8 CONTRACT
LangSmith integration:   AUTHORIZED WITH APPROVED SAFE BOUNDARY
Day 9 / Day 10 work:     NOT AUTHORIZED

IMPLEMENTATION AUTHORIZATION: YES — Day 8 only
```
