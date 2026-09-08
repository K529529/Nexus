# Nexus Day 6 Contract Addendum v0.3

**Project:** Nexus
**Milestone:** Day 6 — MCP Integration
**Status:** Architecture Contract — Approved Implementation Contract
**Baseline:** `Nexus V1 Product Requirements & 10-Day Engineering Specification v1.1.1 — Implementation Ready Frozen Baseline`
**Applies to branch:** `feature/day06-tool-execution-safety`
**Approval date:** 2026-09-08
**Approved decisions incorporated:** user-only MCP TOML; repository MCP fail-closed behavior; MCP WRITE fail-closed behavior; SAFE-only Agent exposure; real Agent-path acceptance; transient-only connection retry
**Scope rule:** This Addendum only resolves Day 6 contract gaps. It MUST NOT redesign prior Day contracts or implement Day 7+ scope.

---

## 1. Purpose and precedence

This Addendum freezes the public and architectural contracts required to implement Day 6 without forcing the Implementation Engineer to make Product Owner / Architect decisions.

It supplements the frozen baseline. If this Addendum conflicts with the Day 6 section only because the baseline left an implementation-facing contract unspecified, this Addendum controls that Day 6 detail. It does **not** override the V1 architecture principles, the normative graph topology, the existing Tool/ToolRuntime security model, configuration precedence, or the Codex decision boundary.

Day 6 remains narrowly scoped to:

> Connect one or more configured MCP servers as an infrastructure tool source, adapt discovered MCP tools into the existing Nexus-owned `Tool` contract, register them into the same `ToolRegistry` used by native tools, and execute eligible MCP tool calls through the existing planning / policy / approval / event / ToolRuntime path.

Nexus remains an **MCP client/host**, not an MCP server.

---

## 2. Frozen Day 6 architectural invariant

The canonical dependency direction for MCP tools is:

```text
Configured MCP Server
        ↓
     MCPManager
        ↓
 MCPToolDescriptor
        ↓
  MCPToolAdapter
        ↓
   Nexus Tool port
        ↓
    ToolRegistry
        ↓
     ToolRuntime
        ↓
CommandPolicy / ApprovalPolicy / RuntimeEvents
```
The Agent and Graph MUST NOT branch on tool source.

The following is forbidden:

```python
if tool_source == "native":
    ...
elif tool_source == "mcp":
    ...
```

The Agent-facing invariant is:

```python
tool = registry.resolve(tool_name)
result = await tool_runtime.execute(invocation, authorization=...)
```

Native and MCP tools may have different infrastructure adapters, but they MUST converge before `ToolRegistry` / `ToolRuntime` execution.

---

## 3. MCP SDK and protocol compatibility

### 3.1 Python dependency

Day 6 MAY add the official Python MCP SDK as a production dependency. The frozen dependency range is:

```text
mcp>=2,<3
```

No alternative MCP client framework may be introduced in Day 6.

### 3.2 Protocol version

Nexus MUST NOT hard-code a single MCP wire protocol revision in domain or application code.

The official SDK negotiation behavior is authoritative. Nexus should use the SDK's normal compatibility/negotiation path and keep negotiated protocol details inside the MCP infrastructure boundary.

Protocol-version-specific types MUST NOT leak into Nexus domain models, Tool contracts, graph state, runtime events, or planner prompts.

---

## 4. V1 implemented transport

### 4.1 Supported transport

Day 6 implements **stdio only**.

```text
MCPTransport.STDIO
```

The architecture may preserve a future transport seam, but Day 6 MUST NOT implement:

- Streamable HTTP;
- SSE compatibility transport;
- WebSocket/custom transports;
- OAuth;
- remote account automation;
- server-side MCP functionality.

### 4.2 Transport ownership

The stdio process is connection infrastructure owned by `MCPManager`; it is not an Agent-proposed `shell` Tool call.

However, the server command MUST come only from resolved RuntimeConfig before agent execution. The model MUST NOT be allowed to construct, mutate, or dynamically launch an MCP server command.

MCP server process stdout is protocol traffic and MUST NOT be treated as user-visible shell output. Server stderr MAY be captured for sanitized diagnostics, subject to the redaction rules in this Addendum.

---

## 5. Frozen configuration contract

### 5.1 RuntimeConfig additions

Add the following Nexus-owned configuration values:

```python
class MCPTransport(StrEnum):
    STDIO = "stdio"


class MCPToolRiskConfig(BaseModel):
    tool_name: str
    risk_level: RiskLevel


class MCPServerConfig(BaseModel):
    server_id: str
    enabled: bool = True
    transport: MCPTransport = MCPTransport.STDIO
    command: str
    args: tuple[str, ...] = ()
    inherit_environment: bool = True
    connect_timeout_seconds: float = 10.0
    tool_timeout_seconds: float = 30.0
    max_connect_attempts: int = 2
    tool_risks: tuple[MCPToolRiskConfig, ...] = ()


class RuntimeConfig(BaseModel):
    ...
    mcp_enabled: bool = False
    mcp_servers: tuple[MCPServerConfig, ...] = ()
```

Equivalent immutable Pydantic representation is acceptable; field names and semantics are frozen.

### 5.2 Validation rules

`MCPServerConfig` MUST validate:

- `server_id` is non-empty and matches `^[a-z][a-z0-9_-]{0,63}$`;
- `command` is non-empty;
- every `args` item is a non-empty string;
- `connect_timeout_seconds` is `> 0` and `<= 60`;
- `tool_timeout_seconds` is `> 0` and `<= 300`;
- `max_connect_attempts` is integer `1..3` and boolean values are rejected;
- duplicate `server_id` values are configuration errors;
- duplicate `tool_name` entries inside one server's `tool_risks` are configuration errors;
- Day 6 rejects any transport other than `stdio`.

If `mcp_enabled == False`, no MCP connection is attempted even if `mcp_servers` contains entries.

If `mcp_enabled == True`, only entries with `enabled == True` are connected.

An empty enabled-server set is valid and produces no MCP tools.

### 5.3 Configuration source bindings and precedence

Day 6 freezes the following user-level `~/.nexus/config.toml` representation:

```toml
[mcp]
enabled = true

[[mcp.servers]]
server_id = "everything"
enabled = true
transport = "stdio"
command = "cmd"
args = [
  "/c",
  "npx",
  "-y",
  "@modelcontextprotocol/server-everything@2026.8.31",
]
inherit_environment = true
connect_timeout_seconds = 10.0
tool_timeout_seconds = 30.0
max_connect_attempts = 2

[[mcp.servers.tool_risks]]
tool_name = "echo"
risk_level = "SAFE"
```

The public TOML mapping is:

```text
[mcp].enabled                         -> RuntimeConfig.mcp_enabled
[[mcp.servers]]                      -> RuntimeConfig.mcp_servers
[[mcp.servers.tool_risks]]           -> MCPServerConfig.tool_risks
```

All `MCPServerConfig` fields other than `server_id` and `command` are optional and use
the defaults frozen in §5.1. Every present `tool_risks` entry requires both `tool_name`
and `risk_level`.

MCP source binding is intentionally narrower than the generic configuration-source list:

- Day 6 adds no MCP CLI override.
- Day 6 defines no `NEXUS_MCP_SERVERS` structured environment variable.
- MCP process/server definitions and `mcp_enabled` come only from user-level
  `~/.nexus/config.toml` or the empty disabled default.
- Repository `.nexus/config.toml` MUST NOT define, override, clear, or enable MCP
  configuration.
- The presence of a top-level repository `mcp` table, including `[mcp]`, an inline
  `mcp` table, or `[[mcp.servers]]`, MUST fail closed as `ConfigurationError`; it MUST
  NOT be ignored and MUST NOT reach Composition Root process startup.
- MCP server credentials are not fields in this TOML contract. They continue to come
  from the inherited process/user environment or another already-approved secret source.

The MCP-specific source order is therefore:

```text
user ~/.nexus/config.toml > disabled/empty defaults
```

Other non-MCP repository fields retain their frozen precedence over user configuration.
Within the single allowed MCP TOML source, `mcp_servers` is one complete tuple: servers
are never merged by `server_id`, and `tool_risks` remain part of their containing server
value. `[mcp] servers = []` resolves to an empty tuple.

A TOML document MUST NOT define both `servers = []` and
`[[mcp.servers]]` entries.

Day 6 does not add a new secret interpolation language.

`inherit_environment=True` means the child stdio server may inherit the Nexus process
environment according to the SDK/adapter implementation. Nexus MUST NOT serialize or log
inherited environment values.

MCP TOML MUST NOT contain credentials or secret token values. If an MCP server requires
credentials, they must come from the process/user environment or another already-approved
secret source; implementing OAuth/account login is out of scope.

---

## 6. Reference MCP server — Owner decision

The Day 6 reference server is frozen as:

```text
DAY6_REFERENCE_MCP_SERVER_ID = "everything"
DAY6_REFERENCE_MCP_SERVER_PACKAGE = "@modelcontextprotocol/server-everything@2026.8.31"
DAY6_REFERENCE_MCP_TRANSPORT = "stdio"
```

### 6.1 Windows acceptance command

```text
command = "cmd"
args = [
  "/c",
  "npx",
  "-y",
  "@modelcontextprotocol/server-everything@2026.8.31"
]
```

### 6.2 POSIX equivalent

```text
command = "npx"
args = [
  "-y",
  "@modelcontextprotocol/server-everything@2026.8.31"
]
```

The package version is intentionally pinned for reproducible Day 6 evidence. Documentation MAY mention how an operator later changes the version, but tests/evidence MUST NOT depend on `@latest`.

The reference server is chosen only as an MCP client-test server. Day 6 MUST NOT expose all of its tools to the Agent merely because the server provides them.

The Agent exposure rule is frozen:

1. every successfully discovered, validated, and adapted MCP tool is registered in the
   single unified `ToolRegistry`;
2. only an MCP tool whose effective resolved risk is exactly `SAFE` is included in
   normalized metadata supplied to the concrete Planner and Agent;
3. explicitly configured `WRITE` tools remain registered but are not Agent-visible;
4. explicitly configured `DANGEROUS` tools and tools with no configured risk
   (effective `DANGEROUS`) remain registered but are not Agent-visible;
5. a programmatic direct invocation of any registered non-SAFE MCP tool still traverses
   `ToolRuntime` and is denied/audited by the frozen policy;
6. Day 6 adds no independent exposure allowlist configuration.

---

## 7. MCP domain-neutral metadata

MCP SDK wire types MUST stay inside the infrastructure adapter.

Nexus defines the following Day 6 value objects outside the raw SDK boundary:

```python
@dataclass(frozen=True, slots=True)
class MCPToolDescriptor:
    server_id: str
    remote_name: str
    registry_name: str
    description: str | None
    input_schema: JsonObject


@dataclass(frozen=True, slots=True)
class MCPConnectionInfo:
    server_id: str
    connected: bool
    server_name: str | None
    server_version: str | None
    negotiated_protocol_version: str | None
```

These values contain only safe, JSON-compatible or primitive metadata. They MUST NOT contain:

- raw SDK session/client objects;
- credentials;
- environment values;
- subprocess handles;
- private model reasoning;
- arbitrary server stderr.

`MCPConnectionInfo` is infrastructure/diagnostic metadata, not a new business persistence table.

No Day 6 database migration is required.

---

## 8. MCPManager public contract

Freeze the Day 6 manager port as an async lifecycle interface with these semantics:

```python
class MCPManager(Protocol):
    async def connect(self) -> tuple[MCPConnectionInfo, ...]: ...
    async def list_tools(self) -> tuple[MCPToolDescriptor, ...]: ...
    async def call_tool(
        self,
        *,
        server_id: str,
        remote_name: str,
        arguments: JsonObject,
        timeout_seconds: float,
    ) -> JsonObject: ...
    async def close(self) -> None: ...
```

Equivalent method placement may be used internally, but these operations and semantics are frozen.

### 8.1 `connect()` semantics

- Idempotent within one manager lifecycle: a second successful call MUST NOT create duplicate live sessions.
- Connects all enabled configured servers sequentially in deterministic configuration order.
- One server receives at most `max_connect_attempts` total attempts.
- Retry is allowed only for retryable transport/startup failures.
- Explicit transient timeout or connection failures are retryable while attempts remain.
- Deterministic failures—including client construction/validation errors, invalid or
  missing executables, permission failures, protocol/handshake errors, and unclassified
  exceptions—MUST map immediately to `MCP_CONNECT_FAILED` and MUST NOT start another
  attempt.
- No exponential-backoff dependency is required; a small private async delay is implementation detail.
- If any enabled server remains unavailable after its attempts, `connect()` raises `MCPError` and bootstrap fails closed for MCP-enabled startup.
- Partially opened connections MUST be closed before propagating the failure.

### 8.2 `list_tools()` semantics

- Requires successful `connect()` first.
- Returns the discovered tool set from all connected servers.
- Ordering is deterministic: server configuration order, then server-provided tool order.
- Discovery/schema errors map to `MCPError`.
- Discovery MUST NOT register tools by itself; registration is Composition Root responsibility after adaptation.

### 8.3 `call_tool()` semantics

- Requires a connected `server_id` and discovered `remote_name`.
- Calls exactly one remote MCP tool.
- Day 6 is sequential: it MUST NOT use `asyncio.gather` or equivalent parallel Agent tool scheduling.
- Applies the configured per-server `tool_timeout_seconds` unless a smaller approved adapter timeout is supplied.
- Transport failure, timeout, protocol failure, invalid result, or MCP error result MUST be surfaced through `MCPError` semantics and then mapped by `MCPToolAdapter` into Nexus `ToolResult` failure.

### 8.4 `close()` semantics

- Idempotent.
- Closes all live sessions/process resources in reverse connection order.
- Attempts cleanup of every opened connection even if one close operation fails.
- During normal bootstrap teardown, cleanup failure MUST NOT replace an already-produced successful Nexus final result; it MAY emit/log a sanitized infrastructure warning.
- During failed bootstrap, cleanup failure is secondary to the original `MCPError`.

---

## 9. Tool naming and duplicate policy

The current exact-name `ToolRegistry` duplicate rejection remains unchanged.

MCP tools MUST be adapted to this registry name:

```text
mcp.<server_id>.<remote_tool_name>
```

Examples:

```text
mcp.everything.echo
mcp.everything.add
mcp.github.get_issue
```

### 9.1 Naming rules

- `server_id` uses the validated config identifier.
- `remote_tool_name` is the exact server-advertised tool name for remote invocation.
- `registry_name` is the namespaced Nexus-visible name.
- The adapter MUST NOT rename the remote name when calling the server.
- Native tool names remain unchanged.

### 9.2 Duplicate handling

The following MUST fail before runtime execution:

1. two configured servers with the same `server_id`;
2. one MCP server advertising the same `remote_name` twice;
3. two adapted tools producing the same final `registry_name`;
4. an adapted registry name colliding with an existing ToolRegistry entry.

Collision failure maps to `ConfigurationError` or `MCPError` during bootstrap/discovery, not a silently overwritten tool.

No "last wins" behavior is permitted.

---

## 10. MCPToolAdapter contract

Each discovered MCP tool is exposed through the **existing** Nexus `Tool` protocol.

Conceptually:

```python
class MCPToolAdapter:
    @property
    def name(self) -> str:
        return descriptor.registry_name

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        ...
```

The adapter MUST NOT call `ApprovalPolicy`, persist approvals, or emit `ToolStarted` / `ToolFinished` directly. Those remain `ToolRuntime` responsibilities.

The adapter owns only:

- validating that `invocation.tool_name == registry_name`;
- validating JSON-compatible arguments against the discovered MCP input schema to the extent provided by the official SDK / chosen lightweight validation path;
- invoking exactly the mapped remote tool through `MCPManager`;
- mapping MCP success/failure into the existing Nexus `ToolResult` shape;
- preserving invocation identity;
- sanitizing output/error data.

The adapter MUST NOT expose raw MCP SDK result classes beyond the infrastructure/tool adapter boundary.

---

## 11. Risk classification — fail closed

### 11.1 No heuristic classification

Day 6 MUST NOT infer safety from:

- MCP tool name;
- description text;
- input-schema field names;
- LLM judgment;
- server identity alone.

Examples such as `get_*`, `read_*`, `delete_*`, or `create_*` MUST NOT be used as security heuristics.

### 11.2 Explicit risk mapping

For each MCP tool, risk is resolved from the server's configured `tool_risks` map using the **remote tool name**.

Conceptually:

```text
("everything", "echo") -> SAFE
("some_server", "update_record") -> WRITE
```

The effective ToolRuntime operation is still the namespaced registry name:

```text
mcp.everything.echo
```

### 11.3 Unknown risk

If no explicit risk is configured for an MCP tool:

```text
RiskLevel.DANGEROUS
```

The operation therefore fails closed through the existing ToolRuntime dangerous-operation path.

This rule is mandatory.

### 11.4 Existing CommandPolicy port remains authoritative

Do not add a second MCP-only approval runtime.

The existing `CommandPolicy.classify(operation, arguments)` remains the single classification entry used by `ToolRuntime`.

`DefaultCommandPolicy` MAY be extended with immutable MCP risk metadata (for example a mapping keyed by namespaced registry name), but the public `CommandPolicy` port MUST NOT change.

Native tool risk semantics MUST remain unchanged.

---

## 12. SAFE / WRITE / DANGEROUS MCP execution contract

### 12.1 SAFE MCP tool

An explicitly configured SAFE MCP tool:

- may be planned as a normal `PlanStep.tool_name`;
- executes through `ToolRuntime`;
- emits the existing `ToolStarted` and `ToolFinished` events;
- increments the existing `tool_call_count` exactly once per actual remote invocation;
- does not require interactive approval solely because it is MCP.

The Day 6 real E2E MUST use a SAFE reference-server tool.

### 12.2 WRITE MCP tool

The frozen V1 rule is conservative:

> Day 6 does **not** add generic MCP WRITE authorization to
> `Plan.AuthorizationScope`.

Therefore an MCP tool explicitly configured as `WRITE` MUST NOT execute a remote side
effect in Day 6.

After the existing `CommandPolicy.classify(operation, arguments)` returns `WRITE`
for an MCP registry operation, `ToolRuntime` MUST fail closed before invoking the Tool
adapter. The frozen denial behavior is:

1. record a denied audit entry using the existing approval/audit persistence boundary;
2. return a failed `ToolResult` with:
   - `risk_level = WRITE`;
   - `policy_decision = DENIED`;
   - `approval_decision = DENIED`;
   - `error.code = "MCP_WRITE_NOT_AUTHORIZED"`;
   - `error.retryable = False`;
3. do not emit `ApprovalRequested`;
4. do not call `ApprovalPolicy.request()`;
5. do not call `MCPToolAdapter.execute()` or `MCPManager.call_tool()`.

The existing `ToolStarted` and `ToolFinished` events still bracket the denied Tool
invocation. `ToolFinished.error_code` is `MCP_WRITE_NOT_AUTHORIZED`.

This is a centralized ToolRuntime security rule, not a second MCP-only approval runtime.
It does not bypass authorization because no WRITE capability is granted and no remote
side effect is attempted.

This contract intentionally preserves Day 4's Plan-before-WRITE invariant without
changing `PlanStep`, `AuthorizationScope`, `CommandPolicy`, or `ApprovalPolicy`
public contracts.

A future approved specification may add generic external write authorization, but Codex
MUST NOT invent it in Day 6.

### 12.3 DANGEROUS MCP tool

An MCP tool classified as `DANGEROUS` follows the existing hard-deny behavior and MUST NOT call the MCP server.

Auto mode MUST NOT bypass this rule.

---

## 13. MCP result → Nexus ToolResult mapping

`MCPToolAdapter` returns the existing `ToolResult` contract.

### 13.1 Successful result

A successful remote result maps to:

```text
success = True
error = None
policy_decision = ALLOWED
approval_decision = None
risk_level = resolved SAFE risk for executable Day 6 MCP E2E
```

`output` MUST be a JSON object. The frozen normalized envelope is:

```python
{
    "content": [...],
    "structured_content": {...} | None,
    "is_error": False,
}
```

`content` contains a sanitized JSON-compatible representation of MCP content blocks. Binary/blob payloads MUST NOT be embedded unboundedly into a ToolResult. Unsupported/non-JSON content is represented by a bounded safe textual/metadata form.

### 13.2 MCP-declared tool error

If the MCP call returns a protocol-valid tool result that is marked as an error, map it to:

```text
success = False
output = None
error.code = "MCP_TOOL_ERROR"
error.retryable = False
```

The error message must be user-readable and sanitized.

### 13.3 Transport / timeout / protocol failures

Use stable error codes:

```text
MCP_CONNECT_FAILED
MCP_DISCOVERY_FAILED
MCP_TOOL_TIMEOUT
MCP_CALL_FAILED
MCP_INVALID_SCHEMA
MCP_INVALID_RESULT
```

The adapter/runtime may use a narrower subset when appropriate, but generic raw exception text MUST NOT become the public contract.

Retryability:

- explicit transient initial timeout/connection failure: `retryable=True` while attempts
  remain;
- deterministic or unclassified connect failure: `retryable=False`, immediately mapped
  to `MCP_CONNECT_FAILED`, with no repeated process start;
- tool timeout: `retryable=True` at infrastructure level, but Nexus MUST NOT automatically replay a remote tool call in Day 6 because idempotency is unknown;
- schema/configuration failure: `retryable=False`;
- MCP-declared tool error: `retryable=False` unless the protocol/server gives an explicit trustworthy retry signal supported by the SDK.

Day 6 MUST NOT automatically retry `call_tool()` after the remote call may have reached the server.

---

## 14. Timeout contract

Two distinct timeout categories are frozen:

```text
connect_timeout_seconds = 10.0 default
server tool_timeout_seconds = 30.0 default
```

Connect timeout covers creation/negotiation/discovery connectivity for one attempt as appropriate to the SDK adapter.

Tool timeout covers one MCP remote tool call.

Timeout cancellation MUST cleanly unwind the current SDK call. A timed-out remote call MUST return a structured failure and MUST NOT be silently retried.

`asyncio.CancelledError` from Nexus cancellation remains cancellation and MUST be re-raised rather than converted into a normal MCP failure.

---

## 15. Composition Root and lifecycle

`src/nexus/infrastructure/bootstrap/` remains the only concrete dependency assembly location.

Frozen bootstrap order when MCP is enabled:

```text
load RuntimeConfig
→ construct database/checkpoint/model/security infrastructure
→ construct MCPManager from mcp_servers
→ MCPManager.connect()
→ MCPManager.list_tools()
→ adapt discovered MCP tools to Nexus Tool
→ combine native tools + MCPToolAdapter instances
→ construct one ToolRegistry
→ construct ToolRuntime
→ construct Context/Planner/Graph/NexusRuntime
→ yield application
→ finally MCPManager.close()
→ close checkpoint/database resources
```

Exact cleanup nesting may differ to preserve existing resource guarantees, but all resources MUST be deterministically released.

When MCP is disabled, bootstrap behavior for Day 1–5 capabilities MUST remain functionally unchanged.

`bootstrap_tool_application()` MAY accept/build MCP only if required by Day 6 tests; it MUST NOT create a second divergent MCP construction path. Shared private Composition Root helpers are preferred.

---

## 16. Agent / Planner visibility

MCP tools whose effective resolved risk is exactly `SAFE` MUST be exposed to the
concrete Planner and Agent only after:

1. successful connection;
2. successful discovery;
3. successful adaptation;
4. registry collision validation;
5. risk metadata resolution.

The normalized planner/agent-facing metadata MUST contain the information required for a
model to select and call an eligible SAFE MCP tool. WRITE, DANGEROUS, and unconfigured
effective-DANGEROUS MCP tools MUST NOT be included in this metadata, even though they
remain registered in the unified `ToolRegistry`.

```text
registry_name
description
input_schema
risk_level
source = "mcp"
server_id
```

The same normalized metadata path MAY include native Tool metadata, but the Planner and
Agent MUST NOT branch on Tool source when selecting or invoking a Tool.

The metadata MUST NOT include credentials, raw environment variables, SDK objects,
subprocess command details, or raw server stderr.

The concrete Planner must be able to create a normal `PlanStep.tool_name` using the
namespaced MCP registry name. The concrete Agent must be able to emit a normal
`ToolAction` with that name and JSON-compatible arguments. Neither the public
`Planner` port nor the public Nexus Tool contract may be widened solely to expose MCP.

No Day 6 requirement exists to teach the model MCP protocol internals. The model sees
only normalized Nexus Tool metadata.

---

## 17. Error boundary and user-visible behavior

Add/use `MCPError` as a `NexusError` subtype with stable `code` and `retryable` semantics consistent with the existing error hierarchy.

The following boundaries are mandatory:

```text
SDK/transport exceptions
        ↓
      MCPError
        ↓
 MCPToolAdapter / bootstrap mapping
        ↓
 Nexus structured ToolResult or visible startup error
```

Raw MCP SDK exception classes MUST NOT escape into Agent/domain code.

User-facing messages should identify:

- server id;
- high-level failed phase (`connect`, `discover`, `call`, `close`);
- safe tool name when relevant;
- retryability when useful.

They MUST NOT include:

- API keys/tokens;
- full environment dumps;
- credential-bearing command strings;
- raw private prompts;
- raw stderr without sanitization.

---

## 18. Real reference-server E2E contract

Day 6 acceptance requires one real stdio session against the pinned Everything Server
through the real Nexus Agent path. A direct ToolRuntime-only E2E is insufficient for Day
6 acceptance.

The minimum reproducible flow is:

```text
RuntimeConfig enables server "everything"
→ bootstrap connects to real stdio server
→ discover tools
→ adapt at least one explicitly SAFE tool
→ validate registry collision and risk metadata
→ expose normalized Tool metadata to concrete Planner/Agent
→ concrete Planner creates a Plan containing "mcp.everything.echo"
→ approval gate preserves the existing Plan lifecycle
→ concrete Agent selects the MCP Tool and produces JSON arguments
→ execute_tool creates ToolInvocation("mcp.everything.echo")
→ ToolRuntime resolves and risk-classifies the invocation
→ ToolStarted emitted
→ real MCP call executed
→ ToolFinished emitted
→ observe receives the structured ToolResult
→ manager/server process closed
```

The model-driven path MAY use a deterministic scripted `ModelGateway` fixture for
reproducibility, but it MUST exercise the production concrete Planner, concrete Agent,
graph execution node, ToolRuntime, MCP adapter, and real MCP stdio server. It MUST NOT
replace the Planner/Agent path with a test-authored direct ToolInvocation.

The selected acceptance tool MUST be deterministic and free of external side effects.
`echo` is the required acceptance tool when present in the pinned server version.

If the pinned reference server changes its exact advertised tool set or no longer
exposes `echo`, implementation MUST STOP and report the incompatibility rather than
silently choose a different mutating or dangerous tool.

The E2E evidence must record:

```text
server_id
package/version
transport
selected registry tool name
risk classification
Planner PlanStep evidence
Agent ToolAction evidence
ToolStarted/ToolFinished presence
success/failure
sanitized output summary
process/lifecycle cleanup confirmation
```

Do not commit machine-specific absolute paths or secrets in evidence.

---

## 19. Required tests — frozen matrix

Day 6 implementation MUST include tests covering all of the following.

### 19.1 Config

1. MCP disabled default causes zero connection attempts.
2. valid user-level stdio server TOML config parses.
3. any repository top-level `mcp` table fails closed as `ConfigurationError`, including
   disabled, empty, inline, and server-defining forms.
4. user MCP configuration remains effective when repository TOML contains only allowed
   non-MCP overrides.
5. no MCP CLI override or `NEXUS_MCP_SERVERS` binding is introduced.
6. invalid/duplicate server id is rejected.
7. invalid timeout/attempt values are rejected.
8. unsupported transport is rejected.
9. config rendering/repr does not leak inherited environment secret values.

### 19.2 Manager lifecycle

11. connect success.
12. deterministic multi-server connection order fixture.
13. explicit transient connect failure retries up to configured total attempts.
14. deterministic/unclassified connect or client-construction failure maps immediately
    to `MCP_CONNECT_FAILED` without another start attempt.
15. exhausted transient connect raises `MCPError`.
16. partial-connect failure closes earlier sessions.
17. close is idempotent and attempts all resources even if one close fails.

### 19.3 Discovery / naming

18. mocked tool discovery maps to `MCPToolDescriptor`.
19. registry name is exactly `mcp.<server_id>.<remote_name>`.
20. duplicate remote tool name is rejected.
21. duplicate final registry name is rejected.
22. native/MCP collision cannot overwrite an existing tool.

### 19.4 Policy/security

23. explicitly SAFE MCP tool is classified SAFE.
24. unconfigured MCP tool risk defaults DANGEROUS.
25. DANGEROUS MCP tool never reaches `MCPManager.call_tool()`.
26. WRITE MCP tool returns `MCP_WRITE_NOT_AUTHORIZED`.
27. WRITE MCP denial persists a denied audit record.
28. WRITE MCP denial emits no `ApprovalRequested`.
29. WRITE MCP denial never calls `ApprovalPolicy`, adapter, or server.
30. auto mode cannot execute a DANGEROUS MCP tool.
31. risk is not inferred from description/name heuristics.

### 19.5 Adapter/result/error

32. success maps to Nexus `ToolResult` and preserves invocation/tool identity.
33. MCP-declared error maps to `MCP_TOOL_ERROR`.
34. timeout maps to `MCP_TOOL_TIMEOUT` without automatic replay.
35. malformed schema maps to `MCP_INVALID_SCHEMA`.
36. invalid/malformed result maps to `MCP_INVALID_RESULT` or
    `MCP_CALL_FAILED` as appropriate.
37. raw SDK exception type does not escape the adapter boundary.
38. cancellation is re-raised.

### 19.6 Planner / Agent visibility

39. an effective-SAFE MCP tool is registered and visible to Planner/Agent.
40. an effective-WRITE MCP tool is registered but not visible to Planner/Agent.
41. an explicitly DANGEROUS or unconfigured effective-DANGEROUS MCP tool is registered
    but not visible to Planner/Agent.
42. MCP metadata remains unavailable until connection, discovery, adaptation, collision
    validation, and risk resolution all succeed.
43. normalized SAFE MCP metadata reaches both the production concrete Planner and Agent.
44. planner/agent metadata excludes environment, command, stderr, credential, and SDK data.
45. a deterministic model-driven integration test creates an MCP `PlanStep` and MCP
    `ToolAction` from normalized SAFE metadata.
46. that action traverses the graph `execute_tool → observe` path.

### 19.7 Existing runtime integration

47. MCP SAFE call emits one `ToolStarted` and one `ToolFinished`.
48. one actual MCP invocation increments existing `tool_call_count` exactly once.
49. sequential ordering is preserved; no parallel Agent tool execution.
50. existing native tool tests remain unchanged/green.
51. MCP-disabled regression path remains green.

### 19.8 Real Agent-path E2E

52. pinned Everything Server is started via stdio.
53. expected deterministic SAFE `echo` tool is discovered.
54. the production concrete Planner/Agent select and invoke it through the normal graph
    and ToolRuntime path.
55. `ToolStarted`, `ToolFinished`, and observe evidence are present.
56. result is structured and user-readable.
57. process/session cleanup is proven.

---

## 20. Dependency and module placement

Expected Day 6 placement:

```text
src/nexus/
  config/
    models.py                         # MCP config additions
  domain/
    mcp.py                            # Nexus-owned MCP metadata values if needed
    ports/
      mcp.py                          # MCPManager port if kept as domain/application port
  infrastructure/
    mcp/
      __init__.py
      manager.py                      # official SDK adapter / lifecycle
      stdio.py                        # optional private transport helper
    bootstrap/
      composition.py                  # assembly + cleanup
  tools/
    mcp.py                            # MCPToolAdapter
  security/
    command_policy.py                 # extend existing policy with explicit MCP risk map
  errors/
    ...                               # MCPError if not already present

tests/
  unit/
  integration/
  e2e/

docs/
  mcp-guide.md
  day6-acceptance-evidence.md
```

Exact private filenames may change. Dependency direction and ownership may not.

Do not create a parallel `mcp_tool_runtime.py`.

Do not move MCP SDK types into `domain`.

---

## 21. Documentation deliverable contract

`docs/mcp-guide.md` MUST document:

- Nexus is an MCP client/host, not a server;
- stdio-only V1 transport;
- user-level configuration example and repository `[mcp]` fail-closed rule;
- namespaced tool naming;
- explicit risk mapping and default DANGEROUS behavior;
- why Day 6 WRITE MCP calls are fail-closed;
- how to run the pinned Everything Server acceptance setup;
- Windows and POSIX command differences;
- connection/discovery/call failure behavior;
- credentials/environment safety;
- current V1 limitations and future transport seam without promising implementation.

`docs/day6-acceptance-evidence.md` MUST contain reproducible real-server evidence, not only mocked-test evidence.

---

## 22. Explicit out-of-scope / forbidden implementation

Codex MUST NOT implement any of the following in Day 6:

- Nexus MCP server;
- Streamable HTTP/SSE production transport;
- OAuth/login/account automation;
- MCP resources/prompts as new Agent primitives;
- MCP sampling/elicitation support;
- remote skill marketplace;
- generic remote code execution outside configured stdio server startup;
- dynamic model-authored MCP server configuration;
- parallel MCP/Native tool scheduling;
- automatic retry of a possibly executed remote tool;
- generic MCP WRITE Plan authorization redesign;
- Day 7 Skills;
- Day 8 LangSmith observability implementation;
- Day 9 evaluation system;
- unrelated ToolRegistry redesign;
- persistence/schema migration solely for MCP connection metadata.

If implementation appears to require one of these, Codex MUST STOP and report the contract conflict.

---

## 23. Day 6 acceptance criteria — executable interpretation

Day 6 passes only when all of the following are true:

1. MCP is disabled by default and Day 1–5 behavior regresses cleanly.
2. The official Python MCP SDK v2 line is used behind an infrastructure adapter.
3. Approved user-only MCP TOML source binding and repository `[mcp]` fail-closed rule are
   implemented.
4. At least one configured stdio MCP server connects through `MCPManager`.
5. Every discovered/validated/adapted MCP tool is registered with native tools in one
   duplicate-safe `ToolRegistry`.
6. Only effective-SAFE MCP metadata is exposed to concrete Planner/Agent; all non-SAFE
   MCP tools remain registered but are not Agent-visible.
7. Every MCP invocation is risk-classified before remote execution.
8. Unknown MCP risk is DANGEROUS and denied.
9. DANGEROUS and unsupported WRITE MCP calls never reach adapter/server.
10. MCP WRITE denial has the approved audit/event/ApprovalPolicy behavior.
11. Validated normalized effective-SAFE MCP metadata reaches the concrete Planner and
    Agent, while WRITE/DANGEROUS/unconfigured tools remain hidden from them.
12. A configured SAFE MCP tool executes through the real Agent/graph/ToolRuntime/event path.
13. MCP failures map to sanitized structured `MCPError` / `ToolResult` failures.
14. Manager resources are deterministically closed.
15. Mocked config/discovery/call/error/security and model-driven integration tests pass.
16. A real pinned Everything Server Agent-path E2E succeeds.
17. `ruff check .`, frozen mypy command, and full pytest suite pass.
18. MCP Guide and reproducible acceptance evidence are committed.
19. No Day 7+ capability is implemented.

---

## 24. Knowledge Review targets

After implementation, the Product Owner must be able to explain at least:

1. Why MCP is a **tool source** behind `ToolRegistry`, rather than a second Agent runtime.
2. Why `MCPToolAdapter` converts protocol-specific data into Nexus-owned `ToolResult`.
3. Why the Agent should not care whether a Tool is Native or MCP.
4. Why an unclassified MCP tool is DANGEROUS instead of SAFE.
5. Why name/description heuristics are insufficient for security classification.
6. Why Day 6 intentionally does not authorize generic MCP WRITE tools.
7. Why connection retry is acceptable but automatic tool-call replay is unsafe without idempotency knowledge.
8. What happens when an MCP server cannot connect, discovery fails, a tool times out, or the manager closes.
9. Why stdio server lifecycle belongs to infrastructure/Composition Root while Agent tool execution still belongs to ToolRuntime.
10. How one actual call travels:

```text
Planner/Agent
→ ToolInvocation("mcp.everything.echo")
→ ToolRuntime
→ CommandPolicy
→ ToolRegistry.resolve
→ MCPToolAdapter
→ MCPManager.call_tool
→ MCP server
→ normalized ToolResult
→ ToolFinished
→ observe/Agent
```

---

## 25. Implementation Engineer stop conditions

Codex MUST stop and ask for an approved contract change if any of the following is encountered:

- the official MCP SDK v2 API cannot support the frozen manager semantics without changing a public Nexus contract;
- the pinned Everything Server no longer exposes the expected deterministic SAFE acceptance tool;
- existing Plan authorization unexpectedly requires generic MCP WRITE support to satisfy a frozen Day 6 acceptance criterion;
- MCP registration requires changing the existing Tool protocol or ToolResult public fields;
- a transport other than stdio becomes necessary;
- the existing Composition Root cannot own MCP lifecycle without changing higher-level architecture;
- an external dependency beyond the official `mcp` SDK is required for core MCP operation;
- any ambiguity would require redefining security policy, graph topology, schema, CLI public contract, or Day 7+ behavior.

Private helpers, exact internal SDK wrapper structure, fixture organization, error wording that preserves frozen codes/semantics, and equivalent low-level implementation details remain within the Implementation Engineer decision boundary.

---

## 26. Frozen owner decisions summary

```text
Reference server:
  everything

Reference package:
  @modelcontextprotocol/server-everything@2026.8.31

Python client SDK:
  mcp>=2,<3

Implemented transport:
  stdio only

MCP TOML:
  [mcp]
  [[mcp.servers]]
  [[mcp.servers.tool_risks]]

MCP server configuration sources:
  user ~/.nexus/config.toml > disabled/empty defaults
  repository .nexus/config.toml [mcp] presence fails ConfigurationError

MCP CLI override:
  none

NEXUS_MCP_SERVERS:
  not defined

mcp_servers precedence:
  one complete user-level field; no server_id merge

Registry namespace:
  mcp.<server_id>.<remote_tool_name>

Risk source:
  explicit configured per-server tool risk map

Unknown MCP risk:
  DANGEROUS

Risk heuristic inference:
  forbidden

SAFE MCP execution:
  allowed through existing ToolRuntime

WRITE MCP execution in Day 6:
  fail closed after classification
  denied audit recorded
  MCP_WRITE_NOT_AUTHORIZED
  no ApprovalRequested
  no ApprovalPolicy call
  no adapter/server call
  AuthorizationScope unchanged

DANGEROUS MCP execution:
  hard denied; never reaches server

ToolRegistry registration:
  every successfully discovered, validated, and adapted MCP tool

Planner/Agent exposure:
  effective resolved SAFE tools only

Registered but not Agent-visible:
  WRITE
  explicitly DANGEROUS
  unconfigured risk (effective DANGEROUS)

Independent exposure allowlist:
  none

Day 6 real E2E:
  production concrete Planner/Agent + graph + ToolRuntime + real stdio MCP server
  direct ToolRuntime-only E2E is insufficient

Tool-call automatic retry:
  forbidden

Connection retry:
  max_connect_attempts, default 2, total max 3
  retry only explicit transient timeout/connection failures
  deterministic and unclassified failures do not restart

MCP transport failure boundary:
  MCPError

Tool model:
  existing Nexus Tool / ToolInvocation / ToolResult

Tool execution ordering:
  sequential

MCP connection persistence:
  none

New database migration:
  none

Nexus as MCP server:
  forbidden
```

---

**End of Nexus Day 6 Contract Addendum v0.3**
