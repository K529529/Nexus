# ADR 002: Runtime Abstraction

## Context

CLI, tests, evaluation, and future UI/API adapters need one programmatic execution path
that does not expose model or graph frameworks.

## Decision

Make async `NexusRuntime.run(task, session_id=None)` the stable entry point. It emits
Nexus-owned typed events and delegates graph execution through `GraphRuntime`.

## Alternatives

- Put orchestration in each interface adapter.
- Expose the compiled LangGraph object as the SDK.
- Return one blocking string instead of an async event stream.

## Why

One runtime path prevents adapter-specific behavior and establishes a safe observable seam
for later capabilities.

## Trade-offs

The runtime must normalize errors and events, and even the small Day 1 lifecycle uses an
async iterator. This is deliberate because later adapters can consume the same contract.

## Future Migration

Later Days may add approved event types and services behind the runtime. Existing consumers
continue to iterate `RuntimeEvent`; contract changes require specification approval.

