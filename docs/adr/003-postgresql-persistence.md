# ADR 003: PostgreSQL Persistence

## Context

Nexus must keep user-facing sessions and run history durable while also allowing
LangGraph execution to recover after process reconstruction. These stores have different
owners and lifecycles even when they use the same PostgreSQL instance.

## Decision

Use PostgreSQL for Nexus V1 persistence. Nexus-owned Repository, NexusSession, Run, and
SessionTurn data use async SQLAlchemy repositories and reviewed Alembic migrations.

Keep LangGraph checkpoints in a separate ownership boundary managed by the official
`AsyncPostgresSaver`. Nexus stores only the Run's `graph_thread_id`; it neither stores
checkpoint payloads nor manages checkpoint tables through Alembic.

## Alternatives

- Store sessions and checkpoints in one Nexus-owned schema.
- Use SQLite or process memory for Day 2 recovery.
- Build a custom checkpoint adapter and schema.
- Query LangGraph checkpoint tables as business history.

## Why

PostgreSQL is the frozen V1 persistence choice and provides transactional business data
plus durable process-independent checkpoints. SQLAlchemy preserves domain independence,
Alembic gives Nexus explicit upgrade/rollback ownership, and the official saver preserves
LangGraph's checkpoint semantics without reverse engineering.

The separation prevents graph implementation state from becoming the only copy of
user-visible history and permits either persistence concern to evolve behind its boundary.

## Trade-offs

Development and tests require a real PostgreSQL service. Two persistence lifecycles must
be operated: Nexus Alembic for business tables and official checkpointer setup for
LangGraph tables. The official async psycopg driver also requires a Selector event loop on
Windows, which the Composition Root configures.

## Future Migration

Future business schema changes remain Nexus Alembic migrations. A future graph engine can
replace the checkpoint adapter without changing Session or Run identity. Day 5 may use the
same PostgreSQL infrastructure for pgvector behind `SemanticSearchProvider`, but no vector
schema or retrieval behavior is introduced by this decision.
