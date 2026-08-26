# ADR 001: Use LangGraph for V1

## Context

Nexus V1 needs an async graph runtime that can grow into the frozen lifecycle in later
Days while Day 1 implements only one model-backed node.

## Decision

Use LangGraph as the V1 graph implementation. Keep every LangGraph type and compiled graph
inside the infrastructure adapter behind the Nexus-owned `GraphRuntime` port.

## Alternatives

- Build a custom graph/state-machine engine.
- Expose LangGraph directly to `NexusRuntime` and CLI consumers.
- Call the model directly and postpone the graph seam.

## Why

LangGraph is the frozen V1 technology choice and supports the later lifecycle without
forcing framework types into Nexus-owned contracts.

## Trade-offs

Nexus carries a framework dependency and must maintain explicit translation at the adapter
boundary. The Day 1 graph is intentionally more structured than a direct model call.

## Future Migration

A future graph engine can implement `GraphRuntime` without changing CLI or runtime
consumers. Such a change requires an approved specification/ADR update.

