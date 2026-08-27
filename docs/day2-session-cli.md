# Day 2 Persistence and Session CLI

## Prepare PostgreSQL

Start the development database and apply Nexus-owned business migrations:

```bash
docker compose up -d postgres
uv run alembic upgrade head
```

`NEXUS_DATABASE_URL` overrides the default PostgreSQL URL for both business persistence
and the official checkpointer. Alembic manages only `repositories`, `sessions`, `runs`, and
`session_turns`; checkpointer setup remains owned by LangGraph.

## Run and List Sessions

Normal chat remains non-interrupted and now persists its Session, Run, and SessionTurn
records:

```bash
uv run nexus chat "Reply with a short greeting."
uv run nexus session list
```

The list is restricted to the current canonical repository and shows whether each session
has an interrupted Run.

## Resume

Resume the newest interrupted Run for a Session:

```bash
uv run nexus session resume <session-id>
```

Nexus rejects missing sessions, sessions owned by another canonical repository, and
sessions without an interrupted Run. It does not inspect checkpoint tables during this
selection.

## Reproduce Durable Recovery

The focused integration test creates a disposable PostgreSQL database and proves the
complete reconstruction boundary:

```bash
uv run pytest tests/integration/test_checkpoint_resume.py -v
```

Its sequence is:

```text
runtime A starts a persisted Run
→ LangGraph interrupts before model_response
→ AsyncPostgresSaver persists a checkpoint
→ Nexus marks the Run INTERRUPTED
→ runtime A and all resources close
→ runtime B is constructed
→ SessionService validates repository ownership
→ GraphRuntime resumes by persisted graph_thread_id
→ the model node continues
→ Nexus persists COMPLETED outcome and assistant SessionTurn
```

No Python `AgentState` object passes from runtime A to runtime B.

## Roll Back the Business Migration

```bash
uv run alembic downgrade base
```

This removes only Nexus-owned Day 2 business tables. It does not manage or remove
LangGraph checkpoint tables.
