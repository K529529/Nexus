# Nexus Day 3 Contract Addendum v1.0

**Status:** APPROVED

**Applies To:** Day 3 — Native Tool Runtime & Security

**Authority:** Product Owner / Architect approved implementation contract

**Baseline:** Nexus V1 Specification v1.1.1, approved Day 1 and Day 2 Contract Addenda,
and the current Day 2 implementation on `feature/day03-tools-security`

## 0. Authority and scope

This Addendum freezes the smallest public contract needed to implement Day 3 and
authorizes Day 3 implementation within the frozen scope.

The authority order is:

1. `AGENTS.md`;
2. `docs/Nexus_V1_Product_Requirements_and_10-Day_Engineering_Specification_v1.1.1_中文.md`;
3. approved Day 1 and Day 2 Contract Addenda for their existing contracts;
4. this approved Addendum for Day 3 details left unspecified by the higher-level documents.

This Addendum does not authorize Day 4 planning/editing, MCP, parallel tool scheduling,
Docker/remote sandboxing, or any other future milestone.

### 0.1 Architecture Review amendments applied

This approved revision incorporates the blocking reviews dated 2026-08-30:

1. `SandboxRequest.operation` preserves dedicated native Tool identity through Sandbox;
2. Sandbox hard-denies DANGEROUS but can execute a WRITE request already authorized by
   Tool Runtime in a later approved milestone;
3. exact native arguments and paths are validated inside each `Tool.execute` before any
   read, subprocess, or side effect, without adding `Tool.validate()`;
4. Tool-local outcomes remain on active `RuntimeStatus.STARTED` and do not terminalize the
   Run;
5. the proposed public batch method and batch failure semantics are removed; sequential
   behavior uses awaited single invocations.
6. Sandbox verifies preserved operation identity against canonical argv before execution;
7. resource security validation may escalate final risk but may never downgrade it.

## 1. Compatibility baseline

Day 3 extends the current code style rather than replacing it:

- public value models use frozen, slotted dataclasses;
- persisted identifiers are canonical UUID strings in Python and PostgreSQL UUID values;
- persisted timestamps are timezone-aware UTC values and PostgreSQL `TIMESTAMPTZ`;
- finite public states use `StrEnum` with uppercase serialized values;
- repository and unit-of-work ports are asynchronous `Protocol` contracts;
- infrastructure repositories may flush but do not independently commit;
- failures crossing stable boundaries use `NexusError` subtypes or structured result
  values, never raw infrastructure exceptions;
- `RuntimeEvent.to_dict()` remains the stable event serialization boundary.

No existing Day 1 or Day 2 field, signature, table, CLI command, or event payload is
removed or reinterpreted by this Addendum.

## 2. Shared Day 3 enums and error semantics

### 2.1 `RiskLevel`

```python
from enum import StrEnum


class RiskLevel(StrEnum):
    SAFE = "SAFE"
    WRITE = "WRITE"
    DANGEROUS = "DANGEROUS"
```

Semantics:

- `SAFE`: explicitly recognized read-only repository or inspection operation;
- `WRITE`: controlled test/build or future workspace mutation that requires an approved
  Plan before execution;
- `DANGEROUS`: unknown, ambiguous, escaping, destructive, system-level, or Git-mutating
  operation; denied by the V1 Agent.

### 2.2 `ApprovalDecision`

```python
class ApprovalDecision(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    DENIED = "DENIED"
```

`PENDING` is non-terminal. `APPROVED` and `DENIED` are terminal and immutable.

### 2.3 `PolicyDecision`

```python
class PolicyDecision(StrEnum):
    ALLOWED = "ALLOWED"
    DENIED = "DENIED"
```

`PolicyDecision` describes whether execution may cross the sandbox boundary. It is not an
approval lifecycle state. In particular, a WRITE request may have
`approval_decision=APPROVED` but `policy_decision=DENIED` because Day 3 has no approved
Plan contract.

### 2.4 Nexus error subtypes

Add only the two subtypes already named by the frozen V1 specification:

```python
class ToolExecutionError(NexusError):
    default_code = "TOOL_EXECUTION_ERROR"


class PermissionDeniedError(NexusError):
    default_code = "PERMISSION_DENIED"
```

These errors are for application/service boundaries. `ToolResult` and `SandboxResult`
must normalize them to `ToolError` before returning to a tool consumer.

Stable Day 3 error codes are:

| Code | Meaning | Retryable |
|---|---|---:|
| `TOOL_NOT_FOUND` | `ToolRegistry` has no such stable tool name | false |
| `INVALID_TOOL_ARGUMENTS` | arguments do not match the named tool contract | false |
| `TOOL_EXECUTION_ERROR` | safe normalized tool failure | false by default |
| `PERMISSION_DENIED` | policy denied an operation | false |
| `WORKSPACE_PATH_DENIED` | path is absolute, escaping, or resolves outside workspace | false |
| `COMMAND_DENIED` | command is not in an executable Day 3 class | false |
| `PLAN_REQUIRED` | WRITE approval exists but no approved Plan exists | false in Day 3 |
| `SANDBOX_TIMEOUT` | allowed process exceeded its timeout | true |
| `COMMAND_EXIT_NONZERO` | allowed process exited with a non-zero code | false |
| `SANDBOX_EXECUTION_ERROR` | process could not be safely started/observed | false |
| `UNSUPPORTED_FILE` | file is binary, non-UTF-8, or above the hard file limit | false |
| `APPROVAL_NOT_FOUND` | requested approval record does not exist | false |
| `INVALID_APPROVAL_TRANSITION` | attempted to change a terminal decision or invalid state | false |
| `APPROVAL_CORRELATION_DENIED` | Run does not belong to the supplied Session | false |
| `APPROVAL_PERSISTENCE_ERROR` | approval transaction failed | true |

Compatibility impact: additive only. Existing `NexusError`, `ConfigurationError`,
`ModelError`, and `SessionError` behavior is unchanged.

## 3. Unified Tool contract

### 3.1 Shared JSON values

Day 3 reuses the existing semantic alias:

```python
JsonObject = dict[str, object]
```

Tool arguments and outputs must contain only values that can be safely serialized by the
standard event/result renderer. Provider, SQLAlchemy, subprocess, `Path`, and exception
objects must not cross this boundary.

### 3.2 `ToolInvocation`

```python
@dataclass(frozen=True, slots=True)
class ToolInvocation:
    invocation_id: str
    tool_name: str
    arguments: JsonObject
    run_id: str
    session_id: str
```

All five fields are required and non-null.

- `invocation_id`, `run_id`, and `session_id` are canonical UUID strings.
- `tool_name` is a non-empty stable name from section 7.
- `arguments` is a copied `dict[str, object]`; each Tool performs exact argument validation.
- Run/session correlation is mandatory for Day 3 observability and approval persistence.

Compatibility impact: new contract only. `AgentState.session_id` and
`RuntimeEvent.session_id` remain nullable for existing Day 1 compatibility; Day 3 tool
invocations require an established Day 2 Run and Session.

### 3.3 `ToolError`

```python
@dataclass(frozen=True, slots=True)
class ToolError:
    code: str
    message: str
    retryable: bool
```

All fields are required and non-null. `message` must be safe for user display. It must not
contain stack traces, secrets, unrestricted environment values, or raw subprocess/
filesystem exception representations.

### 3.4 `ToolResult`

```python
@dataclass(frozen=True, slots=True)
class ToolResult:
    invocation_id: str
    tool_name: str
    success: bool
    output: JsonObject | None
    error: ToolError | None
    risk_level: RiskLevel
    policy_decision: PolicyDecision
    approval_decision: ApprovalDecision | None
    duration_ms: int
```

Invariants:

- identifiers and tool name match the originating invocation;
- `duration_ms >= 0`;
- `success=True` requires `error is None` and `policy_decision=ALLOWED`;
- `success=False` requires `error is not None`;
- `risk_level` is the final observed risk after native argument/resource security
  validation;
- final risk may equal or escalate above the proposed classification but may never
  downgrade it (`SAFE → WRITE/DANGEROUS`, `WRITE → DANGEROUS` are permitted;
  `DANGEROUS → WRITE/SAFE` and `WRITE → SAFE` are forbidden);
- `output` may be present on failure when it contains safe partial evidence such as exit
  code or bounded stderr;
- `approval_decision=None` for SAFE operations, registry/argument failures that never
  created an approval, and failures before approval creation;
- raw exceptions never appear in `output` or `error`.

Compatibility impact: new contract only. It does not replace `RuntimeEvent`.

### 3.5 `Tool`

```python
from typing import Protocol


class Tool(Protocol):
    @property
    def name(self) -> str: ...

    async def execute(self, invocation: ToolInvocation) -> ToolResult: ...
```

`Tool.execute` is async and returns exactly one structured `ToolResult`. A Tool must
normalize handled filesystem/process/adapter failures. `asyncio.CancelledError` may
propagate so application cancellation is not swallowed.

Tool implementations do not decide whether an operation may bypass Tool Runtime policy.
The Runtime classifies and gates before calling `Tool.execute`.

Compatibility impact: new port only; it does not alter `GraphRuntime` or `ModelGateway`.

### 3.6 `ToolRegistry`

```python
from collections.abc import Sequence


class ToolRegistry:
    def __init__(self, tools: Sequence[Tool]) -> None: ...

    def resolve(self, name: str) -> Tool: ...
```

Semantics:

- construction takes the complete immutable Day 3 registry assembled by the Composition
  Root;
- names must be non-empty and unique;
- duplicate names fail construction;
- no public runtime `register`/`unregister` mutation method is added;
- `resolve` performs exact, case-sensitive matching;
- an unknown name raises `ToolExecutionError(code="TOOL_NOT_FOUND")`;
- Tool Runtime catches that safe Nexus error and returns a failed `ToolResult`.

The frozen public lookup entry is therefore `ToolRegistry.resolve(name: str) -> Tool`;
callers do not inspect the registry's internal mapping.

Compatibility impact: additive. The CLI still must not construct `ToolRegistry`.

### 3.7 Single-invocation Tool Runtime entry

```python
class ToolRuntime:
    async def execute(self, invocation: ToolInvocation) -> ToolResult: ...
```

`execute` owns this order:

```text
common ToolInvocation validation
→ resolve Tool
→ classify
→ emit ToolStarted
→ approval or hard deny when required
→ Tool.execute when allowed
→ exact native-tool argument validation inside Tool.execute
→ workspace/path containment inside path-sensitive Tool.execute
→ escalate final risk when resource security validation discovers a stronger violation
→ only then any filesystem/process side effect
→ normalize ToolResult
→ emit ToolFinished
```

Common validation covers the fixed `ToolInvocation` fields and UUID/name shape only.
Tool Runtime does not claim to know every native Tool's exact argument or path schema.
Each native Tool owns exact validation for its argument map and must complete containment
before reading file content, starting a subprocess, or producing any external side effect.

V1 sequential semantics come from awaiting one `execute` call before proposing or starting
the next invocation. No public batch Tool API exists. `asyncio.gather`, task groups,
parallel schedulers, and speculative execution are forbidden for Agent Tool execution.

The mechanism used by the Composition Root to supply an async event-emitter callback is a
constructor detail, not a new Day 8 tracer/event-bus public contract.

Compatibility impact: one additive single-invocation application boundary.
`NexusRuntime.run` and the Day 1/2 graph topology are unchanged; Day 3 does not insert tool
execution into the Agent graph.

## 4. Approval domain and policy contract

### 4.1 `ApprovalRequest`

`ApprovalRequest` is both the Nexus-owned approval lifecycle entity and the value persisted
to the Day 3 `approvals` table. No separate speculative `ApprovalRecord` hierarchy is
introduced.

```python
@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    approval_id: str
    run_id: str
    session_id: str
    operation: str
    risk_level: RiskLevel
    resource_or_command_summary: str
    decision: ApprovalDecision
    actor: str
    reason: str | None
    created_at: datetime
    decided_at: datetime | None
```

Nullability and validation:

- only `reason` and `decided_at` are nullable;
- three identifiers are canonical UUID strings;
- timestamps are timezone-aware UTC;
- `operation`, `resource_or_command_summary`, and `actor` are non-empty;
- summaries are bounded, sanitized audit descriptions, not full file contents, complete
  environments, or unbounded command output;
- `PENDING` requires `decided_at is None` and `actor == "runtime"`;
- `APPROVED`/`DENIED` require `decided_at is not None`;
- terminal decisions cannot transition again.

Frozen Day 3 actor values are plain strings rather than a database enum:

| Actor | Meaning |
|---|---|
| `runtime` | pending request created by Tool Runtime |
| `user` | interactive callback supplied the decision |
| `auto_policy` | AutoApprovalPolicy denied WRITE |
| `security_policy` | centralized hard deny |

No user identity/authentication model is introduced in Day 3.

Compatibility impact: new domain entity only. Existing Session/Run entities are unchanged.

### 4.2 `ApprovalPolicy`

```python
class ApprovalPolicy(Protocol):
    async def request(self, request: ApprovalRequest) -> ApprovalRequest: ...
```

Precondition: `request.decision is PENDING`.

Postcondition: the returned request has the same immutable identity, correlation,
operation, risk, summary, and creation timestamp, with decision `APPROVED` or `DENIED`, a
terminal actor/reason, and non-null UTC `decided_at`.

SAFE operations never call `ApprovalPolicy.request`.

#### `InteractiveApprovalPolicy`

The policy receives an injected async decision callback with this semantic signature:

```python
from collections.abc import Awaitable, Callable

InteractiveDecisionCallback = Callable[
    [ApprovalRequest],
    Awaitable[tuple[ApprovalDecision, str | None]],
]


class InteractiveApprovalPolicy:
    def __init__(self, decision_callback: InteractiveDecisionCallback) -> None: ...

    async def request(self, request: ApprovalRequest) -> ApprovalRequest: ...
```

The tuple is `(decision, reason)`. The callback may return only `APPROVED` or `DENIED`.
The policy sets `actor="user"` and `decided_at` itself. Runtime and policy never call
`input()`.

For `DANGEROUS`, the callback is not invoked; centralized security denial wins.

#### `AutoApprovalPolicy`

```python
class AutoApprovalPolicy:
    async def request(self, request: ApprovalRequest) -> ApprovalRequest: ...
```

- WRITE returns `DENIED`, `actor="auto_policy"`;
- DANGEROUS returns `DENIED`, `actor="security_policy"` if called defensively;
- SAFE is not passed to the policy.

Compatibility impact: additive port and implementations. No CLI callback is wired in Day
3 because no new public CLI is authorized.

### 4.3 Approval lifecycle by risk

| Risk | Approval row | Policy request | Execution in Day 3 |
|---|---|---|---|
| SAFE | none | none | allowed |
| WRITE + interactive | persist PENDING, request, persist terminal decision | yes | denied even if approved; `PLAN_REQUIRED` |
| WRITE + auto | persist PENDING, obtain DENIED, persist DENIED | yes | denied; `COMMAND_DENIED` |
| DANGEROUS | persist terminal DENIED security record when correlation is valid | no interactive request | denied; `PERMISSION_DENIED` or a more specific stable denial code |

If an interactive callback fails, the already committed PENDING record remains PENDING,
and Tool Runtime returns a safe failed result with `APPROVAL_PERSISTENCE_ERROR` only for a
persistence failure or `TOOL_EXECUTION_ERROR` for an unavailable decision source. It must
not silently convert a missing user decision into approval.

For WRITE that receives interactive `APPROVED`:

- the APPROVED record is persisted;
- no native Tool or Sandbox is called;
- `ToolResult.success=False`;
- `risk_level=WRITE`;
- `approval_decision=APPROVED`;
- `policy_decision=DENIED`;
- `error.code="PLAN_REQUIRED"`;
- `duration_ms` covers the governed attempt including approval time;
- Tool Runtime emits `ToolFinished(success=False, status=STARTED)`; the Tool-local failure
  does not terminalize the Run.

This records user intent without pretending that Day 3 has a Plan contract.

## 5. Approval persistence contract

### 5.1 PostgreSQL schema

The Day 3 Alembic revision adds exactly:

```text
approvals
  approval_id UUID PRIMARY KEY
  run_id UUID NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT
  session_id UUID NOT NULL REFERENCES sessions(session_id) ON DELETE RESTRICT
  operation TEXT NOT NULL
  risk_level VARCHAR(16) NOT NULL
  resource_or_command_summary TEXT NOT NULL
  decision VARCHAR(16) NOT NULL
  actor TEXT NOT NULL
  reason TEXT NULL
  created_at TIMESTAMPTZ NOT NULL
  decided_at TIMESTAMPTZ NULL
```

Required check constraints:

```text
risk_level IN ('SAFE', 'WRITE', 'DANGEROUS')
decision IN ('PENDING', 'APPROVED', 'DENIED')
(decision = 'PENDING' AND decided_at IS NULL)
OR
(decision IN ('APPROVED', 'DENIED') AND decided_at IS NOT NULL)
```

Required index:

```text
INDEX approvals(run_id, created_at, approval_id)
```

No existing Day 2 table or constraint is changed. Because no composite uniqueness contract
on `runs(run_id, session_id)` is authorized, the database uses independent FKs and the
application boundary additionally verifies `Run.session_id == ApprovalRequest.session_id`
before insert/update.

### 5.2 `ApprovalRepository`

```python
class ApprovalRepository(Protocol):
    async def add(self, approval: ApprovalRequest) -> None: ...

    async def get(self, approval_id: str) -> ApprovalRequest | None: ...

    async def list_by_run(self, run_id: str) -> list[ApprovalRequest]: ...

    async def update(self, approval: ApprovalRequest) -> None: ...
```

`list_by_run` orders by `created_at ASC, approval_id ASC`. Repositories flush but never
commit. SQLAlchemy types/rows do not cross this port.

`update` may change only `decision`, `actor`, `reason`, and `decided_at`; identity,
correlation, operation, risk, summary, and `created_at` are immutable.

### 5.3 `ApprovalUnitOfWork`

```python
from types import TracebackType
from typing import Protocol, Self


class ApprovalUnitOfWork(Protocol):
    approvals: ApprovalRepository
    runs: RunRepository
    sessions: SessionRepository

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None: ...


class ApprovalUnitOfWorkFactory(Protocol):
    def __call__(self) -> ApprovalUnitOfWork: ...
```

The UoW exposes existing Run/Session repositories only so the application can validate
correlation in the same transaction as approval persistence. It does not modify or inherit
from the frozen `SessionUnitOfWork`.

Transaction semantics:

- pending creation is committed before an interactive wait;
- terminal decision update occurs in a second transaction;
- each transaction validates that Run and Session exist and that the Run belongs to the
  Session;
- a failed transaction rolls back completely;
- no repository commits independently.

Compatibility impact: new independent port/factory. Existing `SessionUnitOfWork` remains
byte-for-byte semantically unchanged.

## 6. Workspace path contract

Recommended Day 3 rule: all Tool path arguments and `SandboxRequest.cwd` are
repository-relative strings. Absolute paths are rejected, even if they happen to point
inside the workspace.

For an existing target:

1. reject absolute input;
2. resolve from the canonical workspace with `Path.resolve(strict=True)`;
3. compare using platform-normalized paths (`os.path.normcase` on Windows);
4. require the resolved path to equal the workspace or be its descendant.

Directory traversal, normalization escape, and symlink/junction escape return
`WORKSPACE_PATH_DENIED`. Recursive traversal never follows directory symlinks. Missing
targets return a structured tool error rather than a raw `FileNotFoundError`.

Exact path validation belongs to each path-sensitive native Tool inside its
`Tool.execute` boundary; Tool Runtime does not depend on a speculative public
`Tool.validate()` API. A path-denied Tool may therefore have entered `Tool.execute`, but it
must not read file content, start a subprocess, or produce any external side effect before
canonicalization and containment succeed.

CommandPolicy supplies the proposed risk before this resource check. Resource validation
may escalate but never downgrade it. For example:

```text
ToolStarted(read_file, risk_level=SAFE)
→ containment detects ../ or symlink/junction escape
→ ToolResult/ToolFinished(
     risk_level=DANGEROUS,
     policy_decision=DENIED,
     success=False,
     error.code=WORKSPACE_PATH_DENIED,
   )
```

Tool Runtime persists the configured terminal DENIED security record for a correlation-
valid operation that escalates to DANGEROUS, without emitting an interactive
`ApprovalRequested` event.

Only `.git` is always excluded from recursive Day 3 file enumeration. Full `.gitignore`,
generated-file, cache, and retrieval filtering belongs to the frozen Day 5 contract and is
not implemented early.

Compatibility impact: reuses the Day 2 canonical-path conventions; does not change
Repository identity.

## 7. Stable native Tool surface

Tool argument maps reject unknown keys and wrong types with `INVALID_TOOL_ARGUMENTS`.
Defaults below are part of the proposed public contract.

### 7.1 `list_files`

Arguments:

```python
{
    "path": str,       # optional, default "."
    "recursive": bool # optional, default True
}
```

Output:

```python
{
    "paths": list[str],  # repository-relative POSIX-style paths, sorted
    "truncated": bool,
}
```

Hard result limit: 200 paths.

### 7.2 `search_files`

Filename/path search using case-sensitive `fnmatch` glob semantics.

Arguments:

```python
{
    "pattern": str, # required, non-empty
    "path": str,    # optional, default "."
}
```

Output and ordering match `list_files`. Hard result limit: 200 paths.

### 7.3 `read_file`

Arguments:

```python
{
    "path": str,      # required
    "start_line": int, # optional, default 1, minimum 1
    "max_lines": int,  # optional, default 400, range 1..400
}
```

Output:

```python
{
    "path": str,
    "start_line": int,
    "end_line": int,
    "content": str,
    "truncated": bool,
}
```

The target must be a regular UTF-8 text file no larger than 1,048,576 bytes. NUL-bearing,
non-UTF-8, special-device, directory, and oversized inputs return `UNSUPPORTED_FILE`.

### 7.4 `lexical_search`

Day 3 freezes literal substring search, not a regex language.

Arguments:

```python
{
    "pattern": str,          # required, non-empty literal
    "path": str,             # optional, default "."
    "case_sensitive": bool,  # optional, default True
}
```

Output:

```python
{
    "matches": list[
        {
            "path": str,
            "line": int,
            "column": int,
            "text": str,
        }
    ],
    "truncated": bool,
}
```

Ordering is path, line, then column. Hard result limit: 200 matches. Individual line text
and total serialized text remain subject to the 1,048,576-byte output cap.

This is a Day 3 inspection tool, not the Day 5 ranked retrieval algorithm.

### 7.5 `shell`

Arguments:

```python
{
    "argv": list[str],        # required, non-empty; every item non-empty
    "cwd": str,               # optional, default "."
    "timeout_seconds": float, # optional, default 30.0, range (0, 300.0]
}
```

Output is the safe dictionary serialization of `SandboxResult`. No environment override,
shell string, stdin payload, pipe, redirection, or command concatenation argument exists.

### 7.6 `git_status`

Arguments: exactly `{}`.

The adapter executes a pinned canonical Git executable with a fixed read-only status
argument set. The canonical argv shape is:

```text
[GIT, "-c", "core.fsmonitor=false", "--no-pager",
 "status", "--short", "--branch"]
```

`GIT` is the canonical absolute executable resolved and pinned by the Composition Root.
Output:

```python
{
    "exit_code": int,
    "stdout": str,
    "stderr": str,
    "truncated": bool,
}
```

### 7.7 `git_diff`

Arguments:

```python
{
    "staged": bool, # optional, default False
}
```

The canonical argv shapes are:

```text
staged=False:
[GIT, "-c", "core.fsmonitor=false", "--no-pager",
 "diff", "--no-ext-diff", "--no-textconv"]

staged=True:
[GIT, "-c", "core.fsmonitor=false", "--no-pager",
 "diff", "--no-ext-diff", "--no-textconv", "--cached"]
```

No other argument shape is authorized. Output matches `git_status`.

### 7.8 `git_log`

Arguments:

```python
{
    "max_entries": int, # optional, default 20, range 1..100
}
```

The canonical bounded argv shape is:

```text
[GIT, "-c", "core.fsmonitor=false", "--no-pager",
 "log", "--max-count=N", "--oneline", "--no-decorate"]
```

`N` is the validated decimal `max_entries` value in `1..100`. No other argument shape is
authorized. Output matches `git_status`.

### 7.9 Shared native output bounds

- file input hard limit: 1,048,576 bytes;
- list/search hard limit: 200 items;
- read hard limit: 400 lines and 1,048,576 output bytes;
- lexical hard limit: 200 matches and 1,048,576 output bytes;
- stdout and stderr: independently capped at 1,048,576 bytes;
- Git log: default 20 and maximum 100 entries;
- all truncation is explicit through `truncated=True` or
  `SandboxResult.output_truncated=True`.

Truncation is not a tool failure unless the operation cannot return a syntactically valid
structured result within the cap.

Compatibility impact: all names and arguments are new. No CLI command is added.

## 8. Command policy contract

```python
class CommandPolicy(Protocol):
    def classify(
        self,
        *,
        operation: str,
        arguments: JsonObject,
    ) -> RiskLevel: ...
```

Classification is synchronous, deterministic, side-effect free, and centralized.
Tool Runtime calls it with `operation=invocation.tool_name` and the invocation arguments.
`LocalProcessSandbox` calls it defensively with `operation=request.operation` and an
argument map constructed from its `SandboxRequest`; it does not accept a caller-supplied
risk value.

`operation` preserves identity but is not an authorization token. Classification must
validate that operation and normalized arguments/argv are mutually consistent. For the
dedicated Git operations, only the exact canonical shapes in sections 7.6–7.8 classify
SAFE. Any mismatch—including `operation="git_status"` paired with Git push argv—classifies
DANGEROUS and is denied before subprocess execution.

The generic `operation="shell"` uses only the frozen shell SAFE allowlist and WRITE matrix.
It cannot borrow privileges from a dedicated Tool identity.

### 8.1 SAFE

The following native names are SAFE after valid workspace containment:

```text
list_files
search_files
read_file
lexical_search
git_status
git_diff
git_log
```

The Day 3 `shell` SAFE allowlist is exact after executable canonicalization:

```text
python --version
python -V
uv --version
git --version
```

On Windows, an `.exe` suffix is equivalent for executable identity. The Composition Root
resolves approved executables through a sanitized PATH that excludes empty, relative, and
workspace-contained entries, then pins their canonical paths. A same-named executable in
the fixture workspace must not become trusted.

No additional SAFE inspection command is configurable in Day 3.

`git status`, `git diff`, and `git log` are SAFE only through their dedicated native Tool
names. Sending those argv through the generic `shell` Tool is DANGEROUS. This prevents the
generic command path from bypassing the dedicated Git adapter's fixed pager, external-diff,
text-conversion, fsmonitor, and output-bound controls.

### 8.2 WRITE classification without execution

The following exact command families classify as WRITE:

```text
pytest ...
ruff check ...
mypy ...
uv run pytest ...
uv run ruff check ...
uv run mypy ...
uv build ...
```

Suffix arguments do not upgrade them to SAFE. Day 3 may persist approval decisions for
these commands but never executes them because no approved Plan contract exists.

### 8.3 DANGEROUS and hard deny

The following classify as DANGEROUS:

- unknown tool or command;
- any unrecognized `git` subcommand or option shape;
- Git commit, push, reset, checkout mutation, clean, rebase, merge, tag mutation, branch
  mutation, or history-changing operation;
- recursive/high-risk deletion;
- PowerShell/cmd/POSIX shell/interpreter code execution;
- Python `-c`, `-m`, or script execution;
- workspace escape;
- system/service/process/account/network administration;
- any argv containing an attempted shell operator token such as `&&`, `||`, `|`, `>`,
  `>>`, `<`, or `;`;
- any ambiguous high-impact operation.

All DANGEROUS operations are denied before subprocess or Tool execution. Interactive
approval cannot override the hard deny.

Compatibility impact: new policy only. It does not change Day 1 model execution or Day 2
session commands.

## 9. Sandbox contract

### 9.1 `SandboxRequest`

```python
@dataclass(frozen=True, slots=True)
class SandboxRequest:
    operation: str
    argv: list[str]
    cwd: str
    timeout_seconds: float
```

All four fields are required and non-null at the port boundary. The calling native Tool
applies its public defaults before constructing the request.

- `operation` is the stable native Tool name responsible for the process request;
- `argv` is non-empty; every item is a non-empty string;
- `cwd` is a repository-relative path resolving to an existing workspace directory;
- `0 < timeout_seconds <= 300.0`.

Operation identity is preserved exactly:

```text
git_status → operation="git_status"
git_diff   → operation="git_diff"
git_log    → operation="git_log"
shell      → operation="shell"
```

Consequently, a dedicated `git_status` request can classify SAFE while
`shell(["git", "status"])` classifies DANGEROUS.

The caller cannot supply environment variables, shell mode, stdin, resource handles, or a
precomputed risk/policy decision.

### 9.2 `SandboxResult`

```python
@dataclass(frozen=True, slots=True)
class SandboxResult:
    argv: list[str]
    cwd: str
    risk_level: RiskLevel
    policy_decision: PolicyDecision
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool
    output_truncated: bool
    error: ToolError | None
```

Invariants:

- `cwd` is rendered repository-relative, never as an unrelated absolute path;
- `duration_ms >= 0`;
- a denied or not-started request has `exit_code=None`;
- a timeout has `timed_out=True`, `exit_code=None`, and
  `error.code="SANDBOX_TIMEOUT"`;
- a policy denial has `policy_decision=DENIED`, `exit_code=None`, and a permission error;
- a started process has `policy_decision=ALLOWED` and an integer exit code unless timeout
  or launch observation failed;
- non-zero process exit has `error.code="COMMAND_EXIT_NONZERO"` and retains bounded stdout
  and stderr evidence;
- decoding uses UTF-8 with replacement for subprocess output only; replacement does not
  expose raw bytes.

### 9.3 `SandboxExecutor`

```python
class SandboxExecutor(Protocol):
    async def execute(self, request: SandboxRequest) -> SandboxResult: ...
```

Only `LocalProcessSandbox` implements this port during Day 3. It defensively reclassifies
the request using its preserved identity:

```python
CommandPolicy.classify(
    operation=request.operation,
    arguments=<normalized sandbox arguments>,
)
```

`LocalProcessSandbox` hard-denies DANGEROUS, permits SAFE, and is capable of executing a
WRITE request once Tool Runtime has authorized it. The call across `SandboxExecutor` is
the assertion that application-level authorization has already succeeded; Sandbox does
not own or reconstruct Plan/Approval state.

Before applying that boundary, Sandbox requires `request.operation` and normalized
`request.argv` to match the same CommandPolicy rule. An operation/argv mismatch escalates
to DANGEROUS and is denied. Preserved operation identity alone never grants execution.

During Day 3, Tool Runtime never calls Sandbox for WRITE because no approved Plan contract
exists. This does not freeze `WRITE → sandbox denied` as a permanent adapter behavior.

The responsibility boundary is:

```text
CommandPolicy → classify risk
ToolRuntime    → authorize execution
Sandbox        → safely perform the authorized process request
```

All Git subprocess execution also passes through this adapter; dedicated Git Tools may not
invoke subprocess APIs directly. `asyncio.create_subprocess_exec` or an equivalent
exec-style async standard-library API may appear only inside this infrastructure adapter.
`shell=True` and shell-string APIs are forbidden.

### 9.4 Timeout and process termination

- default timeout: 30.0 seconds;
- hard request maximum: 300.0 seconds;
- timeout terminates the launched process and waits for it to be reaped;
- process-group creation/termination is used where supported;
- descendant termination is best-effort under standard-library Windows constraints;
- the implementation must not claim hard process-tree, CPU, RAM, filesystem, network, or
  container isolation.

### 9.5 Output capture

stdout and stderr are captured independently and bounded while reading, not captured
without limit and truncated only afterward. Each stream retains at most 1,048,576 bytes.
Excess bytes are discarded and `output_truncated=True`.

### 9.6 Minimum child environment

Child environment starts from an allowlist, not a copy-minus-denylist.

Inherited when present:

```text
all platforms: PATH
Windows: SystemRoot, WINDIR, PATHEXT, TEMP, TMP
POSIX: LANG, LC_ALL, TMPDIR
```

Injected fixed values:

```text
PYTHONIOENCODING=utf-8
GIT_TERMINAL_PROMPT=0
GIT_CONFIG_NOSYSTEM=1
GIT_OPTIONAL_LOCKS=0
```

`GIT_CONFIG_GLOBAL` points to the platform null device. PATH entries that are empty,
relative, or inside the workspace are removed before executable resolution/execution.

The sandbox does not inherit API keys, tokens, passwords, proxy credentials, `PYTHONPATH`,
`VIRTUAL_ENV`, shell startup configuration, or arbitrary repository/user variables.

Compatibility impact: new infrastructure port/adapter. No dependency is added, and the
existing model/checkpoint process environment is unchanged.

## 10. RuntimeEvent additions

### 10.1 `RuntimeStatus`

Add one non-terminal value:

```python
RuntimeStatus.AWAITING_APPROVAL = "AWAITING_APPROVAL"
```

Existing `STARTED`, `INTERRUPTED`, `COMPLETED`, and `FAILED` values retain their exact
meaning and serialization.

### 10.2 `ApprovalRequested`

```python
@dataclass(frozen=True, slots=True, kw_only=True)
class ApprovalRequested(RuntimeEvent):
    status: RuntimeStatus = field(
        default=RuntimeStatus.AWAITING_APPROVAL,
        init=False,
    )
    approval_id: str
    invocation_id: str
    operation: str
    risk_level: RiskLevel
    resource_or_command_summary: str
```

The inherited `run_id` and `session_id` correlate the request. Day 3 emits this event for
WRITE policy requests after PENDING persistence and before asking the selected policy. It
does not emit it for SAFE or immediate DANGEROUS hard denial.

### 10.3 `ToolStarted`

```python
@dataclass(frozen=True, slots=True, kw_only=True)
class ToolStarted(RuntimeEvent):
    status: RuntimeStatus = field(default=RuntimeStatus.STARTED, init=False)
    invocation_id: str
    tool_name: str
    risk_level: RiskLevel
```

`ToolStarted` means a governed invocation entered Tool Runtime. It does not mean a
subprocess or side effect has started. This permits every attempted invocation, including
policy denial, to have a safe start/finish event pair.

`ToolStarted.risk_level` is the proposed operation risk produced before exact native
resource validation.

### 10.4 `ToolFinished`

```python
@dataclass(frozen=True, slots=True, kw_only=True)
class ToolFinished(RuntimeEvent):
    status: RuntimeStatus = field(default=RuntimeStatus.STARTED, init=False)
    invocation_id: str
    tool_name: str
    success: bool
    risk_level: RiskLevel
    policy_decision: PolicyDecision
    approval_decision: ApprovalDecision | None
    duration_ms: int
    error_code: str | None
```

Constructor invariants freeze only the Tool-local outcome mapping:

```text
success=True  → error_code=None
success=False → error_code non-null
```

`RuntimeEvent.status` describes the Run/runtime lifecycle, while `success` and
`error_code` describe the Tool-local outcome. During the active Day 3 execution path,
`ToolStarted` and `ToolFinished` both carry `RuntimeStatus.STARTED`, including when the Tool
result is a local failure. For example:

```python
event = ToolFinished(
    success=False,
    error_code="COMMAND_EXIT_NONZERO",
    ...,
)
assert event.status is RuntimeStatus.STARTED
```

Only terminal Run completion/failure events use `RuntimeStatus.COMPLETED` or
`RuntimeStatus.FAILED`. A Tool-local failure does not terminalize the Run and may later be
followed by observation, replan, repair, or another Agent action when those approved
milestones exist.

`ToolFinished.risk_level` is the final observed risk copied from `ToolResult`. It may be
higher than `ToolStarted.risk_level` when containment/resource validation discovers a
security violation, but it may never be lower.

The event does not contain stdout, stderr, file content, full argv, environment, raw
exceptions, or private reasoning. Detailed bounded evidence remains in `ToolResult`.

Compatibility impact: additive event subclasses and one non-terminal status. Existing
terminal Run status semantics, event fields, ordering, rendering, and Day 1/2 sequences are
unchanged. Day 3 does not wire these events into the LangGraph Agent topology.

## 11. Exact denial and sequential semantics

### 11.1 Single invocation outcome matrix

| Scenario | Tool called | Sandbox/side effect | Result |
|---|---:|---|---|
| SAFE valid | yes | allowed when needed | success or structured execution failure |
| proposed SAFE path escape | yes | no | final DANGEROUS/DENIED `WORKSPACE_PATH_DENIED` before file read/process/side effect |
| WRITE interactive DENIED | no | no | `COMMAND_DENIED`, approval DENIED |
| WRITE interactive APPROVED, no Plan | no | no | `PLAN_REQUIRED`, approval APPROVED |
| WRITE auto | no | no | `COMMAND_DENIED`, approval DENIED |
| DANGEROUS | no | no | `PERMISSION_DENIED` or specific workspace/command denial |
| unknown tool | no | no | `TOOL_NOT_FOUND` |
| timeout | yes | process started, then terminated | `SANDBOX_TIMEOUT` |

Every attempted invocation that reaches Tool Runtime emits one `ToolStarted` followed by
exactly one `ToolFinished`. `ApprovalRequested` may appear between them only as defined in
section 10.2.

### 11.2 V1 sequential invocation semantics

There is no stable batch, fail-fast-batch, or continue-on-error API. Sequential ordering is
expressed only through awaited single invocations:

```python
result_a = await runtime.execute(invocation_a)
# The caller/Agent observes result_a before proposing the next action.
result_b = await runtime.execute(invocation_b)
```

The required observable order is:

```text
Tool A started → Tool A finished → observation → Tool B started → Tool B finished
```

Day 3 tests verify that A finishes before B starts. A private demo/test loop may stop or
continue after a failed result according to that caller's local purpose, but it is not a
Nexus public Tool Runtime contract and must not execute invocations concurrently.

## 12. Composition Root, CLI, and demo boundary

The Composition Root under `src/nexus/infrastructure/bootstrap/` is the only location that
assembles:

```text
ApprovalUnitOfWorkFactory
→ ApprovalPolicy
→ CommandPolicy
→ LocalProcessSandbox
→ native Tools
→ ToolRegistry
→ ToolRuntime
```

No new permanent CLI command is authorized. Existing `nexus`, `nexus chat`, and
`nexus session ...` public contracts remain unchanged.

Day 3 evidence is provided through fixture-based unit/integration/security tests and a
reproducible documented demo harness. The harness calls the Composition Root/application
boundary; it does not construct concrete repositories, policies, sandbox, or ToolRegistry
itself and does not become a supported public CLI.

## 13. Explicit alternatives and trade-offs

The Recommended column is the approved normative contract. Alternatives are retained only
as architecture-review context and are not authorized for Day 3 implementation.

| Decision | Recommended | Alternative | Trade-off |
|---|---|---|---|
| Tool values | frozen dataclasses and `StrEnum`, matching current code | Pydantic public models | Pydantic adds validation convenience but changes current style and increases coupling |
| Tool arguments/output | one bounded `JsonObject` contract with exact per-tool shapes | separate public input/output class per native Tool | per-tool classes are more statically precise but create a larger Day 3 public surface |
| Tool correlation | non-null Run and Session UUIDs | nullable correlation for SAFE tools | nullable values simplify isolated calls but weaken event/audit consistency |
| Registry | immutable construction, exact resolve, no mutation API | public register/unregister | mutation supports future MCP but is premature and complicates duplicate/thread safety |
| Sequential execution | repeated awaited `execute` calls; no batch API | public batch execution method | a batch API bypasses the normative Agent observation boundary and prematurely freezes failure semantics |
| Event delivery | existing RuntimeEvents through an injected internal emitter | new public event bus/observer protocol | an event bus is more extensible but is premature Day 8 abstraction |
| Approval model | one `ApprovalRequest` lifecycle entity | separate Request/Record/Decision models | separate types are expressive but expand the domain beyond Day 3 needs |
| Interactive decision source | minimal async callback | named UI/provider interface | provider interface helps future UI but is speculative before a real second adapter |
| Actor | validated TEXT with four frozen Day 3 values | database/domain enum | enum is stricter but prematurely freezes future authenticated actor identity |
| Approval UoW | independent UoW exposing approvals plus read validation repositories | add approvals to SessionUnitOfWork | adding to SessionUnitOfWork alters the approved Day 2 contract |
| Correlation integrity | existing FKs plus transactional application validation | add composite unique/FK to Day 2 runs schema | composite FK strengthens DB enforcement but changes frozen Day 2 schema |
| Paths | repository-relative inputs only | accept contained absolute inputs | absolute inputs are convenient but expose host-specific paths and enlarge escape handling |
| File search | literal lexical search and fnmatch filename search | regex/ripgrep public semantics | regex is more powerful but adds error/complexity/ReDoS and subprocess semantics early |
| File filtering | exclude `.git` only in Day 3 | implement complete `.gitignore`/Day 5 filter now | full filtering is useful but prematurely implements Day 5 retrieval behavior |
| SAFE shell commands | fixed non-configurable inspection allowlist | configuration-driven allowlist | configuration is flexible but adds an unapproved security/config contract |
| Git inspection | fixed dedicated argv and safety flags | arbitrary user-supplied Git read flags | arbitrary flags can invoke external diff/textconv or create unbounded behavior |
| Child environment | allowlist from empty environment | inherit everything except secret-looking keys | inheritance improves compatibility but risks leaking credentials and unstable behavior |
| Timeout | 30-second default, 300-second hard maximum | config-level timeout fields | configuration is useful later but expands the frozen RuntimeConfig contract now |
| Output cap | 1 MiB per stream/text result | capture all and truncate after completion | post-capture truncation is simpler but does not bound memory use |
| Process isolation | standard-library process group, timeout, best-effort descendants | OS Job Object/new dependency/container | stronger isolation is safer but conflicts with no-new-dependency and no-Docker Day 3 scope |
| Approval event status | add `AWAITING_APPROVAL` | reuse `STARTED` or `INTERRUPTED` | reuse avoids enum growth but misrepresents pending approval or durable graph interruption |
| WRITE approved without Plan | persist APPROVED, deny execution with `PLAN_REQUIRED` | coerce approval to DENIED | coercion hides the user's decision and damages audit semantics |

## 14. Existing public contract impact matrix

| Existing contract | Impact |
|---|---|
| `NexusRuntime.run/resume` | none in Day 3 |
| Day 1 `AgentState` | none |
| `GraphRuntime` and LangGraph topology | none |
| `ModelGateway` | none |
| `RuntimeEvent` common fields/serialization | additive subclasses only |
| existing `RuntimeStatus` values | unchanged; one additive non-terminal value proposed |
| Repository/Session/Run/SessionTurn domain models | none |
| existing repository ports | none |
| `SessionUnitOfWork` | none |
| Day 2 PostgreSQL tables | none; one new table proposed |
| checkpoint ownership/schema | none |
| RuntimeConfig | none |
| CLI | none |
| dependencies | none |

## 15. Explicitly deferred scope

This Addendum does not define or authorize:

- Plan/PlanStep or approved-Plan evidence;
- WRITE execution;
- `write_file` or `apply_patch`;
- Agent graph tool-call integration;
- validation/repair/replan;
- regex/ranked/semantic retrieval;
- `.gitignore`-complete retrieval filtering;
- MCP tool adapters or mutable Tool registry;
- parallel scheduling;
- Docker/Remote sandbox;
- hard CPU/RAM/network/container isolation;
- user identity/authentication;
- public `nexus tool` or `nexus shell` CLI;
- LangSmith or Day 8 tracing;
- evaluation or multi-agent behavior.

## 16. Approval record

Architect / Product Owner review approved:

1. the three enums and stable error codes;
2. exact fields/nullability/invariants for all six required value contracts;
3. Tool, Registry, Tool Runtime, Policy, Repository, UoW, and Sandbox signatures;
4. non-null Run/Session correlation for every Tool invocation;
5. WRITE-approved-without-Plan returning `PLAN_REQUIRED` without execution;
6. awaited single-invocation sequential behavior with no public batch API;
7. native Tool names, arguments, output shapes, and bounds;
8. fixed SAFE/WRITE/DANGEROUS command matrix;
9. relative-only workspace path inputs;
10. approval schema, lifecycle, actors, and application correlation validation;
11. `AWAITING_APPROVAL` and exact event payloads;
12. timeout, output cap, minimum environment, and Windows isolation limitation;
13. no new CLI, dependency, implementation, or Day 4+ scope.

The final two clarifications—operation/argv mutual consistency and monotonic risk
escalation—are incorporated. This Addendum is APPROVED and authorizes Day 3 implementation
within its frozen scope.
