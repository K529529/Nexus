# ADR 004: PostgreSQL/pgvector semantic retrieval behind Nexus providers

Status: implementation follows the approved Day 5 contract; formal review remains a gate.

## Context

Exact identifiers and paths are best found lexically. A task may instead describe a
concept without using the repository's identifiers. Nexus needs both retrieval modes
without coupling graph execution to an embedding vendor or vector database.

## Decision

Use the existing PostgreSQL infrastructure with pgvector and the Nexus-owned
SemanticSearchProvider, LexicalSearchProvider, ContextProvider and ContextManager ports.
The graph retains its Day 4 nodes and edges. Context assembly invokes these abstractions.

Use normalized UTF-8 text, language-agnostic 120-line windows at starts 1, 101, 201, etc.,
with the approved unique-window exception for non-empty files of at most 120 lines.
Use exact cosine distance, 20 lexical and 20 semantic candidates, and RRF with k=60.
The stable fused result retains at most 12 candidates. Exploration seeds and selected
retrieval chunks share the code-count/token budget; no second code copy enters prompts.

Alembic owns repository_semantic_indexes and semantic_code_chunks. Indexing registers
the existing business repository identity, without creating a coding Session/Run or
touching checkpointer tables. Each repository operation holds a PostgreSQL transaction
advisory lock and atomically updates its chunks and compatibility metadata. A query holds
a shared lock while validating metadata and searching, preventing a concurrent rebuild
from changing dimensions between validation and query. Changed and removed files are
replaced/deleted; unchanged hashes avoid embedding calls. Rebuild is explicit and scoped
to the current repository. Rollback preserves the previous committed index.

ContextManager owns both selection and complete model-input budgeting. Production
Planner/Agent adapters supply a pure rendering callback to the manager's private fit
helper so that system messages, JSON framing, validation/repair metadata and the selected
context are counted together. Adapters neither slice nor independently compact history.
The Day 5 v0.1.2 sequence is the only eviction algorithm. Mandatory context overflow raises
CONTEXT_BUILD_FAILED before the model call. Compaction is deterministic and factual;
it introduces no additional LLM call or step.

## Alternatives

- Lexical-only: supported fallback, but does not demonstrate semantic concept matching.
- Qdrant or Milvus: extra operational systems, expressly deferred in V1.
- AST/LSP chunking: stronger structure but language-specific dependencies and scope.
- Weighted raw-score merging: lexical and vector scores are not comparable scales.
- Model reranking: extra latency/cost and an unapproved ranking contract.

## Why

PostgreSQL is already operated for Nexus. Separate logical ownership preserves the
business/checkpoint/retrieval boundaries while keeping V1 deployment simple. Provider
inversion isolates vendor clients and vector SQL. RRF combines rank evidence without
assuming comparable lexical and semantic numerical scores.

The approved pgvector dependency supplies PostgreSQL/SQLAlchemy vector integration;
pathspec supplies gitignore pattern semantics. Existing LangChain OpenAI-compatible
dependencies provide embeddings. No further provider SDK or tokenizer was added.

## Trade-offs

Line windows are language-agnostic but may split a function. Exact vector search does
not optimize very large repositories. Index maintenance, external embedding availability
and compatibility checking remain operational responsibilities. The default 1 MiB
eligibility threshold can be changed for indexing; it does not widen Tool read limits.
Stale or now-ineligible candidates are rejected when selecting model context, without
an automatic rebuild. The character/4 tokenizer fallback is a reproducible estimate,
not a claim of exact vendor tokenization. Real embedding quality requires separate
provider acceptance evidence; known-vector tests prove mechanics only.

## Future migration

New vector providers, chunkers, ANN indexes, model-capacity integration or higher budgets
require an approved future contract. None is implemented by Day 5. The provider boundary
keeps those changes outside graph topology and the public runtime lifecycle.
