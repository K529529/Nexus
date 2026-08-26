# Nexus Day 1 — Approved Contract Addendum v1.0

**Status:** APPROVED
**Applies To:** Day 1 — Project Bootstrap & Runtime Skeleton
**Authority:** Product Owner / Architect approved implementation contract
**Baseline:** `Nexus V1 Specification v1.1.1 — Implementation Ready Frozen Baseline`

---

## 0. Authority and Scope

This Addendum resolves implementation-contract details intentionally or accidentally left unspecified by the frozen v1.1.1 Specification.

For Day 1 implementation, authoritative sources are now:

1. `AGENTS.md`
2. `docs/Nexus_V1_Product_Requirements_and_10-Day_Engineering_Specification_v1.1.1_中文.md`
3. **This approved Day 1 Contract Addendum**

The v1.1.1 Specification remains the authoritative architecture and product baseline.

This Addendum:

* does **not** redesign Nexus;
* does **not** change the Day 1 scope;
* does **not** authorize Day 2+ implementation;
* only freezes contracts required to implement Day 1 safely.

If this Addendum provides a more precise definition for an item that v1.1.1 left unspecified, **this Addendum is normative for that Day 1 detail**.

If a real conflict with an already explicit frozen v1.1.1 decision is discovered:

`STOP → Report conflict → Explain impact/options → Await Product Owner decision`

Do not silently reinterpret either document.

---

# 1. Day 1 Runtime Contract

## 1.1 `NexusRuntime.run`

Freeze the public Runtime entry point as:

```python
from collections.abc import AsyncIterator

class NexusRuntime:
    def run(
        self,
        task: str,
        session_id: str | None = None,
    ) -> AsyncIterator["RuntimeEvent"]:
        ...
```

Implementation MAY use an `async def` async-generator internally.

The required consumer contract is:

```python
async for event in runtime.run(task, session_id=None):
    ...
```

### Semantics

For Day 1:

1. `task` is a required non-empty user task.
2. `session_id` is accepted as an optional opaque identifier only.
3. Day 1 does **not** persist or resume sessions.
4. `NexusRuntime` generates a unique `run_id` for every invocation.
5. Runtime emits `TaskStarted` before invoking the graph/model path.
6. Successful execution terminates with exactly one `FinalResult`.
7. Handled execution failure terminates with exactly one `ErrorOccurred`.
8. `FinalResult` and `ErrorOccurred` are terminal for the invocation.
9. Runtime must not expose LangGraph or LangChain objects through this interface.

The Day 1 event sequence is therefore:

```text
success:
TaskStarted → FinalResult

failure:
TaskStarted → ErrorOccurred
```

No Day 2+ lifecycle events are implemented early.

---

# 2. Runtime Event Contract

## 2.1 Event representation

For Day 1, `RuntimeEvent` is a **Nexus-owned typed base model with concrete event types**, not a raw dictionary and not a LangGraph event.

The exact implementation mechanism may be `dataclass`, Pydantic model, or an equivalent already-supported typed Python mechanism, provided no new unnecessary dependency is introduced.

The semantic contract is:

```python
RuntimeEvent
├── TaskStarted
├── FinalResult
└── ErrorOccurred
```

Day 1 implements **only these three concrete events**.

Future events defined by the V1 Specification are deferred until their corresponding capabilities exist.

---

## 2.2 Common event fields

Every Day 1 event must expose:

```python
run_id: str
session_id: str | None
timestamp: datetime
status: RuntimeStatus
```

Requirements:

* `run_id` must uniquely identify the Runtime invocation.
* `session_id` preserves the value supplied to `NexusRuntime.run`.
* `timestamp` must be timezone-aware UTC.
* runtime-facing serialization must be stable and structured.
* private chain-of-thought/raw reasoning must never appear.

---

## 2.3 Runtime status

Freeze the Day 1 status values to:

```text
STARTED
COMPLETED
FAILED
```

Mapping:

```text
TaskStarted   → STARTED
FinalResult   → COMPLETED
ErrorOccurred → FAILED
```

Do not add future terminal statuses such as repair/replan/validation limit statuses during Day 1.

---

## 2.4 `TaskStarted`

Semantic payload:

```python
task: str
```

Required meaning:

* Runtime accepted the task.
* A run has started.
* It does **not** mean repository exploration, planning, tool execution, or coding capability has started.

---

## 2.5 `FinalResult`

Semantic payload:

```python
content: str
```

Required meaning:

* contains the final user-visible model response for the minimal Day 1 lifecycle;
* contains no raw model/provider object;
* contains no private reasoning;
* does not claim Day 2+ capabilities.

---

## 2.6 `ErrorOccurred`

Semantic payload:

```python
code: str
message: str
retryable: bool
```

Requirements:

* `code` is a stable Nexus-facing error code/string;
* `message` is safe for user display;
* `retryable` indicates whether retry may reasonably succeed;
* provider stack traces, credentials, raw HTTP authorization data, and private reasoning must not enter the event.

Low-level exception details MAY remain available to developer logging only when safely sanitized.

---

# 3. Nexus-owned Model Message Types

Provider-specific/LangChain message classes must not become the public Nexus contract.

Freeze the minimal Nexus-owned Day 1 message representation as conceptually:

```python
ModelMessage:
    role: Literal["system", "user", "assistant"]
    content: str
```

The concrete model adapter is responsible for converting Nexus-owned messages into LangChain/provider-specific message objects.

Day 1 does not require:

* tool messages;
* tool calls;
* multimodal messages;
* reasoning messages;
* structured-output schemas.

These remain future scope unless explicitly introduced by a later approved Specification.

---

# 4. `ModelGateway` Contract

## 4.1 Responsibility

`ModelGateway` is the only model/provider boundary visible to the Nexus runtime/graph layer.

Concrete OpenAI-compatible/LangChain provider initialization must remain behind the infrastructure adapter.

Application/runtime/agent code must not instantiate provider clients directly.

---

## 4.2 Request and response types

Freeze the minimal Nexus-owned response types conceptually as:

```python
ModelResponse:
    content: str
```

and:

```python
ModelChunk:
    content: str
```

No raw LangChain/provider response object may cross the gateway boundary.

Provider metadata may be added internally but is **not** required as part of the Day 1 public contract.

---

## 4.3 `complete`

Freeze the semantic signature as:

```python
async def complete(
    self,
    messages: Sequence[ModelMessage],
) -> ModelResponse:
    ...
```

Semantics:

* performs one non-streaming logical model completion;
* returns normalized Nexus-owned `ModelResponse`;
* provider/config/network/model failures must be mapped to `ModelError`;
* empty model output must be handled explicitly rather than leaking provider behavior.

---

## 4.4 `stream`

Freeze the semantic signature as:

```python
def stream(
    self,
    messages: Sequence[ModelMessage],
) -> AsyncIterator[ModelChunk]:
    ...
```

An implementation may use an async generator.

Semantics:

```python
async for chunk in gateway.stream(messages):
    ...
```

Each chunk:

* contains only normalized user-visible model content;
* must not expose provider objects;
* must not expose reasoning/chain-of-thought;
* may contain an empty content fragment only when required by the underlying provider, but consumers must not depend on empty fragments.

Day 1 must implement both `complete` and `stream`.

The minimal graph may choose the simpler path necessary to satisfy Day 1 acceptance, but both gateway contracts must be implemented and tested at the appropriate level.

---

# 5. `GraphRuntime` Contract

`GraphRuntime` is a Nexus-owned port.

Freeze the Day 1 semantic contract as:

```python
class GraphRuntime:
    async def run(
        self,
        state: AgentState,
    ) -> AgentState:
        ...
```

### Responsibilities

`GraphRuntime`:

* receives Nexus-owned `AgentState`;
* performs the minimal Day 1 model-backed graph execution;
* returns updated Nexus-owned `AgentState`;
* hides LangGraph implementation details completely.

It must not expose:

* `StateGraph`;
* LangGraph compiled graph objects;
* LangGraph checkpoint types;
* LangChain messages;
* provider response types.

---

# 6. Minimal Day 1 Graph

The Day 1 LangGraph implementation must remain deliberately minimal.

Conceptual topology:

```text
START
  ↓
model_response
  ↓
END
```

The `model_response` node:

1. reads the task/messages from Nexus-owned `AgentState`;
2. invokes `ModelGateway`;
3. stores the normalized assistant message/result in Nexus-owned state;
4. updates status appropriately;
5. returns control to the graph.

This topology is **Day 1 bootstrap behavior only**.

It must not implement or simulate the full normative V1 coding-agent topology.

Specifically, Day 1 must not create placeholder graph nodes pretending to perform:

* repository exploration;
* context building;
* planning;
* approval;
* tool execution;
* observation;
* replan;
* validation;
* repair.

Those capabilities belong to later Days.

---

# 7. Minimal `AgentState` Contract

Freeze Day 1 `AgentState` to exactly the semantic fields required by the Specification:

```python
AgentState:
    task: str
    messages: list[ModelMessage]
    run_id: str
    session_id: str | None
    status: RuntimeStatus
```

No Day 2+ state fields may be added.

In particular, do not add:

* plan;
* observations;
* tool_results;
* approvals;
* checkpoint IDs;
* validation result;
* repository context;
* retrieval context;
* counters for future graph lifecycle;
* skill/MCP state.

Internal private helper state is allowed only when it does not become a new public/domain contract or prematurely model future capabilities.

---

# 8. `RuntimeConfig` Contract

## 8.1 Day 1 fields

Freeze the Day 1 runtime configuration to the following semantic fields:

```python
RuntimeConfig:
    model_provider: str
    model_name: str | None
    model_base_url: str | None
    model_api_key: secret | None
    database_url: str
```

No Day 2+ configuration fields are required during Day 1.

---

## 8.2 Defaults

Freeze defaults as:

```text
model_provider = "openai_compatible"
model_name = None
model_base_url = None
model_api_key = None
database_url = "postgresql+asyncpg://nexus:nexus@localhost:5432/nexus"
```

`model_name=None` means no model has been explicitly configured.

The `chat` execution path must fail with a clear `ConfigurationError` if the configuration is insufficient to construct/use the configured model.

Do not silently invent a product-default model name.

---

# 9. Configuration Sources and Precedence

For non-secret Day 1 configuration, preserve the frozen precedence exactly:

```text
CLI arguments
>
repo .nexus/config.toml
>
user ~/.nexus/config.toml
>
environment variables
>
defaults
```

Precedence applies **per field**, not per entire source.

Example:

```text
CLI model_name
+
repo model_base_url
+
environment database_url
```

may coexist in one resolved `RuntimeConfig`.

A higher-priority source only overrides fields it actually supplies.

---

# 10. Environment Variable Contract

Freeze the following Day 1 environment variables:

```text
NEXUS_MODEL_PROVIDER
NEXUS_MODEL_NAME
NEXUS_MODEL_BASE_URL
NEXUS_MODEL_API_KEY
NEXUS_DATABASE_URL
```

No alternative aliases are required.

---

# 11. TOML Contract

For Day 1, repository/user TOML configuration uses:

```toml
[model]
provider = "openai_compatible"
name = "example-model"
base_url = "https://example.invalid/v1"

[database]
url = "postgresql+asyncpg://..."
```

All fields are optional.

Missing fields fall through to the next lower-precedence source.

---

# 12. Secret Handling

`model_api_key` is a secret.

Freeze the Day 1 policy:

* `NEXUS_MODEL_API_KEY` is the supported Day 1 source for the model API key.
* Do not expose a CLI `--api-key` option.
* Do not support `api_key` in repository `.nexus/config.toml`.
* Do not print the API key.
* Do not include it in `RuntimeEvent`.
* Do not include it in normal logs.
* `RuntimeConfig.__repr__` / debug rendering must redact it if the chosen implementation exposes configuration rendering.

This intentionally narrows the generic precedence rule for this secret in order to satisfy the frozen requirement that secrets not be stored in commit-capable repository configuration.

If future secure credential stores are added, that requires a later approved contract.

`database_url` must also be redacted from logs when it contains credentials.

---

# 13. Day 1 CLI Contract

Freeze the Day 1 public CLI surface to:

```bash
nexus
nexus --help
nexus chat TASK
```

Day 2+ commands are not implemented early.

---

## 13.1 Root behavior

For Day 1:

```bash
nexus
```

shows help/usage and exits successfully.

It does **not** start an interactive session during Day 1.

This is an intentionally minimal Day 1 behavior and does not remove the future V1 ability for the root command to become an interactive entry point after session/runtime capability exists.

---

## 13.2 Chat command

Freeze:

```bash
nexus chat TASK
```

where `TASK` is a required positional string.

Required acceptance example:

```bash
nexus chat "Reply with a short greeting."
```

Day 1 CLI-level model overrides:

```text
--model TEXT
--base-url TEXT
```

These map to:

```text
--model    → RuntimeConfig.model_name
--base-url → RuntimeConfig.model_base_url
```

Do not expose:

```text
--api-key
```

Do not add Day 2+ CLI commands during Day 1.

The CLI must remain an adapter:

```text
parse CLI
→ resolve CLI overrides
→ invoke Composition Root
→ call NexusRuntime
→ render RuntimeEvents
```

No graph/model/database orchestration belongs in the Typer handler.

---

# 14. Configuration Error Contract

Add the following approved Day 1 error subtype:

```python
ConfigurationError(NexusError)
```

This resolves the frozen Specification wording that configuration failures must map to a configuration error while the original global hierarchy did not name a concrete subtype.

Use:

```text
ConfigurationError
```

for failures such as:

* missing required model configuration;
* invalid configuration values;
* malformed Nexus TOML;
* unsupported Day 1 provider value;
* invalid resolved database configuration where detected during configuration/bootstrap.

Use:

```text
ModelError
```

for failures occurring after crossing the model gateway boundary, including:

* provider initialization failures attributable to model/provider access;
* model API failures;
* model transport failures;
* normalized provider errors.

`ConfigurationError` must not expose secrets.

No additional error hierarchy redesign is authorized.

---

# 15. OpenAI-compatible Provider Contract

Day 1 supports exactly the configured provider family required by the frozen Specification:

```text
model_provider = "openai_compatible"
```

Provider-specific implementation remains behind `ModelGateway`.

The following must be configurable:

```text
model name
base URL
API key
```

This is intended to support OpenAI-compatible endpoints without coupling Nexus application/domain code to a concrete vendor.

Do not introduce a provider registry/framework during Day 1.

Do not add multiple first-class vendor implementations speculatively.

A small infrastructure adapter/factory branch necessary to construct the approved OpenAI-compatible implementation is sufficient.

---

# 16. PostgreSQL Day 1 Development Infrastructure

Day 1 **shall provide an optional Docker Compose PostgreSQL development service** for reproducible local setup.

This Compose service is development infrastructure only.

It is **not**:

* DockerSandbox;
* RemoteSandbox;
* production deployment infrastructure.

Use PostgreSQL 16-compatible development infrastructure.

It is acceptable and preferred for the development database image to be compatible with the frozen later V1 `pgvector` requirement, but Day 1 must not implement:

* semantic retrieval;
* vector schema;
* embedding;
* indexing.

The default local development connection contract is:

```text
postgresql+asyncpg://nexus:nexus@localhost:5432/nexus
```

The Compose credentials are local development defaults, not production credentials.

Users may override the connection using:

```text
NEXUS_DATABASE_URL
```

Day 1 database work is limited to:

* engine/bootstrap wiring;
* connectivity verification;
* development startup documentation;
* allowed migration/bootstrap scaffold.

Do not create repository/session/run/approval/retrieval business tables early.

---

# 17. Composition Root Contract

Concrete Day 1 dependencies must be assembled only under:

```text
src/nexus/infrastructure/bootstrap/
```

The Day 1 Composition Root assembles only:

```text
resolved RuntimeConfig
→ database bootstrap dependency required for Day 1
→ concrete ModelGateway
→ concrete LangGraph-backed GraphRuntime
→ NexusRuntime
```

It must not construct future dependencies such as:

* business repositories;
* CheckpointProvider;
* ApprovalPolicy;
* SandboxExecutor;
* ToolRegistry;
* Context Provider;
* SemanticSearchProvider;
* Skill services;
* MCPManager;
* full Tracer subsystem.

Those remain deferred.

---

# 18. Day 1 Runtime Flow — Frozen Refinement

The approved Day 1 runtime lifecycle is:

```text
Typer CLI
    ↓
resolve RuntimeConfig
    ↓
Composition Root
    ↓
NexusRuntime.run(task, session_id=None)
    ↓
generate run_id
    ↓
TaskStarted
    ↓
construct minimal AgentState
    ↓
GraphRuntime.run(state)
    ↓
LangGraph minimal model_response node
    ↓
ModelGateway
    ↓
configured OpenAI-compatible model
    ↓
normalized assistant response
    ↓
updated AgentState
    ↓
FinalResult
```

Failure path:

```text
TaskStarted
    ↓
configuration / graph / model failure
    ↓
safe normalized Nexus error
    ↓
ErrorOccurred
```

No future lifecycle stage may be simulated.

---

# 19. Day 1 Test Contract Refinement

The frozen Specification's required tests remain mandatory.

At minimum verify:

### Configuration precedence

Demonstrate per-field precedence:

```text
CLI
>
repo TOML
>
user TOML
>
environment
>
defaults
```

Also verify API key redaction / secret handling where applicable.

### Runtime event ordering

Success:

```text
TaskStarted
FinalResult
```

Failure:

```text
TaskStarted
ErrorOccurred
```

### Mocked `ModelGateway` integration

Verify:

```text
NexusRuntime
→ GraphRuntime
→ mocked ModelGateway
→ normalized FinalResult
```

without real provider credentials.

### CLI smoke

At minimum:

```bash
nexus --help
nexus
nexus chat --help
```

and a mocked/configured chat path as appropriate.

### PostgreSQL connectivity smoke

Verify the configured async PostgreSQL bootstrap can establish and close a connection.

This test may be marked/skipped only when the required external PostgreSQL service is genuinely unavailable, and the final report must state that blocker explicitly.

### Quality gates

Execute the repository-configured equivalents of:

```bash
ruff check .
mypy ...
pytest
```

GitHub Actions must execute the required quality checks.

Do not fabricate a live model acceptance result if credentials/network are unavailable.

---

# 20. Acceptance Semantics

The Day 1 acceptance command remains:

```bash
nexus chat "Reply with a short greeting."
```

With valid OpenAI-compatible configuration, expected externally observable behavior is conceptually:

```text
Task started
<short model-generated greeting>
```

The exact renderer wording/style is a local implementation detail.

The underlying structured event stream must contain:

```text
TaskStarted
FinalResult
```

A model/configuration failure must produce a safe structured error path rather than a false success.

Day 1 must not claim that Nexus can:

* understand a repository;
* search code;
* generate a Plan;
* call Tools;
* edit code;
* validate changes;
* resume sessions;
* retrieve semantic context;
* use MCP;
* act as a complete coding agent.

---

# 21. Implementation Freedom Remaining to Codex

The following remain inside the approved Codex Implementation Decision Boundary:

* exact private helper names;
* exact module-internal function names;
* `dataclass` vs an already-available equivalent typed representation where it does not alter the semantic contract;
* test fixture implementation details;
* internal adapter helpers;
* safe user-facing error wording;
* exact CLI renderer presentation;
* exact PostgreSQL Compose image/tag compatible with the requirements above;
* exact LangGraph adapter implementation necessary to realize the approved minimal graph;
* clearly equivalent low-level implementation choices.

Codex may **not** alter the semantic contracts frozen above.

---

# 22. Instructions to Resume Day 1

This Addendum is approved.

You no longer need to stop on the previously reported contract ambiguities.

Proceed as follows:

1. Treat this Addendum as the approved Day 1 normative contract.
2. Preserve `AGENTS.md` and v1.1.1 as the higher-level frozen architecture/product baseline.
3. If repository traceability is desired, create:

```text
docs/spec-addenda/Nexus_Day1_Contract_Addendum_v1.0.md
```

containing this approved contract.

Creating that documentation file is explicitly authorized and is not considered an unauthorized Specification redesign.

4. Re-inspect the repository if necessary.
5. Produce the concise Day 1 implementation plan required by the original task.
6. Implement Day 1 only.
7. Run all required verification.
8. Produce the required Final Report.
9. Do not commit, push, merge, rebase, or modify Git history.

If a **new** conflict outside this Addendum is discovered and it crosses the Codex Implementation Decision Boundary:

`STOP → Report evidence → Explain impact/options → Await decision`
