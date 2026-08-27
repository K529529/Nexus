# Nexus Day 2 Contract Addendum v1.0

**Status:** APPROVED
**Applies To:** Day 2 — State, Session, Persistence & Checkpoint
**Authority:** Product Owner / Architect approved implementation contract
**Baseline:** Nexus V1 Specification v1.1.1

## Authority and Scope

This addendum freezes implementation details required by Day 2 without changing the
frozen architecture, persistence ownership boundaries, milestone scope, or roadmap. The
v1.1.1 baseline wins if an explicit conflict is discovered.

The approved implementation branch is `feature/day02-state-session-persistence`.

## Identifiers and Time

- Domain, application, and CLI identifiers are canonical UUID strings; PostgreSQL columns
  use UUID. Nexus/application code generates identifiers.
- Persisted timestamps are timezone-aware UTC values stored as `TIMESTAMPTZ`.

## Nexus-owned Business Schema

`repositories`:

```text
id UUID PRIMARY KEY
canonical_path TEXT NOT NULL UNIQUE
metadata JSONB NOT NULL DEFAULT '{}'
configuration_reference TEXT NULL
created_at TIMESTAMPTZ NOT NULL
```

`sessions`:

```text
session_id UUID PRIMARY KEY
repository_id UUID NOT NULL REFERENCES repositories(id) ON DELETE RESTRICT
created_at TIMESTAMPTZ NOT NULL
last_active_at TIMESTAMPTZ NOT NULL
configuration_reference TEXT NULL
```

`runs`:

```text
run_id UUID PRIMARY KEY
session_id UUID NOT NULL REFERENCES sessions(session_id) ON DELETE RESTRICT
task TEXT NOT NULL
status TEXT NOT NULL: RUNNING | INTERRUPTED | COMPLETED | FAILED
model_metadata JSONB NULL
started_at TIMESTAMPTZ NOT NULL
finished_at TIMESTAMPTZ NULL
token_count INTEGER NOT NULL DEFAULT 0
tool_call_count INTEGER NOT NULL DEFAULT 0
changed_file_refs JSONB NULL
final_outcome JSONB NULL
graph_thread_id TEXT NOT NULL UNIQUE
```

`session_turns`:

```text
id UUID PRIMARY KEY
session_id UUID NOT NULL REFERENCES sessions(session_id) ON DELETE RESTRICT
run_id UUID NULL REFERENCES runs(run_id) ON DELETE RESTRICT
sequence INTEGER NOT NULL
role TEXT NOT NULL
content TEXT NOT NULL
metadata JSONB NULL
created_at TIMESTAMPTZ NOT NULL
UNIQUE(session_id, sequence)
```

No Day 2 delete command or cascade-delete product behavior is authorized.

## Repository Identity

Resolve an existing workspace with `Path.resolve(strict=True)` and compare its
platform-normalized path. Windows comparison uses `os.path.normcase`; arbitrary manual
lowercasing is prohibited. Missing and cross-repository sessions raise `SessionError`, and
ownership validation must finish before graph resume.

## Repository and Transaction Contracts

The async `RepositoryRepository`, `SessionRepository`, `RunRepository`, and
`SessionTurnRepository` ports expose only required add/get/list/query/update operations and
never expose SQLAlchemy objects or raw SQL.

A minimal Day 2 `SessionUnitOfWork` owns transaction begin, commit, rollback, and the four
repositories. Repositories may flush but never independently commit. It is not a generic
DAO, transaction bus, event-sourcing layer, or repository hierarchy.

Initial run establishment atomically resolves/creates Repository and Session, creates a
RUNNING Run, writes the user SessionTurn, and updates `last_active_at`. Completion
atomically persists final status/outcome, assistant turn, and session activity. Failed
units roll back.

## Checkpoint Ownership and Linkage

Nexus owns `Session → Run → graph_thread_id`. LangGraph owns checkpoint payloads,
metadata, lineage, schema, and migrations. Nexus business code never scans checkpoint
tables or selects historical checkpoint IDs.

Day 2 maps one Run to one independent LangGraph thread, using a stable namespaced value
derived from `run_id`. `run_id`, `session_id`, `thread_id`, and `checkpoint_id` remain
distinct concepts.

Use the official asynchronous PostgreSQL checkpointer, `AsyncPostgresSaver` or its
compatible official successor. `CheckpointProvider` owns connection, official `setup()`,
and release. Nexus Alembic never creates or alters checkpoint tables.

## Runtime and Resume

`GraphRuntime` retains normal Day 1 execution and minimally adds thread binding and resume.
Normal `nexus chat` never interrupts artificially. `RuntimeStatus.INTERRUPTED` and a safe
typed `RunInterrupted` event represent a durable resumable pause.

The verification fixture may use LangGraph `interrupt_before`. It must dispose runtime A
and reconstruct runtime B without carrying the old Python `AgentState`. Resume selects the
latest INTERRUPTED Run deterministically, restores through its `graph_thread_id`, and
persists the real terminal outcome.

## CLI

- `nexus session list` lists only current-repository sessions ordered by
  `last_active_at DESC`, rendering `session_id`, `last_active_at`, and `resumable`.
- `nexus session resume <session-id>` validates ownership and resumes the latest
  INTERRUPTED Run. Missing, cross-repository, and non-resumable cases return safe
  `SessionError` values and exit code 1.

Stored SessionTurn history is not blindly injected into prompts. Day 5 owns broader
selection and compaction.

## Ownership and Scope Exclusions

Nexus Alembic owns only `repositories`, `sessions`, `runs`, and `session_turns`. The
official checkpointer owns its setup. Adding the official companion checkpoint package is
approved when compatible with the installed LangGraph version.

Day 2 still excludes approvals, tools, shell/sandbox, planner, editing, repository
exploration, retrieval/pgvector indexing, MCP, Skills, Evaluation, and future UI/API work.
