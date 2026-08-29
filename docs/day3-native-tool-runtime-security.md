# Day 3 Native Tool Runtime & Security

Day 3 adds a Nexus-owned, async Tool Runtime for bounded native repository inspection.
The Agent graph is deliberately unchanged: this milestone proves the contracts, policy,
approval audit, sandbox, and native adapters before Day 4 authorizes planning and edits.

The frozen implementation contract is the
[Day 3 Contract Addendum](spec-addenda/Nexus_Day3_Contract_Addendum_v1.0.md).

## Governed execution boundary

One invocation follows this order:

```text
ToolInvocation
→ exact registry resolution
→ proposed SAFE / WRITE / DANGEROUS classification
→ ToolStarted
→ approval or hard deny when required
→ exact native argument validation
→ path canonicalization and workspace containment
→ optional final risk escalation
→ native read or LocalProcessSandbox
→ ToolResult
→ ToolFinished
```

Callers await one invocation and observe its result before proposing the next one. There
is no batch or parallel Tool API.

## Risk and approval matrix

| Class | Day 3 examples | Approval record | Execution |
|---|---|---|---|
| SAFE | file inspection, dedicated bounded Git reads, exact version commands | none | allowed after validation |
| WRITE | pytest, ruff check, mypy, uv build families | pending then terminal | never; approved returns `PLAN_REQUIRED` |
| DANGEROUS | unknown commands, interpreters, Git writes, operation/argv mismatch | terminal denied audit | hard-denied |

Auto mode is not full permission. It denies WRITE because automation is an approval
source, not a security bypass. DANGEROUS cannot be overridden by either interactive or
auto approval.

`ToolStarted.risk_level` is the proposed operation risk. A path-sensitive Tool can later
escalate the final result to DANGEROUS when canonicalization discovers traversal,
symlink, or junction escape. Risk never downgrades, and containment completes before file
content is read or a subprocess is started.

## Defense in depth

- Inputs are repository-relative; absolute and escaping paths fail closed.
- Directory links/junctions are not traversed by list/search, and direct linked-file
  escape is denied after canonical resolution.
- Dedicated `git_status`, `git_diff`, and `git_log` use fixed canonical argv shapes.
- Sandbox reclassifies normalized argv and cross-checks it with the preserved operation.
  For example, `operation=git_status` plus `git push` is DANGEROUS.
- Generic `shell` cannot borrow dedicated Git privileges and never uses `shell=True`.
- Trusted executables are pinned from a PATH stripped of empty, relative, and
  workspace-contained entries.
- Child environment is allowlisted; repository/user secrets and arbitrary variables are
  not inherited.
- Timeout is 30 seconds by default and at most 300 seconds. stdout and stderr are each
  retained up to 1 MiB while streaming; excess output is discarded and marked truncated.
- On Windows, Day 2 retains its Selector event loop for PostgreSQL compatibility. The
  sandbox isolates exec-style child processes on a private Proactor loop without changing
  application-wide policy. Standard-library process-tree termination remains best effort.

Direct subprocess calls in Agent/application code would bypass classification, approval,
workspace checks, environment filtering, timeouts, output bounds, and structured events.
All process execution therefore remains inside `LocalProcessSandbox`.

An operation/argv spoof attempt produces a bounded failure before process start:

```json
{
  "success": false,
  "risk_level": "DANGEROUS",
  "policy_decision": "DENIED",
  "approval_decision": "DENIED",
  "error": {"code": "PERMISSION_DENIED", "retryable": false}
}
```

A path that initially classified SAFE but resolves outside the workspace instead reports
`WORKSPACE_PATH_DENIED`, final `DANGEROUS`, and `DENIED`; no file content is read.

## Native Tool demo

The demo uses the Composition Root and executes only SAFE invocations sequentially. It
does not add a supported Nexus CLI command and does not require a model call:

```powershell
$env:UV_CACHE_DIR = ".uv-cache"
uv run python scripts/day3_native_tool_demo.py --workspace . --read README.md
```

The JSON output contains the ordered `ToolStarted`/`ToolFinished` events and bounded
results for `list_files`, `search_files`, `read_file`, and `git_status`.

## Acceptance test matrix

| Security/contract evidence | Test location |
|---|---|
| traversal, absolute, symlink/junction escape | `tests/unit/test_native_tools.py` |
| SAFE/WRITE/DANGEROUS matrix and Git identity spoofing | `tests/unit/test_security_policy.py`, `tests/unit/test_sandbox.py` |
| dangerous shell/Git mutation has no fixture effect | `tests/unit/test_sandbox.py` |
| timeout, environment allowlist, independent 1 MiB stream caps | `tests/unit/test_sandbox.py` |
| fixed Git read behavior and output shape | `tests/integration/test_git_tools.py` |
| approval policies, Plan-required result, events, sequential ordering | `tests/unit/test_tool_runtime.py` |
| migration, pending/approved/denied persistence, immutable terminal state, correlation | `tests/integration/test_migrations.py`, `tests/integration/test_approval_persistence.py` |

Run the complete Day 3 and prior suite with:

```powershell
$env:UV_CACHE_DIR = ".uv-cache"
uv run ruff check .
uv run mypy src tests scripts
uv run pytest
```

Day 4 planning/editing, validation, Agent graph Tool calls, MCP, parallel execution, and
container sandboxing remain intentionally deferred.
