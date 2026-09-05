# Nexus Day 4 Contract Addendum v0.2.3

**Status:** APPROVED

**Approval:** APPROVED BY PRODUCT OWNER / ARCHITECT

**Implementation authority:** AUTHORIZED FOR DAY 4 IMPLEMENTATION

**Applies To:** Day 4 — Exploration, Planning, Editing & Validation

**Baseline:** Nexus V1 Specification v1.1.1 and approved Day 1–Day 3 Contract Addenda

This document incorporates the Architecture Review Decision on v0.1 and the Final
Consistency Review corrections on v0.2 and v0.2.1, and the approved minimal Planning
correlation patch on v0.2.2. Normative words such as MUST, SHALL, and MAY describe the
approved v0.2.3 contract only. The seven architecture decisions remain resolved, and this
document authorizes Day 4 implementation only.

## 0. Review status and authority

The authority order for this approved Addendum is:

1. `AGENTS.md`;
2. `docs/Nexus_V1_Product_Requirements_and_10-Day_Engineering_Specification_v1.1.1_中文.md`;
3. the approved Day 1 Contract Addendum;
4. the approved Day 2 Contract Addendum;
5. the approved Day 3 Contract Addendum;
6. current Day 1–Day 3 implementation reality.

The purpose of this approved Addendum is to freeze only the public contracts required to implement
Day 4 without an implementation engineer inventing architecture. It does not redesign the
Normative Graph, authorize Day 5 retrieval, add MCP/Skills/Evaluation, or approve code.

Architecture Review has resolved AD-4-01 through AD-4-07. Section 21 records the binding
decisions rather than an options list. Final document approval is complete.

## 1. Compatibility baseline

Day 4 shall extend the existing style:

- public values are frozen, slotted dataclasses and finite states are `StrEnum` values;
- identifiers are canonical UUID strings and timestamps are timezone-aware UTC;
- public provider/service ports are asynchronous `Protocol` contracts;
- `RuntimeEvent.to_dict()` remains the serialization boundary;
- all Tool inputs and outputs remain JSON-compatible Nexus-owned values;
- all repository reads, writes, commands, and Git inspection pass through Tool Runtime;
- Tool execution remains one awaited invocation at a time;
- concrete assembly remains under `src/nexus/infrastructure/bootstrap/`;
- Session/Run business persistence and LangGraph checkpoint ownership remain separate.

No Day 4 database migration and no new dependency are authorized. Plans, graph-local
counters, observations, validation state, and approval evidence are checkpoint state.
Existing `runs.tool_call_count`, `runs.changed_file_refs`, and `runs.final_outcome` hold the
terminal business summary already authorized by the Day 2 schema.

## 2. Domain and value models

### 2.1 Shared conventions

All tuple fields below are immutable ordered snapshots. Implementations may accept a
`Sequence` at construction but must store a tuple. Repository paths are non-empty,
workspace-relative, POSIX-style strings and use the Day 3 containment rules.

User-visible summaries must be concise and safe. They must not contain raw private model
reasoning, secrets, complete environments, or raw exceptions.

### 2.2 Plan enums

```python
class PlanKind(StrEnum):
    INITIAL = "INITIAL"
    REPLAN = "REPLAN"


class PlanStatus(StrEnum):
    CREATED = "CREATED"
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class PlanStepStatus(StrEnum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"
```

`ApprovalDecision` from Day 3 is reused as the Plan approval status; no duplicate approval
enum is added.

### 2.3 `PlanStep`

```python
@dataclass(frozen=True, slots=True)
class PlanStep:
    step_id: str
    sequence: int
    description: str
    tool_name: str | None
    target_paths: tuple[str, ...]
    command_argv: tuple[str, ...] | None
    command_cwd: str | None
    status: PlanStepStatus
```

Invariants:

- `step_id` and `description` are non-empty; `sequence >= 1`;
- step IDs and sequence values are unique inside a Plan; sequence is contiguous from 1;
- `tool_name`, when present, is one stable Tool name;
- `target_paths` contains no duplicates and is sorted for stable presentation;
- `command_argv` is non-empty only when `tool_name == "shell"`;
- `command_cwd` is non-null only with `command_argv`, and defaults to `"."` before Plan
  construction;
- an editing step uses `apply_patch` or `write_file` and has exactly one target path;
- a validation command step uses `shell` and freezes exact normalized argv and cwd;
- a narrative/checkpoint step may have `tool_name=None`, no paths, and no command;
- new Plans start with every step `PENDING`.

Tool argument bodies such as patch content are not stored in the Plan. Authorization is
the exact tuple `(tool_name, target_path)` for file writes or
`(tool_name="shell", command_argv, command_cwd)` for controlled commands.

Every WRITE action and validation command that may be authorized must therefore appear
explicitly in `Plan.steps`. A step description is human-readable context; only its typed
Tool/path or command fields contribute authority.

### 2.4 Authorization scope

Execution-step representation and authorization-scope representation are distinct, but
the authorized entries are an exact projection of the visible steps:

```text
AuthorizationScope
= exact deduplicated set of
  every WRITE (tool_name, target_path) declared by Plan.steps
  plus every validation (shell argv, cwd) declared by Plan.steps
```

```python
@dataclass(frozen=True, slots=True)
class AuthorizationScope:
    allowed_write_actions: tuple[tuple[str, str], ...]
    allowed_commands: tuple[tuple[tuple[str, ...], str], ...]
```

`allowed_write_actions` entries are exact `(tool_name, repository_relative_path)` pairs.
`allowed_commands` entries are exact `(normalized_argv, repository_relative_cwd)` pairs.
Both tuples are deduplicated and sorted. Planner may normalize, deduplicate, and sort only;
it must not add a WRITE action or validation command absent from `Plan.steps`, including
speculative Repair authority. The scope contains no patch body, file content, SAFE read,
or DANGEROUS operation.

For INITIAL and REPLAN, Planner derives `authorization_scope` solely from the complete
typed `Plan.steps` before `PlanCreated` and before approval. Exact equality between this
derivation and the stored scope is a Plan invariant. Once approved, the scope is immutable
for that `(plan_id, version)`.

### 2.5 `Plan`

```python
@dataclass(frozen=True, slots=True)
class Plan:
    plan_id: str
    run_id: str
    session_id: str
    version: int
    kind: PlanKind
    status: PlanStatus
    approval_status: ApprovalDecision
    steps: tuple[PlanStep, ...]
    authorization_scope: AuthorizationScope
    rationale_summary: str
    replan_reason: str | None
    approval_id: str | None
    scope_digest: str
    created_at: datetime
    approved_at: datetime | None
```

Invariants and version rules:

- IDs are canonical UUID strings; `version >= 1`; timestamps are UTC;
- a Plan has at least one step and a non-empty rationale summary;
- `authorization_scope` equals the exact normalized, deduplicated, sorted projection of all
  WRITE actions and validation commands explicitly declared by `steps` and contains no
  additional entry;
- `scope_digest` is the lowercase 64-character SHA-256 digest defined in section 2.6;
- the initial Plan is `kind=INITIAL`, `version=1`, `replan_reason=None`, and
  has its complete authorization scope;
- a material Replan keeps `plan_id`, increments `version` by exactly one, uses
  `kind=REPLAN`, has a non-empty `replan_reason`, and resets approval to `PENDING`;
- the superseded snapshot is retained in `AgentState.plan_history` with
  `status=SUPERSEDED`;
- `status=CREATED` requires `approval_status=PENDING` and no approval timestamp;
- `status=CREATED` also requires `approval_id=None`;
- `status=ACTIVE` requires `approval_status=APPROVED`, non-null `approval_id`, and approved
  Plan evidence;
- `approval_status=DENIED` requires `status=FAILED`;
- `SUPERSEDED`, `COMPLETED`, and `FAILED` are terminal for that Plan snapshot.

Material Replan means new observed evidence invalidated the current strategy, disproved a
critical assumption, or requires a different write path or validation command. Wording
changes, step-status updates, retries, and repairs inside approved scope are not Replans.
For Day 4 V1, section 7 freezes `PLAN_SCOPE_DENIED` as the sole operational evidence that
sets a material Replan reason; the broader description does not authorize another trigger.

The tuple `(plan_id, version)` identifies one versioned strategy. Replan is the only Day 4
operation that creates a new version. `plan_history` contains only superseded versioned
snapshots such as v1, v2, and v3; it never contains Repair attempts.

### 2.6 Approved Plan evidence

```python
class PlanAuthorizationSource(StrEnum):
    INTERACTIVE = "INTERACTIVE"
    AUTO_MODE = "AUTO_MODE"


@dataclass(frozen=True, slots=True)
class ApprovedPlanEvidence:
    plan_id: str
    plan_version: int
    run_id: str
    session_id: str
    source: PlanAuthorizationSource
    approval_id: str
    scope_digest: str
    authorization_scope: AuthorizationScope
    approved_at: datetime
```

Approval copies the Plan's immutable `authorization_scope` byte-for-byte into evidence.
`scope_digest` is lowercase SHA-256 over UTF-8 canonical JSON containing only `plan_id`,
`plan_version`, `authorization_scope.allowed_write_actions`, and
`authorization_scope.allowed_commands`; canonical JSON uses sorted object keys and compact
separators. The same scope and digest are stored on Plan and evidence, and the digest is
bound into the bounded approval summary:

```text
approve_plan:<plan_id>:v<version>:scope-sha256:<64-lowercase-hex>
```

Interactive and auto-mode evidence both require an existing terminal APPROVED approval
row with that exact correlation and summary. Evidence is valid only for the correlated
Run, Session, Plan version, and exact scope. It is not a general capability token and must
never authorize DANGEROUS execution.

After approval, `authorization_scope`, `scope_digest`, and `ApprovedPlanEvidence` are
immutable for that Plan version.

The scope digest is audit and correlation evidence only. It never substitutes for the
human-readable Plan steps, WRITE actions/targets, and validation commands that the user
must see before an interactive approval decision.

### 2.7 Repair guidance

```python
@dataclass(frozen=True, slots=True)
class RepairGuidance:
    plan_id: str
    plan_version: int
    repair_attempt: int
    steps: tuple[PlanStep, ...]
    failure_summary: str
```

RepairGuidance is an execution projection inside the current approved Plan version, not a
Plan and not a versioned strategy snapshot. `repair_attempt >= 1`; its steps may reference
only actions explicitly represented by the approved `Plan.steps` and therefore contained
in the current Plan's immutable authorization scope. It never owns or replaces
authorization scope, digest, approval ID, or ApprovedPlanEvidence.

Authorization is scoped to `(tool_name, target_path)` or an exact validation command, not
to patch or file-content bodies. Repair may therefore generate different patch content for
an already approved Tool/path action without a new Plan or approval.

A generated RepairGuidance that names a new write path, tool scope, or validation command
is malformed and is rejected with `REPAIR_SCOPE_EXPANSION`; rejection must not mutate the
current Plan or its approval evidence. It must not add that action to the old scope. If
subsequent Agent execution requires the new path, Tool action, or validation command, the
Agent proposes it and the `PLAN_SCOPE_DENIED -> observe -> material Replan` path in section
14 creates version N+1 with a new immutable scope/digest and new approval.

### 2.8 Changed-file values

```python
class ChangeKind(StrEnum):
    MODIFIED = "MODIFIED"
    ADDED = "ADDED"


@dataclass(frozen=True, slots=True)
class ChangedFile:
    path: str
    change_kind: ChangeKind
    first_invocation_id: str
    latest_invocation_id: str
```

A path enters this collection only after a successful `apply_patch` or `write_file` Tool
result. Paths are unique and sorted. Day 4 exposes no delete/rename Tool, so deleted and
renamed kinds are not invented. Repeated successful patches update only
`latest_invocation_id`.

## 3. Planning contract

### 3.1 Request and port

```python
@dataclass(frozen=True, slots=True)
class PlanningRequest:
    task: str
    context: WorkingContext
    kind: PlanKind
    previous_plan: Plan | None
    reason: str | None
    run_id: str
    session_id: str


@dataclass(frozen=True, slots=True)
class RepairPlanningRequest:
    task: str
    context: WorkingContext
    plan: Plan
    validation_result: ValidationResult
    repair_attempt: int


class Planner(Protocol):
    async def create_plan(self, request: PlanningRequest) -> Plan: ...

    async def create_repair_guidance(
        self,
        request: RepairPlanningRequest,
    ) -> RepairGuidance: ...
```

Rules:

- `run_id` and `session_id` are canonical UUID planning-operation correlation metadata;
  they are supplied by `create_plan` from current established AgentState and are not part
  of WorkingContext; Planner must never generate either identifier;
- `INITIAL` has no previous Plan or reason, and the returned Plan's Run/Session IDs equal
  the request IDs; any mismatch is `INVALID_PLAN_OUTPUT`;
- `REPLAN` requires the current Plan and a material, safe reason; request Run/Session IDs
  equal the previous Plan IDs, and the returned Plan keeps those same IDs while producing
  exactly the next version with a newly frozen authorization scope; any correlation
  mismatch is `INVALID_PLAN_OUTPUT`;
- `create_repair_guidance` requires the active approved Plan, failed ValidationResult, and
  positive repair attempt; it returns guidance whose every action is a member of the
  existing immutable authorization scope;
- one model call used by the Planner increments `llm_call_count` once, including a handled
  model failure;
- structured output is normalized into Nexus values before crossing this port;
- invalid/empty structured output raises `ModelError(code="INVALID_PLAN_OUTPUT")`;
- scope expansion in Repair guidance raises `ModelError(code="REPAIR_SCOPE_EXPANSION")`.

Planner owns planning only. It never invokes Tools, requests approval, runs validation,
routes the graph, mutates files, or increments graph counters itself.

## 4. Repository exploration contract

### 4.1 Values and port

```python
@dataclass(frozen=True, slots=True)
class ExplorationRequest:
    task: str
    run_id: str
    session_id: str


@dataclass(frozen=True, slots=True)
class RepositoryInstruction:
    path: str
    scope_path: str
    depth: int
    content: str
    truncated: bool


@dataclass(frozen=True, slots=True)
class RepositoryFileEvidence:
    path: str
    category: str
    discovery_reason: str
    summary: str


@dataclass(frozen=True, slots=True)
class ExplorationResult:
    instructions: tuple[RepositoryInstruction, ...]
    manifests: tuple[RepositoryFileEvidence, ...]
    top_level_paths: tuple[str, ...]
    relevant_files: tuple[RepositoryFileEvidence, ...]
    initial_git_status: ToolResult
    tool_results: tuple[ToolResult, ...]
    truncated: bool


class RepositoryExplorer(Protocol):
    async def explore(self, request: ExplorationRequest) -> ExplorationResult: ...
```

The Tool Runtime and workspace root are constructor dependencies; they are not supplied as
untrusted request fields. Explorer may invoke only `list_files`, `search_files`,
`read_file`, `lexical_search`, and SAFE `git_status`. Evidence fields and summaries are
non-empty; summaries are bounded safe descriptions/excerpts, not complete file copies.

### 4.2 Exact Day 4 selective sequence

1. call SAFE `git_status` to establish the pre-run dirty-state evidence;
2. call non-recursive `list_files` at repository root;
3. read root `AGENTS.md` when present;
4. inspect existing root `.nexus/config.toml`, README variants, `pyproject.toml`,
   `package.json`, `pom.xml`, and other root manifest files identified by the top-level
   listing;
5. derive at most eight non-empty literal search terms from explicit path-like tokens and
   identifier-like task tokens, preserving task order and removing duplicates;
6. use bounded filename and lexical search and retain at most twelve relevant file paths;
7. for each retained file, inspect only its ancestor directories for nested `AGENTS.md`;
8. return evidence; file contents for selected code are read by Context Builder.

Bounds:

- at most 24 Tool invocations in exploration;
- at most 12 relevant files and 8 manifests;
- no recursive file-content read and no whole-repository dump;
- no semantic search, rank/merge stage, pgvector, index, chunker, or `.gitignore`-complete
  Day 5 retrieval implementation;
- every retained relevant file has a non-empty discovery reason tied to the task,
  manifest, or observed search match.

Failure to obtain sufficient evidence returns the evidence collected so far with
`truncated=True`; a total inability to inspect the workspace raises
`ContextError(code="REPOSITORY_EXPLORATION_FAILED")`.

### 4.3 `AGENTS.md` scope and precedence

Day 4 supports root and selectively discovered nested `AGENTS.md` files without scanning
the repository for all such files.

- root `AGENTS.md` scope is the entire repository;
- nested scope is its containing directory and descendants;
- for a target file, instructions apply from root to deepest containing directory;
- a deeper instruction overrides a parent only for a direct conflict in that file scope;
- non-conflicting parent and child instructions are cumulative;
- a file with no applicable nested instruction uses the root instruction only;
- incompatible instructions applying to different target files remain per-file scoped and
  are not flattened into one global instruction;
- contradictions at the same depth, or a child instruction attempting to weaken Nexus
  security/Frozen Baseline rules, fail closed with
  `ContextError(code="REPOSITORY_INSTRUCTION_CONFLICT")`.

Nexus safety, the Frozen Baseline, and the current explicit user task remain higher
authority than target-repository instructions. Repository instructions may narrow allowed
work but may not authorize forbidden operations or future scope.

## 5. Day 4 context-build contract

```python
@dataclass(frozen=True, slots=True)
class SelectedFileContext:
    path: str
    content: str
    applicable_instruction_paths: tuple[str, ...]
    discovery_reason: str
    truncated: bool


@dataclass(frozen=True, slots=True)
class WorkingContext:
    task: str
    repository_instructions: tuple[RepositoryInstruction, ...]
    manifest_summaries: tuple[RepositoryFileEvidence, ...]
    top_level_paths: tuple[str, ...]
    selected_files: tuple[SelectedFileContext, ...]
    truncated: bool


@dataclass(frozen=True, slots=True)
class ContextBuildRequest:
    task: str
    exploration: ExplorationResult
    run_id: str
    session_id: str


class ContextBuilder(Protocol):
    async def build(self, request: ContextBuildRequest) -> WorkingContext: ...
```

Context Builder may use only `read_file` through Tool Runtime and reads at most eight
selected files, 400 lines per call, and 48,000 retained characters total across selected
code. It preserves complete recent Tool observations separately in AgentState rather than
copying them into this seed.

Day 4 context contains no embeddings, chunks, hybrid rank, index state, selected Skills,
or broad session-history injection. Selection is deterministic from ExplorationResult;
model-driven retrieval and compaction remain Day 5.

## 6. Structured Agent decision contract

### 6.1 Decision values

```python
class AgentDecisionKind(StrEnum):
    TOOL_ACTION = "TOOL_ACTION"
    CONTINUE = "CONTINUE"
    TASK_READY = "TASK_READY"


@dataclass(frozen=True, slots=True)
class ToolAction:
    tool_name: str
    arguments: JsonObject


@dataclass(frozen=True, slots=True)
class AgentDecision:
    kind: AgentDecisionKind
    action: ToolAction | None
    summary: str
```

Invariants:

- summary is non-empty, safe, user-visible, and contains no private reasoning;
- `TOOL_ACTION` has exactly one action;
- `CONTINUE` and `TASK_READY` have no action;
- a response containing zero or multiple actions is invalid;
- a Tool Action is converted to a new Day 3 `ToolInvocation` using graph-owned invocation,
  Run, and Session UUIDs;
- execution remains `Agent -> one Tool -> observe -> next Agent decision`.

Direct `REPLAN` is deliberately absent. The frozen graph allows Replan only after
`observe`; the model may describe a disproved assumption in its safe summary, but the only
Day 4 result that sets `Observation.replan_reason` is `PLAN_SCOPE_DENIED`.

### 6.2 Agent decision adapter

The frozen adapter contract is:

```python
@dataclass(frozen=True, slots=True)
class AgentDecisionRequest:
    task: str
    context: WorkingContext
    plan: Plan
    observations: tuple[Observation, ...]
    validation_result: ValidationResult | None = None
    repair_guidance: RepairGuidance | None = None


class AgentDecisionAdapter(Protocol):
    async def decide(self, request: AgentDecisionRequest) -> AgentDecision: ...
```

The adapter is an Agent-layer structured-decision boundary implemented over the unchanged
`ModelGateway.complete(...)`. It parses a strict Nexus JSON schema with the standard
library and maps invalid output to `ModelError(code="INVALID_AGENT_DECISION")`. Initial
execution leaves the two optional repair fields null; repair execution supplies the failed
ValidationResult and current RepairGuidance. No structured-output method is added to the
Day 1 ModelGateway, and no LangChain or provider Tool Call object enters domain state.

## 7. AgentState and counters

```python
@dataclass(frozen=True, slots=True)
class Observation:
    invocation_id: str
    tool_name: str
    success: bool
    evidence_summary: str
    error_code: str | None
    replan_reason: str | None


@dataclass(frozen=True, slots=True)
class AgentState:
    task: str
    messages: list[ModelMessage]
    run_id: str
    session_id: str | None
    status: RuntimeStatus
    exploration: ExplorationResult | None
    context: WorkingContext | None
    plan: Plan | None
    plan_history: tuple[Plan, ...]
    observations: tuple[Observation, ...]
    tool_results: tuple[ToolResult, ...]
    pending_tool_action: ToolAction | None
    latest_tool_result: ToolResult | None
    pending_plan_approval: ApprovalRequest | None
    approved_plan: ApprovedPlanEvidence | None
    repair_guidance: RepairGuidance | None
    validation_result: ValidationResult | None
    changed_files: tuple[ChangedFile, ...]
    step_count: int
    tool_call_count: int
    llm_call_count: int
    replan_count: int
    repair_count: int
    terminal_status: TerminalStatus | None
```

Initial Day 4 values are empty tuples, nullable values `None`, and counters zero.
`session_id` remains nullable only for Day 1 constructor compatibility; a Day 4 coding run
requires the SessionService to establish a non-null Session before exploration.

### 7.1 Deterministic observation

The Day 4 `observe` node is deterministic. It constructs one Observation from the pending
Agent-proposed ToolAction, its ToolResult, the current Plan, and the current
ApprovedPlanEvidence/AuthorizationScope. It does not call ModelGateway, introduce an
Observer provider, invoke another Tool, or apply model-derived Replan heuristics.

The V1 material-Replan trigger is exact:

```text
ToolResult.error.code == "PLAN_SCOPE_DENIED"
-> Observation.replan_reason MUST be non-null
-> replan_required? = true
```

The reason is a concise safe statement that the next required Agent action was outside the
currently approved Plan scope. It contains no private reasoning, patch body, command
output, or secret. For every ordinary in-scope Tool success or failure,
`replan_reason=None`. This Addendum freezes no other Day 4 Replan trigger, so
implementation code must not infer one.

`invocation_id`, `tool_name`, `success`, and `error_code` mirror the ToolResult exactly;
`error_code=None` when `ToolResult.error is None`. `evidence_summary` is a bounded safe
summary of the action and result and never changes the routing semantics above.

### 7.2 Counter semantics

- increment `step_count` once on entry to each model-driven `agent_step` decision cycle;
- an "actual Tool invocation" means a `ToolInvocation` accepted into
  `ToolRuntime.execute` for governed processing;
- increment `tool_call_count` exactly once immediately before governed processing begins;
  the count includes policy denial, approval denial, `PLAN_SCOPE_DENIED`, Tool argument or
  resource validation failure, native Tool failure, Sandbox failure, and success; it does
  not require the native Tool body, subprocess, or filesystem side effect to start;
- increment `llm_call_count` immediately before every `ModelGateway.complete` call made by
  Planner or AgentDecisionAdapter, including a failed call;
- `observe` never increments `llm_call_count` because it is deterministic and makes no
  ModelGateway call;
- increment `replan_count` only after a replacement Plan is successfully generated;
- increment `repair_count` when a new repair attempt enters `repair_plan`, before its
  planning call.

Day 2 already persists `Run.tool_call_count`; other Day 4 counters are additive checkpoint
state and terminal `Run.final_outcome` metrics. No schema migration is authorized.

The explicit graph route `execute_tool -> observe` is mandatory for every Agent-proposed
ToolAction. RepositoryExplorer, ContextBuilder, ValidationRunner, and final diff collection
may invoke Tool Runtime sequentially inside their frozen node responsibilities. Those
lifecycle calls still increment `tool_call_count`, emit `ToolStarted`/`ToolFinished`, and
retain structured evidence, but do not traverse the explicit LangGraph `execute_tool` or
`observe` nodes.

## 8. Graph routing and terminal contract

### 8.1 Route values

```python
class AgentRoute(StrEnum):
    TOOL_REQUIRED = "TOOL_REQUIRED"
    CONTINUE = "CONTINUE"
    TASK_READY = "TASK_READY"


class ObserveRoute(StrEnum):
    REPLAN_REQUIRED = "REPLAN_REQUIRED"
    CONTINUE = "CONTINUE"


class ValidationRoute(StrEnum):
    PASSED = "PASSED"
    REPAIR_AVAILABLE = "REPAIR_AVAILABLE"
    FAILED = "FAILED"
```

Predicates are deterministic and side-effect free:

- `tool_required?` is true exactly for `AgentDecisionKind.TOOL_ACTION`;
- `task_ready?` is true exactly for `TASK_READY`;
- `replan_required?` is exactly:

  ```text
  latest_observation.error_code == "PLAN_SCOPE_DENIED"
  AND latest_observation.replan_reason is not None
  ```

  The Observation invariant in section 7 makes this true for every `PLAN_SCOPE_DENIED`
  result and false for every ordinary in-scope Tool result;
- `validation_passed?` is true only for `ValidationStatus.PASS`;
- `repair_available?` is exactly:

  ```text
  validation_result.status == FAIL
  AND validation_result.repairable == True
  AND repair_count < max_repair_attempts
  ```

  No graph node infers an additional repairability policy. Anticipated scope insufficiency
  does not add a direct `validate -> create_plan` edge; it is resolved through the section
  14 path.

### 8.2 Terminal values

```python
class TerminalStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    FAILED_APPROVAL_DENIED = "FAILED_APPROVAL_DENIED"
    FAILED_VALIDATION = "FAILED_VALIDATION"
    FAILED_VALIDATION_UNKNOWN = "FAILED_VALIDATION_UNKNOWN"
    STOPPED_MAX_STEPS = "STOPPED_MAX_STEPS"
    STOPPED_MAX_REPAIRS = "STOPPED_MAX_REPAIRS"
    STOPPED_MAX_REPLANS = "STOPPED_MAX_REPLANS"
```

Limit checks:

- before entering `agent_step`, `step_count >= max_steps` stops without another model call;
- when observation requires Replan, `replan_count >= max_replans` stops without calling
  Planner;
- after validation failure, `repair_count >= max_repair_attempts` stops without entering
  another repair;
- `max_repair_attempts=3` permits attempts 1, 2, and 3; a failure after attempt 3 stops.

No stopped status may emit or persist success.

Architecture Review authorizes exactly two consistency-amendment routes:

```text
agent_step router
-> max steps exhausted
-> finalize_failed

observe router
-> Replan required but max replans exhausted
-> finalize_failed
```

No other ordinary lifecycle node, responsibility, or edge changes.

## 9. Plan approval, checkpoint, and resume

### 9.1 Interactive lifecycle

The exact lifecycle is:

```text
create_plan
-> persist PENDING ApprovalRequest(operation="approve_plan", risk=WRITE)
-> approval_gate emits ApprovalRequested
-> LangGraph checkpoint contains complete AgentState
-> interrupt
-> Runtime marks Run INTERRUPTED
-> external adapter supplies PlanApprovalResumeInput
-> GraphRuntime resumes the same thread
-> persist terminal approval decision
-> approved: construct ApprovedPlanEvidence and enter agent_step
-> denied: finalize_failed(FAILED_APPROVAL_DENIED)
```

```python
@dataclass(frozen=True, slots=True)
class PlanApprovalResumeInput:
    decision: ApprovalDecision
    reason: str | None


class GraphRuntime(Protocol):
    async def run(
        self,
        state: AgentState,
        *,
        thread_id: str | None = None,
    ) -> AgentState: ...

    async def resume(
        self,
        *,
        thread_id: str,
        resume_input: PlanApprovalResumeInput | None = None,
    ) -> AgentState: ...
```

`resume_input=None` remains valid for Day 2 checkpoints. If the checkpoint is waiting for
Plan approval, a missing decision leaves it interrupted and re-emits the pending request;
it never implies approval. Only APPROVED or DENIED is accepted.

Plan approval persistence is isolated from the Day 3 per-Tool ApprovalService:

```python
class PlanApprovalService:
    async def create_pending(self, plan: Plan) -> ApprovalRequest: ...

    async def create_auto_approved(self, plan: Plan) -> ApprovalRequest: ...

    async def persist_user_decision(
        self,
        approval_id: str,
        decision: PlanApprovalResumeInput,
    ) -> ApprovalRequest: ...

    async def require_approved(
        self,
        evidence: ApprovedPlanEvidence,
    ) -> ApprovalRequest: ...
```

All four methods use the existing ApprovalUnitOfWork and validate Run/Session correlation.
They add no repository port or table. `create_pending` writes operation `approve_plan`,
risk WRITE, the canonical digest summary from section 2.6, actor `runtime`, and PENDING.
`create_auto_approved` writes the same identity/scope with APPROVED, actor `auto_policy`,
and one UTC creation/decision timestamp. `persist_user_decision` permits one PENDING to
APPROVED/DENIED transition and sets actor `user`. `require_approved` requires matching
approval ID, Run, Session, operation, summary digest, APPROVED decision, and allowed actor.
Before `create_pending` or `create_auto_approved` persists authority, PlanApprovalService
must verify that AuthorizationScope is the exact section 2.4 projection of `Plan.steps`; a
mismatch is an invalid Plan and must not emit ApprovalRequested or create approval evidence.

The Day 3 `ApprovalRequest` invariant is additively extended only as follows:

```text
APPROVED actor is "user"
OR
APPROVED actor is "auto_policy" and operation is exactly "approve_plan"
```

Direct Tool WRITE passed to Day 3 `AutoApprovalPolicy` remains denied.

The compatible Runtime extension is:

```python
async def NexusRuntime.resume(
    self,
    session_id: str,
    *,
    resume_input: PlanApprovalResumeInput | None = None,
) -> AsyncIterator[RuntimeEvent]: ...
```

Existing `resume(session_id)` consumers remain source-compatible.

### 9.2 CLI behavior

No new command or required positional argument is authorized.

Before collecting APPROVED or DENIED for any Plan, both the initial `nexus chat` adapter
and the explicit session-resume adapter must render a safe human-readable approval view
containing at least:

- Plan step summaries in sequence order;
- every WRITE Tool action and target path;
- every validation command with exact argv and cwd.

The view must correspond exactly to the Plan whose scope digest is being approved. The
SHA-256 digest is displayed or retained as audit/correlation evidence but is not a
substitute for human-readable scope. The view exposes no patch body, file content, secret,
raw exception, or private reasoning.

For an initial interactive coding run, the existing `nexus chat` command follows this
exact adapter flow:

```text
nexus chat <task>
-> NexusRuntime.run(...)
-> approval_gate
-> ApprovalRequested
-> LangGraph interrupt/checkpoint
-> Runtime marks execution interrupted
-> CLI receives and renders the typed event and safe Plan
-> CLI collects APPROVED or DENIED
-> CLI calls NexusRuntime.resume(
     session_id,
     resume_input=PlanApprovalResumeInput(...)
   )
-> the same CLI command continues rendering RuntimeEvents
```

LangGraph may interrupt without requiring the CLI process or `nexus chat` command to
terminate. The CLI renders, collects user input, invokes Runtime, and continues event
rendering; it performs no graph orchestration, checkpoint access, approval persistence, or
business-state transition.

The existing recovery command remains:

```text
nexus session resume <session_id>
```

It is used for cross-process recovery, an approval left pending after CLI/process
termination, and ordinary Day 2 interrupted runs. When Runtime re-emits a pending Plan
approval, this adapter renders the same safe Plan/request, collects APPROVED/DENIED, and
calls `NexusRuntime.resume` with the same typed input. Non-approval Day 2 checkpoints
resume unchanged. Terminal input remains outside Runtime, graph nodes, policies, and
services.

### 9.3 Material Replan

A material Replan supersedes the current Plan, creates version `N+1`, clears old approved
evidence, and always returns through `approval_gate`. Approval of version N never
authorizes version N+1.

## 10. Plan approval versus WRITE approval

Architecture Review freezes scoped Plan authority. One approved Plan authorizes only exact
`apply_patch`/`write_file` actions and exact validation argv/cwd contained in its immutable
AuthorizationScope. An in-scope ordinary WRITE does not request a second interactive
approval.

An out-of-scope operation returns `PLAN_SCOPE_DENIED`, produces observation evidence, and
requires material Replan, Plan version N+1, a newly frozen scope/digest, and new approval.
It must not execute and must not be converted into Repair. DANGEROUS remains hard-denied.

Interactive mode persists one Day 3 `ApprovalRequest` for the Plan. Individual actions are
audited by `ToolStarted`/`ToolFinished` and changed-file/validation evidence. Existing
Day 3 isolated WRITE calls without Plan evidence retain their approval-plus-`PLAN_REQUIRED`
behavior.

In `approval_mode="auto"`, ordinary approval is skipped as allowed by the Frozen Baseline.
The approval gate immediately persists an APPROVED `approve_plan` row with
`actor="auto_policy"` and constructs AUTO_MODE evidence; there is no user interaction or
checkpoint pause. Scope checks still apply and DANGEROUS remains denied. Auto mode is
therefore not unrestricted permission.

The `auto_policy` APPROVED exception applies only to `operation="approve_plan"`. Direct
WRITE governed by the Day 3 AutoApprovalPolicy remains DENIED.

## 11. Tool Runtime Day 4 extension

### 11.1 Compatible signature

```python
class ToolRuntime:
    async def execute(
        self,
        invocation: ToolInvocation,
        *,
        authorization: ApprovedPlanEvidence | None = None,
    ) -> ToolResult: ...
```

The existing one-argument call remains valid. `ToolInvocation` and Tool/ToolResult shapes
remain unchanged.

For each governed WRITE:

1. no evidence follows the exact Day 3 behavior;
2. evidence correlation, digest, and approved status are validated against the persisted
   ApprovalRequest identified by `authorization.approval_id`;
3. Tool Runtime recomputes the digest from `authorization.authorization_scope` and
   requires exact equality with `authorization.scope_digest` and the approval summary;
4. `apply_patch`/`write_file` require exact `(tool_name, path)` membership in
   `authorization_scope.allowed_write_actions`;
5. `shell` requires exact normalized `(argv, cwd)` membership in
   `authorization_scope.allowed_commands`;
6. DANGEROUS is denied before authorization can be considered;
7. valid scope sets `policy_decision=ALLOWED` and calls the Tool once;
8. invalid, expired, mismatched, or out-of-scope evidence returns `PLAN_SCOPE_DENIED`
   without Tool execution.

Tool Runtime validates authorization but never generates or edits a Plan and never decides
whether Replan is needed.

The optional keyword is the sole Day 4 authorization transport. Day 3 ToolInvocation keeps
exactly its frozen five fields; no implicit context-variable authorization is permitted.

## 12. Editing Tool contracts

### 12.1 `apply_patch`

Stable name: `apply_patch`. It modifies exactly one existing UTF-8 text file.

Arguments:

```python
{
    "path": str,   # required repository-relative existing regular file
    "patch": str,  # required non-empty single-file unified diff
}
```

Patch restrictions:

- headers are exactly `--- a/<path>` and `+++ b/<path>` and must match `path`;
- one or more standard `@@` hunks are required;
- context and removed lines must match the current file exactly;
- rename, delete, mode change, binary patch, multi-file patch, and path traversal are
  rejected;
- input and resulting file are each at most 1,048,576 bytes;
- newline style and final-newline state are preserved unless the patch explicitly changes
  them;
- replacement is atomic after full parse, containment, and hunk validation;
- an empty/no-op result fails with `PATCH_NO_CHANGES` and does not count as changed.

Success output:

```python
{
    "path": str,
    "change_kind": "MODIFIED",
    "bytes_before": int,
    "bytes_after": int,
    "sha256_before": str,
    "sha256_after": str,
}
```

Mismatch returns `PATCH_CONFLICT`; missing target returns `FILE_NOT_FOUND`; invalid syntax
returns `INVALID_PATCH`; workspace/security failures retain Day 3 codes. Failure performs
no partial write.

### 12.2 `write_file`

Stable name: `write_file`. It creates exactly one new UTF-8 text file.

Arguments:

```python
{
    "path": str,     # required repository-relative absent file
    "content": str,  # required; empty content is allowed
}
```

Restrictions:

- target must not already exist, including a symlink or junction;
- parent directory must already exist, be contained, and not be a link/junction escape;
- parent directories are never created implicitly;
- encoded content is at most 1,048,576 bytes;
- creation is exclusive/atomic and never overwrites a race-created file;
- it is not a replacement path for existing files of any size.

Success output:

```python
{
    "path": str,
    "change_kind": "ADDED",
    "bytes_written": int,
    "sha256": str,
}
```

Existing collision returns `FILE_ALREADY_EXISTS`; missing parent returns
`PARENT_DIRECTORY_MISSING`; failure leaves no partial file.

Both Tools reuse the Day 3 Tool port/result, containment, risk escalation, event pair, and
Tool Registry. CommandPolicy classifies both stable names as WRITE before execution.

## 13. Validation contract

### 13.1 Values

```python
class ValidationStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


class ValidationConfidence(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class ValidationCheckKind(StrEnum):
    TEST = "TEST"
    BUILD = "BUILD"
    LINT = "LINT"
    TYPE_CHECK = "TYPE_CHECK"
    REPOSITORY_COMMAND = "REPOSITORY_COMMAND"
    GENERATED_TARGETED_TEST = "GENERATED_TARGETED_TEST"
    BASIC_EXECUTION = "BASIC_EXECUTION"
    DIFF_INSPECTION = "DIFF_INSPECTION"


@dataclass(frozen=True, slots=True)
class ValidationCheck:
    check_id: str
    sequence: int
    kind: ValidationCheckKind
    tool_name: str
    arguments: JsonObject
    selection_reason: str
    required: bool


@dataclass(frozen=True, slots=True)
class ValidationCheckResult:
    check: ValidationCheck
    status: ValidationStatus
    tool_result: ToolResult | None
    evidence_summary: str


@dataclass(frozen=True, slots=True)
class ValidationPlan:
    checks: tuple[ValidationCheck, ...]


@dataclass(frozen=True, slots=True)
class ValidationResult:
    selected_checks: tuple[ValidationCheck, ...]
    executed_checks: tuple[ValidationCheckResult, ...]
    status: ValidationStatus
    confidence: ValidationConfidence
    repairable: bool
    repair_count: int
    summary: str
```

IDs/reasons/summaries are non-empty, sequence is unique and contiguous, Tool arguments are
JSON-compatible, and `repair_count >= 0`. If `status != ValidationStatus.FAIL`,
`repairable` must be false.

### 13.2 Ports

```python
class ValidationPlanner(Protocol):
    async def plan(
        self,
        *,
        task: str,
        plan: Plan,
        exploration: ExplorationResult,
        context: WorkingContext,
        changed_files: tuple[ChangedFile, ...],
    ) -> ValidationPlan: ...


class ValidationRunner(Protocol):
    async def run(
        self,
        plan: ValidationPlan,
        *,
        run_id: str,
        session_id: str,
        authorization: ApprovedPlanEvidence,
        repair_count: int,
    ) -> ValidationResult: ...
```

Validation Planner is deterministic in Day 4 and makes no model call. It selects the
smallest relevant checks in frozen priority order:

```text
existing relevant test -> build/compile -> lint -> type check
-> repository-defined command -> generated targeted test
-> basic syntax/execution -> diff inspection
```

It may select only commands already present in approved Plan scope. Generated tests are
created earlier through an approved `write_file` Agent action; Validation Planner never
writes a test. Any changed-file run includes diff inspection.

ValidationPlanner identifies the smallest relevant checks without owning programming-
language or command-policy knowledge. A repository-defined command may be represented by
a ValidationCheck when it is relevant and present in the approved AuthorizationScope.

ValidationRunner submits executable checks sequentially to ToolRuntime. ToolRuntime and
CommandPolicy alone decide whether the command family is permitted. Day 4 does not expand
the Day 3 command matrix. A check denied by the current policy is not passed to Sandbox;
its ValidationCheckResult is UNKNOWN and confidence follows the UNKNOWN rules. The current
Python fixture may execute its already permitted pytest/ruff/mypy/uv families normally,
but those family names are not architectural knowledge in ValidationPlanner or
ValidationRunner.

Validation Runner records each governed Tool result. Aggregation is exact:

- any required `COMMAND_EXIT_NONZERO` check is `FAIL`;
- timeout, unavailable executable, denied authorization, truncated evidence that prevents
  a conclusion, or Tool infrastructure failure is `UNKNOWN`;
- a successful conclusive check is `PASS`;
- overall PASS requires at least one conclusive behavioral/build check when code changed,
  all required checks PASS, and diff inspection PASS;
- any required FAIL makes overall FAIL;
- otherwise overall status is UNKNOWN;
- UNKNOWN or insufficient evidence always has LOW confidence;
- PASS may be MEDIUM or HIGH according to check coverage; tests alone are evidence, not
  proof;
- Validation failure or UNKNOWN never yields successful FinalResult.

ValidationRunner deterministically aggregates `repairable` from ValidationCheckResults; no
graph node, Planner, Agent, model, stdout semantic classifier, regex heuristic, or
implementation-specific inference owns a second repairability policy.

A failed ValidationCheckResult is mechanically repairable exactly when all of these are
true:

```text
check_result.status == FAIL
AND check_result.check.kind IN {
    TEST,
    BUILD,
    LINT,
    TYPE_CHECK,
    GENERATED_TARGETED_TEST,
    BASIC_EXECUTION,
}
AND check_result.tool_result is not None
AND check_result.tool_result.error is not None
AND check_result.tool_result.error.code == "COMMAND_EXIT_NONZERO"
```

This ToolResult represents a governed validation command that executed normally and
completed with a non-zero exit. No stdout or stderr content is interpreted. A
`REPOSITORY_COMMAND` is not mechanically repairable in Day 4. This Addendum freezes no
mapping from it to another repairable check contract, and no semantic mapping from its name
or output to another check kind is permitted.

A failed check is always non-repairable when it is DIFF_INSPECTION or when its result is a
policy/approval denial, `PLAN_SCOPE_DENIED`, timeout, unavailable executable/check, Tool or
Sandbox infrastructure failure, truncated/insufficient evidence, UNKNOWN, or
`DIFF_UNAVAILABLE`.

Overall aggregation is exact:

```text
ValidationResult.repairable == True
IFF
ValidationResult.status == FAIL
AND at least one required failed ValidationCheckResult is mechanically repairable
AND no required failed ValidationCheckResult is non-repairable
```

Otherwise `ValidationResult.repairable == False`.

## 14. Repair and Replan semantics

Repair flow is entered only for a repairable validation failure while attempts remain.
RepairGuidance itself may project only actions inside the approved strategy's immutable
scope. If execution instead establishes that the strategy or scope is insufficient, the
governed denial and Observation trigger material Replan as defined below.

```text
FAIL + repairable + repair_count < limit
-> increment repair_count
-> emit RepairStarted
-> repair_plan creates RepairGuidance for the current Plan version
-> agent_step
-> validate again
```

Repair:

- keeps `plan_id`, `plan_version`, AuthorizationScope, `scope_digest`, and
  ApprovedPlanEvidence byte-for-byte equivalent;
- stores only the current RepairGuidance in `AgentState.repair_guidance`; the current Plan
  and `plan_history` are not replaced or appended;
- may edit only actions and run only commands in the immutable approved scope;
- does not increment `replan_count` and does not re-enter approval;
- may not convert UNKNOWN infrastructure evidence into an assumed implementation defect;
- may not expand changed-file or Tool scope;
- stops with `STOPPED_MAX_REPAIRS` after the configured attempts are consumed.

If a repair-focused Agent proposes a new write path, Tool scope, or validation command,
ToolRuntime returns `PLAN_SCOPE_DENIED` without execution. Deterministic `observe` records
the required safe non-null material Replan reason, and the result follows the existing
`execute_tool -> observe -> create_plan` route to version N+1 and new approval. The
out-of-scope action is never added to RepairGuidance or executed under old evidence.

The minimum V1 Replan mechanism is that the Agent interprets evidence and continues Agent
decisions while the approved strategy is sufficient. When the Agent requires an action
outside the approved scope, it proposes that ToolAction; ToolRuntime returns
`PLAN_SCOPE_DENIED`; deterministic `observe` records the material reason; and
`replan_required?` routes to `create_plan`. No Observer model call or additional semantic
Replan heuristic is authorized.

Replan increments version/count, emits `ReplanOccurred`, clears authorization, and returns
through approval. The `validate` node cannot route directly to Replan under the frozen
topology. A failure may initially be classified repairable because the available validation
evidence supports an in-scope correction. If repair execution later establishes that the
required Agent action is outside scope, that action follows the deterministic
denial/observe route. Scope expansion must never be inserted into RepairGuidance or
executed under the old approval.

## 15. Exact diff and changed-file semantics

`changed_files` is run-owned evidence from successful editing Tools, not every pre-existing
dirty path reported by Git.

Final diff collection is sequential and uses Tool Runtime:

1. `git_status` records final repository state;
2. `git_diff(staged=False)` returns the exact tracked working-tree diff;
3. each run-created untracked file is read through one or more bounded `read_file` calls
   until `truncated=False` and represented as a standard unified new-file patch from
   `/dev/null`;
4. every read and Git call increments `tool_call_count` and retains Tool evidence;
5. no graph node invokes Git subprocess directly.

Tracked modifications use Git's exact output, including any pre-existing unstaged changes
to the same file. FinalResult must set `includes_preexisting_changes=True` when the initial
SAFE `git_status` showed a dirty workspace; it must never attribute the entire diff solely
to Nexus in that case.

New text files use:

```text
diff --git a/<path> b/<path>
new file mode 100644
--- /dev/null
+++ b/<path>
@@ -0,0 +1,N @@
+<each exact line>
```

and preserve the `No newline at end of file` marker when applicable.

Cases:

- no successful edits: `changed_files=()`, `diff=""`;
- tracked modified file: exact Git diff is returned;
- newly created UTF-8 file: deterministic new-file patch is appended;
- binary/unsupported new file: impossible through Day 4 text-only WriteFileTool; a
  pre-existing binary tracked diff remains Git-rendered;
- diff Tool failure: `diff=None`, terminal failure with `DIFF_UNAVAILABLE`; success is
  forbidden;
- output truncation preventing exactness: same `DIFF_UNAVAILABLE` failure.

No change to the Day 3 `git_diff` argument contract is authorized.

## 16. RuntimeEvent additions

All events inherit existing correlation/timestamp/status fields and serialize through
`RuntimeEvent.to_dict()`.

```python
@dataclass(frozen=True, slots=True, kw_only=True)
class RepositoryExplored(RuntimeEvent):
    instruction_paths: tuple[str, ...]
    manifest_paths: tuple[str, ...]
    relevant_paths: tuple[str, ...]
    exploration_tool_calls: int
    truncated: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class ContextBuilt(RuntimeEvent):
    selected_paths: tuple[str, ...]
    retained_characters: int
    truncated: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class PlanCreated(RuntimeEvent):
    plan_id: str
    plan_version: int
    plan_kind: PlanKind
    step_summaries: tuple[str, ...]
    replan_reason: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class ReplanOccurred(RuntimeEvent):
    plan_id: str
    previous_version: int
    new_version: int
    reason: str
    replan_count: int


@dataclass(frozen=True, slots=True, kw_only=True)
class ValidationStarted(RuntimeEvent):
    check_ids: tuple[str, ...]
    check_kinds: tuple[ValidationCheckKind, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class ValidationFinished(RuntimeEvent):
    validation_status: ValidationStatus
    confidence: ValidationConfidence
    executed_check_count: int
    repair_count: int


@dataclass(frozen=True, slots=True, kw_only=True)
class RepairStarted(RuntimeEvent):
    plan_id: str
    plan_version: int
    repair_count: int
    max_repair_attempts: int
    failure_summary: str
```

All seven events use `RuntimeStatus.STARTED`. Counts are non-negative, path lists are safe,
and events contain no file contents, stdout/stderr, patch bodies, or reasoning.

### 16.1 `ApprovalRequested` extension

Architecture Review approves this additive shape:

```python
class ApprovalSubject(StrEnum):
    TOOL = "TOOL"
    PLAN = "PLAN"


class ApprovalRequested(RuntimeEvent):
    approval_id: str
    invocation_id: str | None
    operation: str
    risk_level: RiskLevel
    resource_or_command_summary: str
    subject: ApprovalSubject = ApprovalSubject.TOOL
    plan_id: str | None = None
    plan_version: int | None = None
```

TOOL preserves the Day 3 invariant: non-null invocation and null Plan fields. PLAN requires
null invocation, non-null Plan ID/version, and `operation="approve_plan"`.

### 16.2 `FinalResult` extension

Architecture Review approves this source-compatible extension:

```python
class FinalResult(RuntimeEvent):
    content: str
    terminal_status: TerminalStatus = TerminalStatus.SUCCEEDED
    changed_files: tuple[ChangedFile, ...] = ()
    diff: str | None = ""
    validation_result: ValidationResult | None = None
    includes_preexisting_changes: bool = False
```

Existing Day 1 construction defaults to success. `terminal_status=SUCCEEDED` maps event
status to COMPLETED; every other terminal value maps it to FAILED. A changed-code success
requires non-null PASS validation and non-null exact diff.

A normally executed but unsuccessful coding task emits FinalResult with a non-success
TerminalStatus; it is not automatically an ErrorOccurred. ErrorOccurred remains for a
handled runtime/service/model/persistence failure that prevents truthful normal task
finalization. One terminal path emits exactly one of FinalResult or ErrorOccurred.

## 17. RuntimeConfig additions

```python
class ApprovalMode(StrEnum):
    APPROVAL = "approval"
    AUTO = "auto"


class RuntimeConfig(BaseModel):
    approval_mode: ApprovalMode = ApprovalMode.APPROVAL
    max_steps: int = 30
    max_repair_attempts: int = 3
    max_replans: int = 2
```

Validation:

- `max_steps` is an integer greater than zero;
- `max_repair_attempts` and `max_replans` are integers greater than or equal to zero;
- booleans are not accepted as integers;
- invalid values raise the existing safe `ConfigurationError`.

Sources:

```text
environment:
NEXUS_APPROVAL_MODE
NEXUS_MAX_STEPS
NEXUS_MAX_REPAIR_ATTEMPTS
NEXUS_MAX_REPLANS

TOML:
[runtime]
approval_mode = "approval"
max_steps = 30
max_repair_attempts = 3
max_replans = 2
```

Existing per-field precedence is unchanged. Day 4 adds no CLI flags, so these fields use
repository TOML > user TOML > environment > defaults unless a later approved CLI override
is added. No Day 5 configuration is introduced.

## 18. Error semantics

Add only error subtypes already named by the Frozen Baseline:

```python
class ContextError(NexusError):
    default_code = "CONTEXT_ERROR"


class ValidationError(NexusError):
    default_code = "VALIDATION_ERROR"
```

Stable Day 4 codes:

| Code | Boundary | Retryable |
|---|---|---:|
| `REPOSITORY_EXPLORATION_FAILED` | Explorer could not establish evidence | true |
| `REPOSITORY_INSTRUCTION_CONFLICT` | applicable instructions conflict unsafely | false |
| `CONTEXT_BUILD_FAILED` | selected context could not be built | true |
| `INVALID_PLAN_OUTPUT` | model output cannot form a valid Plan | true |
| `REPAIR_SCOPE_EXPANSION` | repair attempted unapproved scope | false |
| `INVALID_AGENT_DECISION` | model decision violates exact schema | true |
| `PLAN_APPROVAL_REQUIRED` | interactive Plan has no terminal decision | false |
| `PLAN_APPROVAL_DENIED` | user denied Plan | false |
| `PLAN_SCOPE_DENIED` | Agent ToolAction is outside approved Plan scope | false |
| `INVALID_PATCH` | patch syntax/headers are invalid | false |
| `PATCH_CONFLICT` | patch context does not match current file | false |
| `PATCH_NO_CHANGES` | patch produces no content change | false |
| `FILE_NOT_FOUND` | apply-patch target is missing | false |
| `FILE_ALREADY_EXISTS` | write-file target exists | false |
| `PARENT_DIRECTORY_MISSING` | write-file parent is absent | false |
| `VALIDATION_PLAN_INVALID` | selected check violates validation contract | false |
| `DIFF_UNAVAILABLE` | exact final diff cannot be established | true |

Handled Tool failures remain `ToolResult` values. Model failures remain `ModelError`.
Approval persistence keeps Day 3 codes. Raw exceptions never enter state, events, or final
output.

## 19. Required tests and contract tests

Day 4 implementation requires at minimum:

1. exact Plan/PlanStep/AuthorizationScope projection and no-extra-authority invariants,
   canonical scope digest, immutable approved evidence, version/Replan identity,
   RepairGuidance-without-history, and different-patch/same-approved-action tests;
2. Planner `create_plan` INITIAL/Replan and `create_repair_guidance` signature, canonical
   request correlation, returned Plan correlation, Replan identity preservation, and
   invalid structured-output tests;
3. selective exploration bounds and root/nested `AGENTS.md` scope/precedence tests;
4. Context Builder file/count/character bounds and no Day 5 provider usage;
5. structured Agent decision schema and exactly-one-Tool Action tests;
6. exact counter semantics across success, policy/approval/scope denial, Tool validation,
   native/Sandbox failure, Replan, and Repair;
7. every frozen graph route and terminal predicate, including exact
   `ValidationResult.repairable` aggregation/invariants, never-repairable evidence, and the
   frozen `repair_available?` predicate;
8. durable Plan approval interrupt, same-command initial `nexus chat` continuation,
   exact human-readable step/WRITE/validation scope rendering before input, digest-not-a-
   substitute behavior, process reconstruction, explicit session resume, denial, and
   material Replan reapproval;
9. approved Plan before every mutation, no second approval for in-scope WRITE,
   out-of-scope path/Tool/validation-command denial through observe/Replan, scoped auto
   Plan approval, and direct auto WRITE denial;
10. Day 3 no-evidence WRITE compatibility (`PLAN_REQUIRED`);
11. `apply_patch` success, conflict, invalid/multi-file/no-op, containment, and atomicity;
12. `write_file` success, existing collision, missing parent, containment, and atomicity;
13. validation PASS, FAIL, UNKNOWN, unavailable check, non-zero command, low confidence,
    the exact repairable-kind allowlist, required-failure aggregation, every frozen
    non-repairable case, REPOSITORY_COMMAND exclusion, and absence of semantic heuristics;
14. FAIL -> Repair -> PASS and max-repair exhaustion;
15. max steps and max Replans truthful terminal status;
16. Agent Tool result ordering always `execute_tool -> observe`, deterministic Observation
    construction without an Observer/LLM call, mandatory `PLAN_SCOPE_DENIED` Replan reason,
    ordinary in-scope null reason, and rejection of additional Replan heuristics;
17. exact tracked diff, untracked new-file diff, no change, pre-existing dirty state,
    truncation, and diff failure;
18. exact RuntimeEvent payload/ordering and private-reasoning exclusion;
19. RuntimeConfig defaults, TOML/environment precedence, and invalid limits;
20. one realistic fixture E2E task proving task -> selective explore -> Plan -> durable
    approval -> bounded edit -> validation -> exact diff -> truthful FinalResult;
21. all prior Day 1–Day 3 tests remain passing.

Quality gates remain `ruff check .`, `mypy src tests scripts`, and `pytest`, plus the
required PostgreSQL integration path when available. Local checks do not replace CI,
architecture review, or Product Owner knowledge review.

## 20. Compatibility impact

| Existing contract | Reviewed Day 4 impact |
|---|---|
| `NexusRuntime.run(task, session_id=None)` | unchanged |
| `NexusRuntime.resume(session_id)` | approved optional typed keyword input; old call remains valid |
| Day 1 `ModelGateway` | unchanged; Agent adapter wraps it |
| Day 1 `AgentState` | additive Day 4 fields with compatible defaults |
| Day 1 `FinalResult` | approved additive payload and conditional failed status |
| Day 2 Session/Run schema | no migration; terminal summary uses existing columns |
| Day 2 Checkpoint ownership | unchanged; complete AgentState remains LangGraph-owned snapshot |
| Day 2 session resume command | same syntax; approved approval-aware repeat-resume behavior |
| Day 3 `ToolInvocation` | unchanged |
| Day 3 Tool/ToolResult/Registry | unchanged; two new registered Tools |
| Day 3 `ToolRuntime.execute` | approved optional authorization keyword |
| Day 3 approval schema | unchanged; Plan approval reuses existing row |
| Day 3 ApprovalRequest actor invariant | approved auto_policy exception only for approve_plan |
| Day 3 `ApprovalRequested` | approved additive subject/Plan fields |
| Day 3 command/security policy | add editing Tools as WRITE; no validation-command expansion; DANGEROUS unchanged |
| Day 3 Git tools | unchanged |
| RuntimeConfig | four additive fields and config sources |
| Dependencies | none |
| Day 5+ systems | none |

## 21. Architecture decision record

Architecture Review resolved all seven v0.1 decisions. They remain binding within this
approved v0.2.3 Addendum.

| Decision | Status | Approved resolution |
|---|---|---|
| AD-4-01 | RESOLVED / APPROVED BY ARCHITECT REVIEW | Approved Plan grants exact scoped ordinary WRITE authority; auto_policy may approve only approve_plan; direct auto WRITE remains denied |
| AD-4-02 | RESOLVED / APPROVED BY ARCHITECT REVIEW | ToolRuntime.execute accepts optional ApprovedPlanEvidence; ToolInvocation is unchanged |
| AD-4-03 | RESOLVED / APPROVED BY ARCHITECT REVIEW | Existing ApprovalRequested gains additive Plan fields; Runtime resume accepts typed optional input; initial chat continues through the same resume contract |
| AD-4-04 | RESOLVED / APPROVED BY ARCHITECT REVIEW | Only max-steps and max-replans exhaustion add routes to existing finalize_failed |
| AD-4-05 | RESOLVED / APPROVED BY ARCHITECT REVIEW | execute_tool -> observe is mandatory for Agent actions; lifecycle services call ToolRuntime internally with equivalent counters/events/evidence |
| AD-4-06 | RESOLVED / APPROVED BY ARCHITECT REVIEW | FinalResult additively carries TerminalStatus and represents normal unsuccessful task outcomes |
| AD-4-07 | RESOLVED / APPROVED BY ARCHITECT REVIEW | Validation is command-policy-neutral; Day 4 does not expand the Day 3 permitted command matrix |

## 22. Explicitly deferred scope

This approved Addendum does not define or authorize pgvector, semantic/hybrid retrieval, indexing,
Day 5 chunking/ranking, MCP, Skills, Day 8 tracing, Day 9 Evaluation, multi-agent behavior,
Git mutation, delete/rename Tools, Docker/remote sandbox, a new database table, a new
dependency, or a new public CLI command.

## 23. Approval record

Current review state:

```text
Architecture decisions AD-4-01 through AD-4-07:
RESOLVED / APPROVED

Final document consistency approval:
APPROVED

Implementation authorization:
YES — DAY 4 ONLY
```

Implementation is authorized for Day 4 only. Day 5 and later scope remains deferred.
