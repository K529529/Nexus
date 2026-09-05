# Day 5 implementation, operations, and acceptance plan

Authority: V1.1.1 baseline and the sole Day 5 implementation contract,
`docs/spec-addenda/Nexus_Day5_Contract_Addendum_v0.1.2.md`.

1. Preserve approved contracts in docs/spec-addenda; add the frozen domain values,
   provider ports, additive WorkingContext fields and configuration.
2. Implement line windows, filtering, independent embedding, transactional incremental
   PostgreSQL/pgvector indexing and Alembic migration; expose the thin index CLI.
3. Implement policy-safe lexical retrieval, compatibility-gated semantic retrieval,
   exact RRF, seed normalization, instruction resolution and shared code budgeting.
4. Integrate ContextManager at existing build_context/agent_step boundaries; prepare
   bounded history and observations; enforce the complete serialized model input budget
   for planning, repair and Agent calls without changing graph edges or counters.
5. Validate unit contracts, PostgreSQL migration/index lifecycle, retrieval and CLI E2E,
   Day 4 coding/approval/validation/resume regressions; run Ruff, Mypy and pytest.
6. Deliver ADR 004, index/config guide and acceptance evidence. Distinguish deterministic
   fixtures from live embedding evidence, and local checks from remote CI/Architecture
   Review/Product Owner Knowledge Review. No merge is implied by local success.

Deferred: all Day 6+ scope, watchers, AST/LSP, alternate vector stores, ANN tuning,
rerankers, query-planning models, Skills, long-term memory and multi-agent retrieval.

## Configuration and migration

PostgreSQL must provide pgvector. Apply the normal business/semantic migrations:

```powershell
uv --cache-dir .uv-cache sync --frozen --dev
uv --cache-dir .uv-cache run --frozen --env-file .env alembic upgrade head
```

Day5 adds `day05_0003` after `day03_0002`. Downgrade removes only the two Day5
tables and retains the shared vector extension. LangGraph continues to own checkpoint
tables separately. `nexus index` never runs migrations silently.

```toml
[context]
semantic_enabled = false
max_model_input_tokens = 24000
max_code_context_tokens = 12000
max_retrieved_chunks = 12
max_recent_observations = 8
max_file_size_bytes = 1048576

[embedding]
provider = "openai_compatible"
model = "YOUR_EMBEDDING_MODEL"
dimension = 1536 # example only; use the deployed model's actual dimension
base_url = "https://YOUR_ENDPOINT/v1"
```

Set `NEXUS_EMBEDDING_API_KEY` in the process environment or a locally ignored `.env`.
Embedding model, dimension, and base URL may use `NEXUS_EMBEDDING_MODEL`,
`NEXUS_EMBEDDING_DIMENSION`, and `NEXUS_EMBEDDING_BASE_URL`. Semantic enablement uses
`NEXUS_SEMANTIC_ENABLED`. Secrets are rejected in TOML. Fixed retrieval values
20/20/60/12 are not tunable. Budget ceilings can only be reduced; code budget cannot
exceed total input. Disabling semantic retrieval requires no embedding configuration;
explicit indexing always does and never depends on the chat model.

## Index lifecycle

```powershell
uv --cache-dir .uv-cache run --frozen --env-file .env nexus index
uv --cache-dir .uv-cache run --frozen --env-file .env nexus index --rebuild
```

The first form incrementally indexes the canonical current workspace and reports scanned,
changed, unchanged, removed, skipped, chunk-count, and embedding metadata. The second
explicitly replaces only that repository's semantic records.

Text uses normalized UTF-8 with optional BOM removal and LF-normalized hashing. Binary,
undecodable, oversized, ignored, generated/cache/dependency files, and `AGENTS.md` as an
ordinary code candidate are omitted. Repository instructions remain available separately
through scoped policy-safe reads. Nested `.gitignore` rules and workspace containment apply.

Unchanged files reuse vectors. Changed files transactionally replace old windows; removed
or newly ignored files lose stored chunks. Failure rolls back and returns
`INDEX_BUILD_FAILED`. Configuration mismatch returns `INDEX_INCOMPATIBLE` and requires
`nexus index --rebuild`. Source, Session/Run/Approval, and checkpoint data are untouched.

## Retrieval, fallback, and model input

`build_context` combines deterministic lexical retrieval and compatible pgvector cosine
retrieval using RRF `1/(60+rank)`. Raw cosine distance remains diagnostic only. Day4 seeds
and fused candidates normalize/deduplicate into one budget of at most 12 chunks and 12000
estimated code tokens. A stale/deleted/newly ignored chunk is rejected before model use.
New source paths resolve ancestor `AGENTS.md` scope before selection.

- Semantic disabled: lexical context continues normally.
- `INDEX_NOT_FOUND`: lexical continuation plus `nexus index` guidance.
- `INDEX_INCOMPATIBLE`: lexical continuation plus rebuild guidance.
- Embedding/database failure: safe visible warning without claiming semantic success.

The manager retains up to eight recent observations and six recent conversation turns,
with deterministic factual compaction for older entries. Complete AgentState/session history
remains durable outside the prompt. The default complete model-input ceiling is 24000
estimated tokens, including system messages, instructions, task, Plan, code, history,
observations, and serialized framing.

The sole reduction order is: lowest-priority code; observations; conversation; manifest/
layout evidence; remaining optional context; safe failure. System/safety instructions,
applicable repository instructions, current task, and required current Plan are never
silently removed. Mandatory overflow returns `CONTEXT_BUILD_FAILED` before model invocation.

## Acceptance evidence map

| Contract / acceptance item | Evidence |
| --- | --- |
| windows, overlap, short-file identity, normalized hashes | `test_day5_context.py`, `test_day5_retrieval.py` |
| ignore/binary/size/instruction filtering | `test_day5_context.py`, `test_day5_retrieval.py` |
| lexical top 20, semantic cosine, deterministic RRF/final 12 | `test_day5_retrieval.py` |
| repository isolation and all compatibility fields | `test_day5_retrieval.py` against PostgreSQL/pgvector |
| incremental reuse/change/removal/rollback/rebuild | `test_day5_retrieval.py` |
| Alembic upgrade/downgrade/re-upgrade | `test_migrations.py` |
| three-run `nexus index` CLI lifecycle and subsequent query | `test_index_cli_e2e_three_runs` |
| disabled/missing/incompatible/transient fallback | `test_context_retrieval_budget_instructions_and_fallback` |
| shared seed/retrieval budget and whole-chunk overflow skip | `test_lexical_top20_and_shared_budget_overflow_skip` |
| total prompt accounting and mandatory-context protection | `test_day5_context.py` |
| code-before-conversation eviction and manager ownership | `test_day5_context.py` |
| Day4 coding loop and process reconstruction with semantic on/off | `test_day4_coding_loop.py` |
| real-provider paraphrased concept acceptance | `test_day5_live_embedding.py`, deployment configuration required |
| architecture/provider/ownership decision | `docs/adr/004-pgvector-semantic-retrieval.md` |

## Verification and acceptance boundary

```powershell
uv --cache-dir .uv-cache run --frozen ruff check .
uv --cache-dir .uv-cache run --frozen mypy src tests
uv --cache-dir .uv-cache run --frozen --env-file .env pytest --ignore=tests/local
```

`tests/local` is an ignored, intentionally failing Day4 debugging fixture and is absent
from CI. The live embedding test skips without deployment credentials; deterministic
`FixtureEmbedding` proves persistence/query/ranking mechanics, not real semantic quality.

Local success does not establish remote CI, formal Architecture Review, live embedding
acceptance, or Product Owner Knowledge Review. No merge is authorized until every Day5
gate in the v0.1.2 contract passes.

For the knowledge review, explain lexical versus semantic matching, why RRF combines ranks,
how provider inversion and persistence ownership protect Runtime, why embedding changes
require explicit rebuild, how code and total-input budgets differ, and why deterministic
vectors do not prove real embedding quality.
