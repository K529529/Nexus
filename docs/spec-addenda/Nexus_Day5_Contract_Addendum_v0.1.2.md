# Nexus Day 5 Contract Addendum v0.1.2

**Addendum Version:** `v0.1.2`
**Applies To:** `Nexus V1 Product Requirements & 10-Day Engineering Specification v1.1.1`
**Milestone:** Day 5 — Context Engineering & Hybrid Retrieval
**Implementation Branch:** `feature/day05-context-hybrid-retrieval`
**Status:** APPROVED
**Authority:** Product Owner / Architect approved implementation contract

This consolidated revision supersedes and replaces the former Day 5 Addendum v0.1.0,
Supplementary Decisions v0.1.1, SD-5-08, and SD-5-08A. It is the sole Day 5
implementation contract. The consolidation changes no approved decision semantics.

---

# 1. Authority and Scope

This Addendum freezes the implementation contract required for Day 5.

The parent Specification v1.1.1 remains authoritative for all architecture, security, graph topology, persistence ownership, configuration precedence, tooling, and milestone boundaries not explicitly refined here.

If this Addendum conflicts with a less-specific Day 5 statement in the parent Specification, this Addendum controls for Day 5 implementation.

This Addendum does **not** authorize:

* modification of the normative Agent Graph topology;
* Qdrant or Milvus;
* AST/LSP-aware chunking;
* automatic filesystem/index watcher;
* MCP implementation;
* Skill selection implementation;
* long-term memory;
* new Agent graph nodes;
* automatic full-repository reindex after every Agent edit;
* HNSW/IVFFlat tuning or ANN-specific architecture.

Codex remains the **Implementation Engineer**, not the Architect.

If implementation requires changing a public contract frozen below, Codex must:

```text
STOP
→ Report conflict
→ Show concrete evidence
→ Explain impact/options
→ Await approved specification change
```

---

# 2. Day 5 Architectural Boundary

The normative graph remains:

```text
initialize_run
    ↓
explore_repository
    ↓
build_context
    ↓
create_plan
    ↓
...
```

Day 5 does not add retrieval nodes to the graph.

Responsibilities are frozen as follows.

## 2.1 `explore_repository`

`explore_repository` remains repository discovery.

It is responsible for information such as:

* repository instructions;
* root/nested applicable `AGENTS.md`;
* manifests;
* top-level repository layout;
* repository/project type;
* deterministic repository evidence;
* likely task-relevant paths/search hints.

It does **not** own:

* embedding;
* pgvector querying;
* RRF;
* context budget;
* conversation compaction;
* semantic ranking.

## 2.2 `build_context`

`build_context` is the primary Day 5 integration point.

Its effective flow becomes:

```text
task + exploration
        ↓
ContextManager
        ↓
Lexical Retrieval
        +
Semantic Retrieval
        ↓
Normalize Candidates
        ↓
RRF Merge
        ↓
Budget / Selection
        ↓
WorkingContext
```

`build_context` must call Context abstractions.

It must not directly depend on PostgreSQL/pgvector, ripgrep process details, or concrete embedding vendor clients.

## 2.3 Agent-step context refresh

Day 5 also applies to prompt/context assembly performed during later `agent_step` cycles.

No new graph node is introduced.

Before each model-driven `agent_step`, the existing Agent implementation may request a bounded model-input view from `ContextManager`.

This view may include:

* initial Working Context;
* current Plan;
* recent Tool observations;
* compacted older observations;
* current task;
* repository instructions.

This integration must not change graph edges or step-count semantics.

---

# 3. Frozen Day 5 Core Types

The following contracts are public Day 5 contracts.

Exact source file placement may follow the existing repository structure, but Codex may not materially change their fields or semantics.

```python
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, Sequence


class RetrievalSource(StrEnum):
    LEXICAL = "LEXICAL"
    SEMANTIC = "SEMANTIC"


@dataclass(frozen=True, slots=True)
class CodeChunk:
    file_path: str
    language: str
    symbol: str | None
    start_line: int
    end_line: int
    content: str
    content_hash: str
    file_hash: str


@dataclass(frozen=True, slots=True)
class ContextCandidate:
    chunk: CodeChunk
    lexical_rank: int | None
    semantic_rank: int | None
    semantic_score: float | None
    rrf_score: float
    sources: tuple[RetrievalSource, ...]


@dataclass(frozen=True, slots=True)
class ContextBudget:
    max_retrieved_chunks: int
    max_exploration_seed_chunks: int
    max_code_context_tokens: int
    max_recent_observations: int


@dataclass(frozen=True, slots=True)
class RetrievalQuery:
    repository_id: str
    workspace_path: str
    text: str
    limit: int


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    candidates: tuple[ContextCandidate, ...]
    semantic_used: bool
    semantic_status: str | None
```

Invariants:

* `file_path` is repository-relative and normalized with `/`.
* `start_line` and `end_line` are 1-based inclusive.
* `start_line <= end_line`.
* `content_hash` is SHA-256 of normalized chunk content encoded as UTF-8.
* `file_hash` is SHA-256 of the full indexed file bytes/content after the selected normalized text-decoding policy.
* ranks are 1-based.
* lexical-only result has `semantic_rank=None`.
* semantic-only result has `lexical_rank=None`.
* `sources` must accurately identify how the candidate was discovered.
* no raw private reasoning is stored in these values.

---

# 4. Existing Day 4 WorkingContext Compatibility

Day 5 must preserve existing Day 4 consumers of `WorkingContext`.

Existing fields must not be removed or silently change meaning.

If the current implementation follows the approved Day 4 shape:

```python
@dataclass(frozen=True, slots=True)
class WorkingContext:
    task: str
    repository_instructions: tuple[RepositoryInstruction, ...]
    manifest_summaries: tuple[RepositoryFileEvidence, ...]
    top_level_paths: tuple[str, ...]
    selected_files: tuple[SelectedFileContext, ...]
    truncated: bool
```

Day 5 must extend it source-compatibly rather than replacing it with an unrelated model.

Approved additive fields are:

```python
@dataclass(frozen=True, slots=True)
class WorkingContext:
    task: str
    repository_instructions: tuple[RepositoryInstruction, ...]
    manifest_summaries: tuple[RepositoryFileEvidence, ...]
    top_level_paths: tuple[str, ...]
    selected_files: tuple[SelectedFileContext, ...]
    truncated: bool

    retrieved_candidates: tuple[ContextCandidate, ...] = ()
    compacted_conversation: str | None = None
    semantic_retrieval_used: bool = False
    semantic_retrieval_status: str | None = None
    recent_observations: tuple[Observation, ...] = ()
    recent_conversation_turns: tuple[SessionTurn, ...] = ()
    compacted_observations: str | None = None
```

Rules:

1. `selected_files` remains the actual bounded code text selected for model use.
2. `retrieved_candidates` records retrieval/ranking evidence and metadata.
3. Context retrieval must not cause the complete repository to be copied into `selected_files`.
4. Day 5 must not implement Day 7 Skill selection.
5. Any future Skill-related field remains empty/unresolved until Day 7.
6. `AgentState` remains the complete authoritative graph-execution state.
7. `ContextManager` exclusively owns model-visible observation/history selection and
   compaction; Agent/model adapters must not apply a second independent slicing policy.

If the actual Day 4 `WorkingContext` contract differs from the shape above, Codex must preserve the approved Day 4 public fields and apply the same **additive-only** principle.

Codex must not redesign `WorkingContext` without approval.

---

# 5. Context Provider Contracts

## 5.1 LexicalSearchProvider

```python
class LexicalSearchProvider(Protocol):
    async def search(
        self,
        query: RetrievalQuery,
    ) -> tuple[ContextCandidate, ...]:
        ...
```

The V1 concrete implementation is ripgrep/equivalent lexical retrieval.

The provider must not expose subprocess/ripgrep details to Agent code.

Where the existing Tool Runtime already owns repository lexical search, the concrete lexical provider must reuse that policy-safe path rather than bypassing Tool Runtime with arbitrary subprocess execution.

Lexical retrieval remains subject to workspace containment and existing Tool/Sandbox policy.

---

## 5.2 SemanticSearchProvider

```python
class SemanticSearchProvider(Protocol):
    async def search(
        self,
        query: RetrievalQuery,
    ) -> tuple[ContextCandidate, ...]:
        ...

    async def validate_index(
        self,
        repository_id: str,
    ) -> "IndexCompatibility":
        ...
```

Concrete Day 5 adapter:

```text
PgVectorSemanticSearchProvider
```

Agent Runtime and graph nodes must not import or depend directly on pgvector-specific implementation classes.

---

## 5.3 ContextProvider

```python
@dataclass(frozen=True, slots=True)
class ContextRequest:
    task: str
    repository_id: str
    workspace_path: str
    exploration: ExplorationResult
    run_id: str
    session_id: str


class ContextProvider(Protocol):
    async def retrieve(
        self,
        request: ContextRequest,
    ) -> RetrievalResult:
        ...
```

`ContextProvider` represents retrieval capability.

It does not own graph transitions.

---

## 5.4 ContextManager

```python
class ContextManager(Protocol):
    async def build(
        self,
        request: ContextRequest,
    ) -> WorkingContext:
        ...

    async def prepare_agent_context(
        self,
        *,
        working_context: WorkingContext,
        plan: Plan | None,
        observations: Sequence[Observation],
        conversation_turns: Sequence[SessionTurn],
    ) -> WorkingContext:
        ...
```

Responsibilities:

```text
retrieve
→ merge
→ budget
→ selection
→ observation retention
→ conversation/observation compaction
→ WorkingContext
```

`ContextManager` owns context assembly policy.

It does not own:

* Graph routing;
* Plan creation;
* Approval;
* Tool execution;
* Validation;
* persistence transaction orchestration outside its own retrieval/index adapters.

---

# 6. Chunker Contract

```python
class Chunker(Protocol):
    def chunk(
        self,
        *,
        file_path: str,
        language: str,
        content: str,
        file_hash: str,
    ) -> tuple[CodeChunk, ...]:
        ...
```

V1 implementation is frozen:

```text
strategy = line_window
chunk_size_lines = 120
chunk_overlap_lines = 20
step_lines = 100
chunk_strategy_version = "line_window_v1"
```

Chunk starts are therefore:

```text
1
101
201
301
...
```

Each chunk contains at most 120 lines.

For files of 120 lines or fewer:

```text
exactly one chunk
```

The final chunk may contain fewer than 120 lines.

No empty chunk may be persisted.

`symbol` is `None` in the V1 language-agnostic chunker unless symbol metadata is already deterministically available without introducing AST/LSP parsing.

Codex must not add AST/LSP parsing in Day 5.

---

# 7. File Filtering Contract

Repository indexing must:

1. respect `.gitignore`;
2. exclude binary files;
3. exclude unsupported undecodable files;
4. exclude files larger than configured maximum size;
5. skip generated/cache/dependency directories.
6. keep applicable `AGENTS.md` files out of ordinary lexical/semantic code candidates;
   they are loaded separately as scoped repository instructions.

Frozen default excluded directories:

```text
.git
node_modules
.venv
venv
__pycache__
dist
build
target
coverage
.pytest_cache
.mypy_cache
.ruff_cache
```

Default:

```text
max_file_size_bytes = 1_048_576
```

The value is configurable.

A skipped file must not crash indexing.

Unsupported/skipped-file counts may be reported in the index result.

No recursive complete-repository content may be injected into an LLM prompt.

---

# 8. Lexical Query Derivation Baseline

Codex must not invent an LLM-based lexical query planner in Day 5.

V1 lexical retrieval uses deterministic query extraction from the task.

The extractor uses, in order:

1. quoted strings;
2. path-like tokens;
3. identifier-like tokens;
4. alphanumeric tokens of length >= 3.

Duplicate terms are removed while preserving first occurrence.

Identifier-like baseline:

```text
[A-Za-z_][A-Za-z0-9_.:/\\-]*
```

Tokens consisting only of common natural-language stop words may be removed by a small static internal set.

No ModelGateway call is required for lexical query generation.

If no useful lexical token is produced:

```text
lexical result = empty
```

Semantic retrieval may still proceed when enabled.

Lexical retrieval must remain capable of finding an exact code symbol/string present in the task.

---

# 9. Lexical Candidate Normalization

Lexical search returns exact file/line matches.

For RRF interoperability with semantic chunks, lexical matches are normalized to the same line-window convention.

For a lexical match on 1-based line `L`:

```text
if file_line_count <= 120 and file_line_count > 0:
    window_start = 1
    window_end = file_line_count

otherwise:
window_start = 1 + floor((L - 1) / 100) * 100
window_end = min(window_start + 119, file_line_count)
```

Empty files produce no chunks and are not persisted.

Multiple lexical hits that normalize to the same:

```text
file_path + start_line + end_line + content_hash
```

produce one lexical candidate.

Lexical candidate ranking follows the stable provider result order.

Rank is 1-based.

---

# 10. Embedding Gateway Contract

Day 5 introduces the approved abstraction:

```python
@dataclass(frozen=True, slots=True)
class EmbeddingConfig:
    provider: str
    model: str
    dimension: int


class EmbeddingGateway(Protocol):
    @property
    def config(self) -> EmbeddingConfig:
        ...

    async def embed_documents(
        self,
        texts: Sequence[str],
    ) -> tuple[tuple[float, ...], ...]:
        ...

    async def embed_query(
        self,
        text: str,
    ) -> tuple[float, ...]:
        ...
```

Invariants:

* returned embedding count equals input document count;
* every vector dimension equals configured `dimension`;
* query vector dimension equals configured `dimension`;
* embedding provider/model details remain behind the gateway;
* Agent/domain code must not directly instantiate vendor embedding clients.

Embedding API credentials follow existing secret/config policy and must not be committed into repository TOML.

The initial Day 5 implementation supports only `provider="openai_compatible"`.
Embedding configuration is independent from the chat `ModelGateway`:

```toml
[embedding]
provider = "openai_compatible"
model = ""
dimension = 0
base_url = ""
```

`NEXUS_EMBEDDING_API_KEY` is the only supported secret source. Environment configuration
may additionally provide `NEXUS_EMBEDDING_MODEL`, `NEXUS_EMBEDDING_DIMENSION`, and
`NEXUS_EMBEDDING_BASE_URL`. With `semantic_enabled=false`, embedding configuration is not
required. With semantic retrieval or explicit indexing enabled, provider/model/dimension/
base URL/API key must be valid and dimension must be greater than zero. No concrete model
name is frozen, and `nexus index` must not depend on the chat/completion model.

---

# 11. Semantic Search Baseline

Frozen settings:

```text
semantic_top_k = 20
metric = cosine
```

The provider executes cosine-distance/similarity search using pgvector.

Ordering must represent the best semantic match first.

The provider must return 1-based semantic ranks.

Raw semantic score/distance may be retained as diagnostic metadata, but:

**raw semantic score must not be numerically combined with lexical score to produce final hybrid ranking.**

Hybrid ranking is exclusively RRF in V1.

---

# 12. Hybrid Merge — Exact RRF Contract

V1 hybrid merge is frozen to Reciprocal Rank Fusion.

```text
rrf_k = 60
```

For candidate `d`:

```text
RRF(d) =
    lexical contribution
    +
    semantic contribution
```

Where:

```text
contribution(rank) = 1 / (60 + rank)
```

A missing source contributes zero.

Example:

```text
lexical rank = 2
semantic rank = 5

score =
1 / (60 + 2)
+
1 / (60 + 5)
```

Candidate identity for deduplication is:

```text
file_path
+ start_line
+ end_line
+ content_hash
```

Final stable ordering:

1. `rrf_score` descending;
2. best available rank ascending:

```text
min(lexical_rank, semantic_rank)
```

3. `file_path` ascending;
4. `start_line` ascending;
5. `content_hash` ascending.

Frozen retrieval limits:

```text
lexical_top_k = 20
semantic_top_k = 20
final_candidate_count = 12
max_retrieved_chunks = 12
max_exploration_seed_chunks = 6
```

No weighted-score fusion.

No custom reranker.

No model-based reranker.

---

# 13. Code and Overall Model-Input Budget Contract

Frozen defaults and absolute ceilings are:

```text
max_retrieved_chunks = 12
max_exploration_seed_chunks = 6
max_code_context_tokens = 12000
max_recent_observations = 8
max_model_input_tokens = 24000
```

These five limits may be configured downward only. `max_model_input_tokens` must be
positive, and `max_code_context_tokens <= max_model_input_tokens`. When provider/model
input capacity is known, the configured total ceiling must not exceed it; otherwise the
configured Nexus ceiling is authoritative. A future approved contract is required to
raise any ceiling.

The code ceiling applies only to selected code. Budget enforcement occurs after hybrid
ranking, in final RRF order. A candidate is selected only if it keeps both code ceilings;
an overflowing chunk is skipped whole rather than partially truncated. Later smaller
candidates may still fit. Omitted relevant candidates set `WorkingContext.truncated=True`.

The total ceiling applies to the complete serialized input for every model-driven call,
including system/safety instructions, repository instructions, current task, current Plan,
selected code, manifest/layout evidence, recent and compacted conversation, recent and
compacted observations, and other model-visible structured context. It excludes model
output tokens, non-injected database/index metadata, telemetry, and unselected AgentState.
Provider framing overhead may be estimated conservatively.

An already-approved provider tokenizer may be used. Otherwise the deterministic policy is:

```text
estimated_tokens = ceil(character_count / 4)
```

The same estimator is used throughout one context-build operation. No tokenizer dependency
is authorized solely for Day 5.

Mandatory context must never be silently removed merely to fit: Nexus system/safety and
relevant frozen runtime constraints, applicable repository instructions, the current
explicit task, the current Plan during coding execution, and the minimum structurally valid
request. If mandatory context cannot fit, `ContextManager` raises `ContextError` with
`code="CONTEXT_BUILD_FAILED"` and a safe message explaining that authoritative context
exceeds the configured budget.

When total input exceeds the ceiling, the sole normative reduction order is:

```text
1. remove lowest-priority retrieved code; high-confidence explicit/task seeds last
2. compact/reduce older observations, preserving critical factual evidence
3. compact/reduce conversation history, preserving the current task
4. reduce non-authoritative manifest/layout evidence
5. reduce remaining optional context by relevance and recency
6. fail safely if mandatory context still cannot fit
```

Any authority/importance list is descriptive only and must not override this eviction order.
In particular, retrieved code is reduced before recent conversation when only one can fit.

All total-input enforcement belongs to `ContextManager`. Agent/model adapters may provide a
pure representation callback so the manager can count the actual final request, but may not
independently slice messages, observations, repository instructions, code, or conversation.
`prepare_agent_context()` must return a compliant representation before `ModelGateway` is
called. The gateway may defensively reject a known hard provider limit but must not silently
truncate.

---

# 14. Observation Retention and Compaction

`AgentState` retains complete current execution observations. Model context retains at most
the configured ceiling of eight recent observations. Older observations, or additional
observations removed under total-budget pressure, are compacted into safe factual evidence:
tool, relevant path, status, important result, error, changed assumption, or validation fact.
Private chain-of-thought is neither requested nor retained. Deterministic factual compaction
is permitted; an LLM compactor must use `ModelGateway` and increment `llm_call_count`.

---

# 15. Conversation History Policy

Day 5 must not inject all `session_turns`. `ContextManager` may obtain durable turns through
the existing Session/Turn service boundary and retains the latest six user-visible turns by
default. Older relevant turns may be summarized into `compacted_conversation`. Compaction
preserves factual task/session context, not private reasoning. Day 5 does not implement a
general long-term memory subsystem.

---

# 16. Repository Indexer Contract

```python
@dataclass(frozen=True, slots=True)
class IndexRequest:
    repository_id: str
    workspace_path: str
    rebuild: bool


@dataclass(frozen=True, slots=True)
class IndexResult:
    repository_id: str
    scanned_files: int
    indexed_files: int
    unchanged_files: int
    removed_files: int
    skipped_files: int
    chunk_count: int
    rebuilt: bool


class RepositoryIndexer(Protocol):
    async def index(
        self,
        request: IndexRequest,
    ) -> IndexResult:
        ...
```

Index lifecycle:

```text
scan
→ filter
→ file hash
→ detect unchanged/changed/new/removed files
→ chunk changed/new files
→ embed changed/new chunks
→ persist
→ delete removed-file chunks
→ update index metadata
```

Standalone indexing is not an Agent Tool invocation and must not create synthetic
`run_id`, `session_id`, `ToolInvocation`, `ApprovalRequest`, Session, or Run values.
`RepositoryIndexer` may scan read-only through a dedicated infrastructure filesystem
boundary guarded by existing workspace containment and Day5 filtering. It may read source
and `.gitignore`, compute hashes, and persist Nexus-owned semantic records; it may not
modify repository source. Coding-agent retrieval/read operations continue through the
existing policy-safe Tool Runtime path. The indexing file-size limit does not widen the
Day3 Tool read limit.

---

# 17. Incremental Reindex Contract

Default:

```bash
nexus index
```

means:

```text
incremental index/update
```

It is not equivalent to full rebuild.

For each allowed file:

```text
current file_hash
vs
stored file_hash
```

If equal:

```text
do not re-embed
```

If changed:

```text
delete old chunks for file
→ chunk current file
→ embed new chunks
→ insert new chunks
```

If new:

```text
chunk
→ embed
→ insert
```

If previously indexed file no longer exists or is now ignored:

```text
delete its stored chunks
```

The changed-file replacement must occur transactionally per repository index operation.

A failed embedding/index update must not leave a silently partially-valid index advertised as current.

No automatic repository watcher is implemented.

No automatic graph-triggered reindex after every patch is required in Day 5.

---

# 18. Full Rebuild Contract

Exact public CLI behavior:

```bash
nexus index --rebuild
```

means:

```text
explicit destructive rebuild of the Nexus semantic index
for the current repository only
```

It may delete/recreate semantic index records owned by Nexus for that repository.

It must not modify repository source files.

It must not modify LangGraph checkpoint tables.

It must not modify Nexus business Session/Run/Approval data.

Rebuild uses the current embedding and chunk configuration.

Successful rebuild replaces stored compatibility metadata with current metadata.

---

# 19. PostgreSQL / pgvector Persistence Contract

Semantic retrieval persistence remains logically separate from:

* Nexus business persistence;
* LangGraph checkpointer persistence.

Day 5 adds the following approved tables.

## 19.1 `repository_semantic_indexes`

Required columns:

```text
repository_id UUID PRIMARY KEY
embedding_provider TEXT NOT NULL
embedding_model TEXT NOT NULL
embedding_dimension INTEGER NOT NULL
index_version TEXT NOT NULL
chunk_strategy_version TEXT NOT NULL
indexed_at TIMESTAMPTZ NOT NULL
```

Frozen values:

```text
index_version = "v1"
chunk_strategy_version = "line_window_v1"
```

`repository_id` references the existing Nexus repository identity according to the current persistence model.

---

## 19.2 `semantic_code_chunks`

Required columns:

```text
id UUID PRIMARY KEY
repository_id UUID NOT NULL
file_path TEXT NOT NULL
language TEXT NOT NULL
symbol TEXT NULL
start_line INTEGER NOT NULL
end_line INTEGER NOT NULL
content TEXT NOT NULL
content_hash TEXT NOT NULL
file_hash TEXT NOT NULL
embedding VECTOR NOT NULL
created_at TIMESTAMPTZ NOT NULL
updated_at TIMESTAMPTZ NOT NULL
```

Required constraints:

```text
start_line >= 1
end_line >= start_line
```

Required unique key:

```text
(repository_id, file_path, start_line, end_line)
```

Required normal indexes:

```text
(repository_id)
(repository_id, file_path)
(repository_id, content_hash)
```

Day 5 does not require HNSW or IVFFlat.

V1 may use exact pgvector cosine search.

Different embedding dimensions must never be queried together inside one logical repository index.

Alembic owns these schema changes.

Raw semantic SQL must remain inside the PostgreSQL semantic infrastructure adapter, not inside Agent or Context Manager business logic.

---

# 20. Index Compatibility Contract

```python
class IndexCompatibilityStatus(StrEnum):
    COMPATIBLE = "COMPATIBLE"
    INDEX_NOT_FOUND = "INDEX_NOT_FOUND"
    INDEX_INCOMPATIBLE = "INDEX_INCOMPATIBLE"


@dataclass(frozen=True, slots=True)
class IndexCompatibility:
    status: IndexCompatibilityStatus
    reason: str | None
```

Before semantic retrieval compare current config:

```text
embedding_provider
embedding_model
embedding_dimension
index_version
chunk_strategy_version
```

with stored index metadata.

All must match.

Otherwise:

```text
INDEX_INCOMPATIBLE
```

No silent rebuild is permitted.

User-visible remediation must include:

```bash
nexus index --rebuild
```

An incompatible semantic index must not be queried.

---

# 21. Semantic Disabled and Fallback Semantics

Configuration:

```text
semantic_enabled = true | false
```

When:

```text
semantic_enabled = false
```

behavior is:

```text
skip semantic provider
→ lexical retrieval
→ selection/budget
→ continue normally
```

This is a valid supported mode.

When semantic is enabled but no index exists:

```text
semantic_status = INDEX_NOT_FOUND
```

The coding task must safely fall back to lexical retrieval and expose a user-visible/safe diagnostic that semantic retrieval is unavailable and `nexus index` can initialize it.

When the index is incompatible:

```text
semantic_status = INDEX_INCOMPATIBLE
```

The coding task must safely fall back to lexical retrieval and expose the required rebuild instruction.

When the semantic provider experiences a transient embedding/database failure:

* do not claim semantic retrieval succeeded;
* surface a structured Context error/warning;
* lexical-only continuation is permitted if policy and existing context are sufficient.

A semantic failure must not silently become an empty successful semantic result.

---

# 22. Configuration Contract

Day 5 adds/uses the following logical settings.

Names may follow existing RuntimeConfig naming conventions, but the semantics and defaults are frozen.

```toml
[context]
semantic_enabled = false
max_retrieved_chunks = 12
max_exploration_seed_chunks = 6
max_code_context_tokens = 12000
max_recent_observations = 8
max_model_input_tokens = 24000
max_file_size_bytes = 1048576

[embedding]
provider = "openai_compatible"
model = ""
dimension = 0
base_url = ""
```

`lexical_top_k=20`, `semantic_top_k=20`, `rrf_k=60`, and
`final_candidate_count=12` are fixed implementation constants, not TOML/environment
settings in the initial Day5 implementation. The code/chunk/observation/overall
budget ceilings are configurable downward only. `max_file_size_bytes` configures indexing
eligibility only.

`max_exploration_seed_chunks` has default and V1 maximum `6`, with allowed range
`0..6`. It limits accepted unique Day4 exploration seed chunks before hybrid candidates
are appended; it does not reserve lexical or semantic quotas.

When semantic retrieval/indexing is enabled:

```text
embedding.dimension MUST be > 0
```

Semantic retrieval is explicit opt-in. Existing model/database-only runtime
configuration therefore remains lexical-only by default. `semantic_enabled=true`
requires complete valid embedding configuration. `nexus index` requires the same
embedding configuration regardless of the chat runtime setting.

Configuration precedence remains the parent Specification contract:

```text
CLI
>
repo .nexus/config.toml
>
user ~/.nexus/config.toml
>
environment
>
defaults
```

Secrets may not be stored in committable repository TOML.

---

# 23. CLI Contract

Day 5 freezes these public forms:

```bash
nexus index
nexus index --rebuild
```

## `nexus index`

Runs incremental index/update for the current repository.

Successful output must provide a safe summary containing at least:

```text
scanned files
indexed/changed files
unchanged files
removed files
skipped files
chunk count
embedding provider/model
```

## `nexus index --rebuild`

Runs an explicit full semantic-index rebuild for the current repository.

CLI remains an adapter.

It must call application/index service assembled through the Composition Root.

CLI must not:

* contain scanning/chunking logic;
* issue pgvector SQL;
* instantiate concrete embedding vendor client;
* directly orchestrate persistence.

---

# 24. Context Error Contract

Day 5 uses the existing `ContextError` hierarchy.

Approved stable Day 5 error codes:

```text
INDEX_NOT_FOUND
INDEX_INCOMPATIBLE
INDEX_BUILD_FAILED
EMBEDDING_FAILED
SEMANTIC_SEARCH_FAILED
CONTEXT_BUILD_FAILED
```

Errors must include a safe user-facing message.

They must not expose:

* API keys;
* database credentials;
* complete raw prompt;
* private chain-of-thought.

`INDEX_NOT_FOUND` and `INDEX_INCOMPATIBLE` are recoverable for coding tasks through lexical fallback.

`INDEX_BUILD_FAILED` during explicit `nexus index` causes the command to fail truthfully.

---

# 25. Composition Root Changes

Day 5 Composition Root may add construction of:

```text
EmbeddingGateway
Chunker
RepositoryIndexer
LexicalSearchProvider
PgVectorSemanticSearchProvider
ContextProvider
ContextManager
```

Concrete dependency construction remains in:

```text
src/nexus/infrastructure/bootstrap/
```

The Agent Runtime receives abstractions, not pgvector/vendor clients.

No concrete semantic provider is constructed directly inside graph nodes.

---

# 26. Required Integration With `build_context`

Conceptual contract:

```python
async def build_context(state: AgentState) -> AgentState:
    request = ContextRequest(
        task=state.task,
        repository_id=...,
        workspace_path=...,
        exploration=state.exploration,
        run_id=state.run_id,
        session_id=state.session_id,
    )

    working_context = await context_manager.build(request)

    return state_with_context(...)
```

This is illustrative control flow, not permission to replace the existing graph/state architecture.

The actual node must preserve approved Day 4 state/event contracts.

`ContextBuilt` remains the RuntimeEvent for completed context construction.

Safe event metadata may include:

```text
selected chunk count
lexical candidate count
semantic candidate count
semantic enabled/used
truncated
estimated context tokens
```

Do not include complete retrieved source content in telemetry merely for observability.

---

# 27. Interaction With Existing Day 4 Context Selection

Day 4 deterministic exploration/seed behavior must not be discarded if it provides high-confidence repository instruction/manifest/task evidence.

Day 5 retrieval supplements and upgrades it.

High-confidence Day4 source-code seeds are normalized into the same `CodeChunk` identity
as retrieval candidates. Seeds and hybrid candidates are deduplicated and share one
chunk-count/token budget; no unbudgeted duplicate code copy may enter model input.

Normalize and deduplicate exploration seeds in their existing deterministic order and
accept at most `max_exploration_seed_chunks=6` unique seed chunks. Then append hybrid
candidates in existing deterministic RRF order, deduplicating against accepted seeds.
Exploration seeds remain first, while the combined result remains within 12 chunks and
12000 estimated code tokens. Lexical and semantic candidates continue to compete only
through RRF; no separate retrieval-source quota is reserved.

Context priority for source-code selection is:

```text
explicit task/path evidence
+
high-confidence exploration evidence
+
hybrid retrieval candidates
```

All selected code still shares the same `max_code_context_tokens` absolute ceiling.

Repository instructions are not discarded merely because they do not rank highly in RRF.

Security/repository instructions remain authoritative context and are not treated as ordinary retrievable code chunks.

When retrieval introduces a path not resolved by Day4 exploration, `ContextManager` must
read all applicable ancestor `AGENTS.md` files through the policy-safe repository path,
apply the existing Day4 scope/precedence/conflict rules, and only then accept the source
chunk into model context.

---

# 28. Security Rules

Day 5 must maintain:

* workspace containment;
* `.gitignore` and configured exclusions;
* no filesystem escape;
* no repository source mutation during indexing;
* no direct arbitrary subprocess from Agent/Context Manager;
* no secret logging;
* no full-repository prompt injection.

Content retrieved from repository files is **data/context**, not higher-authority Nexus system policy.

Repository `AGENTS.md` precedence continues to follow the approved Day 4 contract.

Semantic similarity must never elevate arbitrary repository text above Nexus safety/Frozen Baseline/user-task authority.

---

# 29. Required Unit Tests

At minimum:

### Chunker

* file <120 lines → one chunk;
* file =120 lines → one chunk;
* file >120 lines → correct overlap;
* starts `1,101,201...`;
* correct line metadata;
* stable `content_hash`;
* stable `file_hash`.

### File filtering

* ignored directory;
* `.gitignore`;
* binary file;
* oversized file;
* `AGENTS.md` excluded from ordinary code candidates while remaining available as scoped instructions;
* normal source file.

### Lexical retrieval

* exact symbol hit;
* duplicate hit normalization;
* canonical line-window mapping;
* lexical `top_k=20`.

### RRF

* lexical-only candidate;
* semantic-only candidate;
* candidate appearing in both;
* exact formula with `k=60`;
* stable deterministic tie-breaking;
* final candidate limit 12.

### Budget

* max 12 chunks;
* more than six unique exploration seed chunks are capped at six;
* a unique hybrid candidate remains eligible after the exploration seed cap;
* seed/hybrid chunk identity is deduplicated across their shared budget;
* max 12000 estimated code tokens;
* overflow candidate skipped;
* `truncated=True` when budget excludes candidates;
* complete input below the overall ceiling;
* code below 12000 while total input exceeds its ceiling;
* code evicted before recent conversation;
* older observations compacted before mandatory context;
* conversation reduced before manifest/layout evidence;
* repository instructions, current task, and required Plan retained;
* impossible mandatory context returns `CONTEXT_BUILD_FAILED` before model invocation;
* successful final serialized input is within `max_model_input_tokens`;
* Agent/model adapters apply no second independent history slice.

### Compatibility

* compatible metadata;
* missing index;
* provider mismatch;
* model mismatch;
* dimension mismatch;
* index version mismatch;
* chunk strategy mismatch.

---

# 30. Required PostgreSQL Integration Tests

Using PostgreSQL + pgvector:

1. Alembic upgrade creates semantic tables.
2. Alembic downgrade removes/reverts Day 5 schema correctly.
3. Insert chunk with embedding.
4. Query semantic nearest result using cosine metric.
5. repository isolation prevents cross-repository result leakage.
6. changed-file reindex removes old chunks.
7. unchanged file does not trigger new embedding.
8. removed file deletes existing chunks.
9. metadata is persisted.
10. incompatible metadata is detected before semantic query.

---

# 31. Required Context / Retrieval Integration Tests

Fixture repository must prove:

### Lexical

A query containing an exact code identifier finds the expected source chunk.

### Semantic

A query using a paraphrased concept, without the exact target identifier, retrieves the expected conceptually related chunk from pgvector.

### Hybrid

A deterministic fixture proves RRF ordering.

### Semantic disabled

With:

```text
semantic_enabled = false
```

the task still receives useful lexical context and proceeds normally.

### Missing index

Semantic enabled + no index:

```text
lexical fallback
+
INDEX_NOT_FOUND diagnostic
```

### Incompatible index

Changed embedding configuration:

```text
INDEX_INCOMPATIBLE
+
lexical fallback
+
nexus index --rebuild remediation
```

---

# 32. Required Index E2E Test

The Day 5 E2E fixture must execute:

```bash
nexus index
```

and demonstrate:

```text
repository scan
→ filter
→ chunk
→ embed
→ pgvector persist
```

Then perform retrieval proving the indexed repository is actually queryable.

A second `nexus index` without source changes must demonstrate unchanged-file reuse and no unnecessary re-embedding.

After modifying one fixture source file, another `nexus index` must demonstrate changed-file-only reindex behavior.

---

# 33. Required Coding-Task E2E

At least one existing Day 4 coding-loop fixture must run with Day 5 retrieval enabled.

Evidence must demonstrate:

```text
explore_repository
→ build_context
→ lexical + semantic retrieval
→ bounded WorkingContext
→ create_plan
→ existing Day 4 coding loop
```

Day 5 must not regress:

* approval;
* Tool Runtime;
* editing;
* validation;
* repair/replan;
* diff/finalization;
* session/resume.

---

# 34. ADR 004 Required Decision

Create/update:

```text
docs/adr/004-pgvector-semantic-retrieval.md
```

It must record:

## Context

Nexus requires repository semantic retrieval without coupling Agent Runtime to a vector-store vendor.

## Decision

Use:

```text
PostgreSQL + pgvector
SemanticSearchProvider abstraction
line-window chunking
cosine similarity
lexical + semantic hybrid retrieval
RRF
```

## Alternatives

At least:

```text
lexical-only
Qdrant
Milvus
AST/LSP chunking
weighted score merge
```

## Why

Explain:

* shared PostgreSQL infrastructure;
* V1 operational simplicity;
* provider isolation;
* lexical and semantic retrieval solve different failure modes;
* RRF avoids comparing incomparable raw score scales.

## Trade-offs

Include:

* line windows are language-agnostic but structurally weak;
* exact pgvector search does not optimize very large repositories;
* embeddings require index maintenance and compatibility;
* future provider/chunker replacement remains possible.

## Approved Day 5 dependencies

The only Day5 dependency additions are `pgvector` and `pathspec`, managed through the
existing `uv`/`pyproject.toml` workflow. `pgvector` supplies PostgreSQL/SQLAlchemy vector
integration; `pathspec` supplies `.gitignore`-compatible matching. Embedding reuses the
existing OpenAI-compatible/LangChain-compatible infrastructure where practical. Any
additional dependency requires separate approval.

---

# 35. Implementation Decision Boundary

Codex may decide:

* private helper names;
* module-private utility layout;
* SQLAlchemy mapping style consistent with repository conventions;
* internal batching size for embedding, provided behavior is equivalent;
* internal test fixture names;
* safe error wording;
* standard-library implementation details;
* equivalent low-level parsing helpers.

Codex may **not** decide or alter:

* graph node/edge topology;
* ContextProvider/Manager responsibilities;
* Chunker strategy;
* line-window sizes;
* retrieval limits;
* cosine metric;
* RRF algorithm/k;
* candidate identity;
* tie-breaking;
* budget ceiling;
* observation retention count;
* index compatibility fields;
* semantic persistence ownership;
* public CLI commands;
* PostgreSQL vs alternative vector store;
* embedding contract;
* new major dependency;
* Day 6+ capability.

---

# 36. Explicitly Deferred Scope

The following remain deferred:

```text
AST chunking
Tree-sitter
LSP integration
symbol graph retrieval
cross-encoder reranker
model-driven retrieval planner
query rewriting LLM
HNSW/IVFFlat tuning
automatic file watcher
background indexing daemon
Qdrant
Milvus
MCP
Skill selection
long-term memory
multi-agent retrieval
remote repository indexing
```

They may be discussed only as future extension points.

---

# 37. Day 5 Acceptance Gate

Day 5 is accepted only when all of the following are true:

1. `nexus index` indexes a fixture repository end-to-end.
2. PostgreSQL + pgvector stores and retrieves actual embeddings.
3. exact lexical token fixture succeeds.
4. paraphrased semantic fixture succeeds.
5. hybrid ranking is deterministic.
6. RRF uses exactly `k=60`.
7. final retrieval uses at most 12 chunks.
8. selected code context respects the 12000-token ceiling.
9. semantic-disabled mode remains functional.
10. missing semantic index falls back safely.
11. incompatible embedding config returns `INDEX_INCOMPATIBLE`.
12. user-visible remediation includes `nexus index --rebuild`.
13. rebuild behavior works.
14. unchanged files do not re-embed unnecessarily.
15. changed-file reindex works.
16. removed-file cleanup works.
17. semantic tables are owned by Alembic migration.
18. Agent Runtime does not depend directly on pgvector.
19. graph topology remains unchanged.
20. Day 4 coding-loop regressions remain green.
21. lint/type/test/CI are green.
22. ADR 004 is complete.
23. Architecture Review passes.
24. Product Owner Knowledge Review passes.

Only then may:

```text
feature/day05-context-hybrid-retrieval
```

be merged.

---

# 38. Day 5 Definition of Done

Day 5 is complete when Nexus can truthfully demonstrate:

```text
Repository
   ↓
nexus index
   ↓
filter → chunk → embed → pgvector

Task
   ↓
explore_repository
   ↓
build_context
   ↓
lexical + semantic retrieval
   ↓
RRF
   ↓
budgeted WorkingContext
   ↓
existing Day 4 coding loop
```

with safe lexical-only fallback and without loading the complete repository into the model context.

**Implementation authorization: APPROVED under this Addendum.**

---

# 39. Approval Record

```text
Day 5 decisions SD-5-01 through SD-5-08A:
RESOLVED / APPROVED BY PRODUCT OWNER / ARCHITECT

Consolidation into v0.1.2 without semantic change:
APPROVED

Architecture Review correction after commit 784790f:
APPROVED — semantic retrieval is explicit opt-in and defaults to disabled

Second Architecture Review correction after commit cebbe3f:
APPROVED — exploration seeds default to and are capped at six unique chunks

Implementation authorization:
YES — DAY 5 ONLY
```
